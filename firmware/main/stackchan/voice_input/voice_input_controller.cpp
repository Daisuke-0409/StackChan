/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "voice_input_controller.h"

#include <algorithm>
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
constexpr size_t kFrameSamples = 320;  // ~20ms at 16kHz mono, matched to the stackchan update tick
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
    if (!codec->InputData(frame)) {
        return;  // no new samples available this tick
    }

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
