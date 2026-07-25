/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "voice_input_controller.h"

#include <algorithm>
#include <application.h>
#include <audio_codec.h>
#include <board.h>
#include <cJSON.h>
#include <esp_crt_bundle.h>
#include <esp_http_client.h>
#include <esp_netif.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <mooncake_log.h>
#include <settings.h>
#include <string_view>
#ifdef TACHIKOMA_DEBUG_TIMING
// TEMPORARY, investigation-only -- see the TACHIKOMA_DEBUG_TIMING option in
// CMakeLists.txt. esp_timer_get_time() (microsecond, monotonic) rather than
// GetHAL().millis(): a single InputData() call is expected to be fast, and
// millisecond resolution could hide exactly the cost this is meant to catch.
#include <esp_timer.h>
#endif

#include "ai_gateway/ai_gateway_client.h"
#include "ai_gateway/speech_announcer.h"
#include "hal/hal.h"
#include "stackchan/state/tachikoma_state_manager.h"
#include "stackchan/state/tachikoma_state_types.h"

namespace stackchan::voice_input {
namespace {
constexpr std::string_view kTag = "VoiceInput";
constexpr char kSettingsNamespace[] = "tachi_stt";  // NVS namespace <= 15 chars
constexpr uint32_t kMaxRecordingMs = 8000;
constexpr uint32_t kMinRecordingMs = 300;
// How long after playback ends (Speaking -> Idle) to ignore a new Press.
// Confirmed on real hardware: the speaker's own vibration during Zephyr
// TTS playback reached the Si12T head-touch sensor and was misread as a
// physical touch, immediately re-triggering push-to-talk and producing an
// unbounded reply loop with no user involvement. Tune here if false
// triggers still slip through (louder replies vibrate longer) or if this
// proves longer than necessary once retested.
constexpr uint32_t kPostSpeechCooldownMs = 1500;
constexpr size_t kFrameSamples = 320;  // ~20ms at 16kHz mono
constexpr size_t kMaxRecordingSamples = 128 * 1024;  // 256 KiB of int16 PCM; mirrors SpeechAnnouncer's kMaxAudioBytes
constexpr int kFallbackSampleRate = 16000;
constexpr size_t kMaxResponseBytes = 4096;

struct HttpBuffer {
    std::string body;
};

esp_err_t HttpEvent(esp_http_client_event_t* event)
{
    auto* buffer = static_cast<HttpBuffer*>(event->user_data);
    if (event->event_id == HTTP_EVENT_ON_DATA && buffer != nullptr && event->data != nullptr) {
        if (buffer->body.size() + event->data_len > kMaxResponseBytes) {
            return ESP_ERR_NO_MEM;
        }
        buffer->body.append(static_cast<const char*>(event->data), event->data_len);
    }
    return ESP_OK;
}

}  // namespace

bool HasExceededMaxDuration(uint32_t now, uint32_t started_ms, uint32_t max_duration_ms)
{
    return static_cast<int32_t>(now - started_ms) >= static_cast<int32_t>(max_duration_ms);
}

bool MeetsMinimumDuration(uint32_t duration_ms, uint32_t min_duration_ms)
{
    return duration_ms >= min_duration_ms;
}

size_t ClampToRemainingCapacity(size_t current_size, size_t incoming, size_t capacity)
{
    if (current_size >= capacity) {
        return 0;
    }
    return std::min(incoming, capacity - current_size);
}

std::vector<int16_t> DownmixToChannel0(const std::vector<int16_t>& interleaved, int channels)
{
    if (channels <= 1) {
        return interleaved;
    }
    std::vector<int16_t> mono;
    mono.reserve(interleaved.size() / static_cast<size_t>(channels));
    for (size_t i = 0; i < interleaved.size(); i += static_cast<size_t>(channels)) {
        mono.push_back(interleaved[i]);
    }
    return mono;
}

TriggerAction MapHeadTouchGesture(HeadPetGesture gesture)
{
    switch (gesture) {
        case HeadPetGesture::Press: return TriggerAction::Press;
        case HeadPetGesture::Release: return TriggerAction::Release;
        case HeadPetGesture::None:
        case HeadPetGesture::SwipeForward:
        case HeadPetGesture::SwipeBackward:
            return TriggerAction::None;
    }
    return TriggerAction::None;
}

VoiceInputController& GetVoiceInputController()
{
    static VoiceInputController controller;
    return controller;
}

VoiceInputConfig VoiceInputController::LoadConfig() const
{
    Settings settings(kSettingsNamespace);
    VoiceInputConfig config;
    config.endpoint = settings.GetString("url");
    config.device_token = settings.GetString("device_token");
    config.device_id = settings.GetString("device_id", GetHAL().getFactoryMacString(""));
    return config;
}

bool VoiceInputController::ConfigureTranscribeQueue(const std::string& endpoint, const std::string& device_token,
                                                    const std::string& device_id)
{
    if (endpoint.empty() || device_token.empty() || endpoint.size() > 255 || device_token.size() > 255) {
        return false;
    }
    Settings settings(kSettingsNamespace, true);
    settings.SetString("url", endpoint);
    settings.SetString("device_token", device_token);
    if (!device_id.empty()) {
        settings.SetString("device_id", device_id);
    }
    return true;
}

void VoiceInputController::OnButtonPressed(uint32_t now)
{
    const auto state = tachikoma_state::GetTachikomaStateManager().GetCurrentState();
    if (state != tachikoma_state::TachikomaState::Idle) {
        // Speaking is deliberately excluded (was previously allowed, to let
        // a user barge in on a reply): combined with the speaker-vibration
        // false-trigger above, allowing a Press while still Speaking meant
        // the tail end of the false trigger itself could restart the loop.
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = VoiceInputErrorCode::Busy;
        return;
    }
    if (static_cast<int32_t>(now - cooldown_until_ms_) < 0) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = VoiceInputErrorCode::Cooldown;
        return;
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (recording_ || busy_) {
            return;
        }
        recording_ = true;
        recording_started_ms_ = now;
        buffer_.clear();
        last_error_ = VoiceInputErrorCode::None;
    }

    // Stop xiaozhi's own wake-word/processor task from also reading the
    // mic for the duration of this recording -- see
    // AudioService::SetAudioInputPaused()'s declaration for why both
    // reading at once corrupts the recording instead of erroring cleanly.
    Application::GetInstance().GetAudioService().SetAudioInputPaused(true);

    auto* codec = Board::GetInstance().GetAudioCodec();
    if (codec != nullptr) {
        codec->EnableInput(true);
    }
    tachikoma_state::GetTachikomaStateManager().Notify(tachikoma_state::TachikomaEvent::UserSpeechStarted);
}

void VoiceInputController::OnButtonReleased(uint32_t now)
{
    StopRecordingAndUpload(now);
}

void VoiceInputController::ConnectHeadTouchTrigger()
{
    if (head_touch_connected_) {
        return;
    }
    head_touch_connected_ = true;
    head_touch_connection_ = GetHAL().onHeadPetGesture.connect([this](HeadPetGesture gesture) {
        switch (MapHeadTouchGesture(gesture)) {
            case TriggerAction::Press:
                OnButtonPressed(GetHAL().millis());
                break;
            case TriggerAction::Release:
                OnButtonReleased(GetHAL().millis());
                break;
            case TriggerAction::None:
                break;
        }
    });
}

void VoiceInputController::Update(uint32_t now)
{
    const auto current_state = tachikoma_state::GetTachikomaStateManager().GetCurrentState();
    if (last_observed_state_ == tachikoma_state::TachikomaState::Speaking &&
        current_state == tachikoma_state::TachikomaState::Idle) {
        cooldown_until_ms_ = now + kPostSpeechCooldownMs;
    }
    last_observed_state_ = current_state;
}

void VoiceInputController::StartRecordingTask()
{
    if (recording_task_started_) {
        return;
    }
    recording_task_started_ = true;
    // Mirrors AudioService::AudioInputTask()'s CONFIG_USE_AUDIO_PROCESSOR
    // configuration (stack 2048*3, priority 8, pinned to core 0): both tasks
    // read the same physical mic and are mutually exclusive via
    // AudioService::SetAudioInputPaused() (never run their capture work at
    // the same time), so matching stack/priority/core keeps their scheduling
    // behavior comparable instead of one starving the other.
    xTaskCreatePinnedToCore(&VoiceInputController::RecordingTaskEntry, "voice_input_rec", 2048 * 3, this, 8, nullptr,
                            0);
}

void VoiceInputController::RecordingTaskEntry(void* arg)
{
    static_cast<VoiceInputController*>(arg)->RecordingTask();
}

void VoiceInputController::RecordingTask()
{
#ifdef TACHIKOMA_DEBUG_TIMING
    // TEMPORARY, investigation-only -- see the TACHIKOMA_DEBUG_TIMING option
    // in CMakeLists.txt. Same tick_interval measurement as hal.cpp's shared
    // task used to log, but for this task's own loop, so the fix can be
    // verified against the same yardstick the original bug was measured with.
    uint32_t debug_last_tick_ms = 0;
    bool debug_had_last_tick = false;
#endif
    while (true) {
        const uint32_t now = GetHAL().millis();
#ifdef TACHIKOMA_DEBUG_TIMING
        if (IsRecording()) {
            if (debug_had_last_tick) {
                mclog::tagInfo(kTag, "debug_timing rec_task_tick_interval_ms={}", now - debug_last_tick_ms);
            }
            debug_had_last_tick = true;
            debug_last_tick_ms = now;
        } else {
            debug_had_last_tick = false;
        }
#endif
        CaptureTick(now);
        // ~4x tighter than the shared task's 20ms nominal (and well under
        // what it actually measured, 30-85ms) so a slow iteration here still
        // leaves headroom before a whole 20ms frame is missed. InputData()
        // itself measured 22-64us, so polling this often costs nothing.
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

void VoiceInputController::CaptureTick(uint32_t now)
{
    bool is_recording;
    uint32_t started_ms;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        is_recording = recording_;
        started_ms = recording_started_ms_;
    }
    if (!is_recording) {
        return;
    }

    if (HasExceededMaxDuration(now, started_ms, kMaxRecordingMs)) {
        StopRecordingAndUpload(now);
        return;
    }

    auto* codec = Board::GetInstance().GetAudioCodec();
    if (codec == nullptr) {
        return;
    }
    std::vector<int16_t> frame(kFrameSamples);
#ifdef TACHIKOMA_DEBUG_TIMING
    const int64_t debug_input_data_start_us = esp_timer_get_time();
#endif
    const bool got_frame = codec->InputData(frame);
#ifdef TACHIKOMA_DEBUG_TIMING
    mclog::tagInfo(kTag, "debug_timing InputData_us={} got_frame={}",
                    esp_timer_get_time() - debug_input_data_start_us, got_frame);
#endif
    if (!got_frame) {
        return;  // no new samples available this tick
    }
    // See DownmixToChannel0's declaration: this board's mic is 2-channel
    // (real mic + AEC reference) at the I2S level, but every consumer past
    // this point -- buffer_, the upload's declared "channels=1" -- assumes
    // mono. No-ops when input_channels() is 1.
    frame = DownmixToChannel0(frame, codec->input_channels());

    bool cap_reached = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!recording_) {
            return;  // stopped while we were reading the frame
        }
        const size_t take = ClampToRemainingCapacity(buffer_.size(), frame.size(), kMaxRecordingSamples);
        buffer_.insert(buffer_.end(), frame.begin(), frame.begin() + static_cast<long>(take));
        cap_reached = buffer_.size() >= kMaxRecordingSamples;
    }
    if (cap_reached) {
        StopRecordingAndUpload(now);
    }
}

void VoiceInputController::StopRecordingAndUpload(uint32_t now)
{
    std::vector<int16_t> pcm;
    uint32_t duration_ms = 0;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!recording_) {
            return;
        }
        recording_ = false;
        duration_ms = now - recording_started_ms_;
        pcm = std::move(buffer_);
        buffer_.clear();
    }

    auto* codec = Board::GetInstance().GetAudioCodec();
    if (codec != nullptr) {
        codec->EnableInput(false);
    }
    // Unconditional and unpaired with the pause call in OnButtonPressed()
    // on purpose: this is the one function every recording_=true..false
    // transition passes through (Update()'s two StopRecordingAndUpload()
    // call sites included), so resuming here -- rather than matching each
    // call to a corresponding pause -- can't leave AudioService paused
    // after some early-return path forgets to undo it.
    Application::GetInstance().GetAudioService().SetAudioInputPaused(false);
    tachikoma_state::GetTachikomaStateManager().Notify(tachikoma_state::TachikomaEvent::UserSpeechEnded);

    if (!MeetsMinimumDuration(duration_ms, kMinRecordingMs) || pcm.empty()) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            last_error_ = VoiceInputErrorCode::RecordingTooShort;
        }
        tachikoma_state::GetTachikomaStateManager().Notify(tachikoma_state::TachikomaEvent::AiRequestFailed);
        return;
    }

    const auto config = LoadConfig();
    uint32_t generation = 0;
    bool ready = false;
    VoiceInputErrorCode bail_error = VoiceInputErrorCode::None;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (config.endpoint.empty() || config.device_token.empty()) {
            bail_error = VoiceInputErrorCode::NotConfigured;
        } else if (!ai_gateway::IsNetworkStackReady(esp_netif_get_nr_of_ifs())) {
            bail_error = VoiceInputErrorCode::NetworkUnavailable;
        } else {
            busy_ = true;
            generation = ++generation_;
            ready = true;
        }
        if (!ready) {
            last_error_ = bail_error;
        }
    }
    if (!ready) {
        tachikoma_state::GetTachikomaStateManager().Notify(tachikoma_state::TachikomaEvent::AiRequestFailed);
        return;
    }

    const int raw_rate = (codec != nullptr) ? codec->input_sample_rate() : 0;
    const uint32_t sample_rate = raw_rate > 0 ? static_cast<uint32_t>(raw_rate) : static_cast<uint32_t>(kFallbackSampleRate);

    auto* args = new WorkerArgs{this, config, std::move(pcm), sample_rate, generation};
    const auto result = xTaskCreate(&VoiceInputController::WorkerTask, "voice_input", 8192, args, 2, nullptr);
    if (result != pdPASS) {
        delete args;
        Complete(generation, VoiceInputErrorCode::Internal, {});
    }
}

void VoiceInputController::WorkerTask(void* arg)
{
    auto* args = static_cast<WorkerArgs*>(arg);
    if (args != nullptr && args->controller != nullptr) {
        args->controller->RunWorker(args);
    }
    vTaskDelete(nullptr);
}

void VoiceInputController::RunWorker(WorkerArgs* args)
{
    std::string text;
    VoiceInputErrorCode error = VoiceInputErrorCode::None;
    UploadAndTranscribe(args->config, args->pcm, args->sample_rate, text, error);
    Complete(args->generation, error, text);
    delete args;
}

bool VoiceInputController::UploadAndTranscribe(const VoiceInputConfig& config, const std::vector<int16_t>& pcm,
                                               uint32_t sample_rate, std::string& text, VoiceInputErrorCode& error)
{
    if (config.device_id.empty()) {
        error = VoiceInputErrorCode::Internal;
        return false;
    }
    if (config.endpoint.rfind("http://", 0) == 0) {
#if !defined(DEVELOPMENT_BUILD)
        error = VoiceInputErrorCode::NotConfigured;
        return false;
#else
        mclog::tagWarn(kTag, "HTTP transcribe queue is DEVELOPMENT_BUILD-only; use HTTPS in production");
#endif
    }

    HttpBuffer buffer;
    esp_http_client_config_t http_config = {};
    http_config.url = config.endpoint.c_str();
    http_config.method = HTTP_METHOD_POST;
    http_config.timeout_ms = static_cast<int>(config.response_timeout_ms);
    http_config.event_handler = HttpEvent;
    http_config.user_data = &buffer;
    if (config.endpoint.rfind("https://", 0) == 0) {
        http_config.crt_bundle_attach = esp_crt_bundle_attach;
    }
    esp_http_client_handle_t client = esp_http_client_init(&http_config);
    if (client == nullptr) {
        error = VoiceInputErrorCode::ConnectionFailed;
        return false;
    }

    const std::string auth = "Bearer " + config.device_token;
    const std::string sample_rate_str = std::to_string(sample_rate);
    const std::string content_type = "audio/L16;rate=" + sample_rate_str + ";channels=1";
    esp_http_client_set_header(client, "Authorization", auth.c_str());
    esp_http_client_set_header(client, "X-Device-Id", config.device_id.c_str());
    esp_http_client_set_header(client, "X-Sample-Rate", sample_rate_str.c_str());
    esp_http_client_set_header(client, "Content-Type", content_type.c_str());
    esp_http_client_set_post_field(client, reinterpret_cast<const char*>(pcm.data()),
                                   static_cast<int>(pcm.size() * sizeof(int16_t)));

    const esp_err_t result = esp_http_client_perform(client);
    const int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (result != ESP_OK) {
        error = (result == ESP_ERR_TIMEOUT) ? VoiceInputErrorCode::Timeout : VoiceInputErrorCode::ConnectionFailed;
        return false;
    }
    if (status == 401 || status == 403) {
        error = VoiceInputErrorCode::AuthenticationFailed;
        return false;
    }
    if (status < 200 || status >= 300) {
        error = VoiceInputErrorCode::ServerError;
        return false;
    }

    cJSON* parsed = cJSON_ParseWithLength(buffer.body.data(), buffer.body.size());
    if (parsed == nullptr) {
        error = VoiceInputErrorCode::InvalidResponse;
        return false;
    }
    const auto* text_item = cJSON_GetObjectItemCaseSensitive(parsed, "text");
    if (!cJSON_IsString(text_item) || text_item->valuestring == nullptr || text_item->valuestring[0] == '\0') {
        cJSON_Delete(parsed);
        error = VoiceInputErrorCode::InvalidResponse;
        return false;
    }
    text = text_item->valuestring;
    cJSON_Delete(parsed);
    return true;
}

void VoiceInputController::Complete(uint32_t generation, VoiceInputErrorCode error, const std::string& text)
{
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (generation != generation_) {
            return;
        }
        busy_ = false;
        last_error_ = error;
    }

    if (error == VoiceInputErrorCode::None) {
        mclog::tagInfo(kTag, "transcribed text len={}", text.size());
        if (ai_gateway::GetAiGatewayClient().StartRequest(text)) {
            return;
        }
        mclog::tagWarn(kTag, "AiGatewayClient rejected transcribed text");
    } else {
        mclog::tagWarn(kTag, "voice input upload/transcription failed");
    }
    tachikoma_state::GetTachikomaStateManager().Notify(tachikoma_state::TachikomaEvent::AiRequestFailed);
}

void VoiceInputController::Stop()
{
    std::lock_guard<std::mutex> lock(mutex_);
    ++generation_;
    busy_ = false;
    recording_ = false;
    buffer_.clear();
}

bool VoiceInputController::IsRecording() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return recording_;
}

bool VoiceInputController::IsBusy() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return busy_;
}

VoiceInputErrorCode VoiceInputController::GetLastError() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return last_error_;
}

}  // namespace stackchan::voice_input
