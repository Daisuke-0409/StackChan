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
#include <esp_heap_caps.h>
#include <esp_http_client.h>
#include <esp_netif.h>
#include <esp_timer.h>
#include <new>
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
#include "hal/audio_codec_guard.h"
#include "hal/hal.h"
#include "stackchan/stackchan.h"
#include "stackchan/state/tachikoma_state_manager.h"
#include "stackchan/state/tachikoma_state_types.h"

namespace stackchan::voice_input {
namespace {
constexpr std::string_view kTag = "VoiceInput";
constexpr char kSettingsNamespace[] = "tachi_stt";  // NVS namespace <= 15 chars
// 30s, not the previous 8s: asking the device to look something up takes
// noticeably longer to say than a one-line question, and being cut off
// mid-sentence is worse than a slightly longer wait. Only the ceiling moves
// -- an ordinary short utterance still ends the moment the button is
// released, so this costs nothing in the common case.
constexpr uint32_t kMaxRecordingMs = 30000;
constexpr uint32_t kMinRecordingMs = 300;
// How long after playback ends (Speaking -> Idle) to ignore a new Press.
// Confirmed on real hardware: the speaker's own vibration during Zephyr
// TTS playback reached the Si12T head-touch sensor and was misread as a
// physical touch, immediately re-triggering push-to-talk and producing an
// unbounded reply loop with no user involvement. Tune here if false
// triggers still slip through (louder replies vibrate longer) or if this
// proves longer than necessary once retested.
constexpr uint32_t kPostSpeechCooldownMs = 1500;
// How much audio to ask the codec for per read. This must be derived from the
// mic's real rate and channel count, never assumed: a fixed 320 int16 was the
// bug that made every recording unintelligible. The mic produces
// rate * channels int16 per second (24000 * 2 = 48000 here), while a
// 320-sample read on a 10ms-nominal (18.4ms measured) loop drained only
// ~17,400 -- about 36% of it. The rest overran the 1,440-frame DMA ring
// (6 * AUDIO_CODEC_DMA_FRAME_NUM, ~60ms at 24kHz), so the recording became
// disjoint 320-sample fragments spliced together: roughly 2.8x too fast, with
// the gaps destroying exactly the formant and pitch continuity speech
// recognition depends on. Confirmed by measured_rate=8313..8542 against a
// declared 24000.
constexpr uint32_t kCaptureChunkMs = 20;
constexpr size_t kFallbackFrameSamples = 960;  // 20ms at 24kHz, 2 channels
constexpr size_t kMaxFrameSamples = 4096;
// 30s of 24kHz mono int16 = 1.44 MB. That is far too large for internal RAM
// (~55 KB free), but CONFIG_SPIRAM_USE_MALLOC=y with
// CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL=512 sends any allocation this size to
// the 8 MB PSRAM, and the upload hands esp_http_client a pointer to this
// buffer rather than copying it. The old 128*1024 was expressed as "256 KiB,
// mirrors SpeechAnnouncer's kMaxAudioBytes" -- a playback-side limit that
// never had anything to do with how long someone may speak. It capped
// recordings at 5.5s once capture ran at the correct rate.
// The gateway's MAX_TRANSCRIBE_AUDIO_BYTES must stay >= this in bytes.
constexpr size_t kMaxRecordingSamples = 30 * 24000;  // 30s at 24kHz mono
// Floor for the degraded case: 3s at 24kHz. Below this a recording is not
// worth attempting, so ReserveRecordingBuffer() reports failure instead.
constexpr size_t kMinRecordingCapacitySamples = 3 * 24000;
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

size_t ReserveRecordingBuffer(std::vector<int16_t>& buffer)
{
    // Reserve the ceiling up front rather than letting the vector grow into
    // 1.44 MB: each doubling holds the old and new blocks at once, a >2 MB
    // transient, during a real-time capture loop that cannot afford to stall.
    //
    // But do not assume the allocation succeeds. CONFIG_COMPILER_CXX_EXCEPTIONS
    // is on, so a failed reserve() throws std::bad_alloc, and an uncaught
    // throw here would abort the device on a button press. PSRAM is shared
    // with LVGL and the camera (stackchan_camera.cc takes frame-sized blocks),
    // so a fragmented heap is a realistic way to get there. Ask what is
    // actually available, keep half of it free for everything else, and treat
    // whatever we get as this recording's real capacity.
    size_t wanted = kMaxRecordingSamples;
    const size_t largest_block_samples = heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM) / sizeof(int16_t);
    if (largest_block_samples < wanted * 2) {
        wanted = std::min(wanted, largest_block_samples / 2);
    }
    wanted = std::max(wanted, kMinRecordingCapacitySamples);
    while (wanted >= kMinRecordingCapacitySamples) {
        try {
            buffer.reserve(wanted);
            return buffer.capacity();
        } catch (const std::bad_alloc&) {
            wanted /= 2;
        }
    }
    mclog::tagWarn(kTag, "could not reserve a recording buffer; recording unavailable");
    return 0;
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
    // The head-touch sensor is mounted on the part that moves, so the servos
    // shake it. The speaker already had this problem (see the cooldown
    // above); once gestures grew large enough to be worth watching, the
    // motion did too, and the robot started opening conversations by itself
    // whenever it turned to look at someone -- "顔を見た瞬間話しかけてくる".
    //
    // Recording while the neck is driving would also put servo noise into
    // the clip, so declining here costs nothing: a real press during a
    // gesture is a fraction of a second from being possible again.
    if (GetStackChan().motion().isMoving()) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = VoiceInputErrorCode::Busy;
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
        buffer_.shrink_to_fit();  // release the previous recording's block first
        recording_capacity_samples_ = ReserveRecordingBuffer(buffer_);
        last_error_ = VoiceInputErrorCode::None;
    }

    // Stop xiaozhi's own wake-word/processor task from also reading the
    // mic for the duration of this recording -- see
    // AudioService::SetAudioInputPaused()'s declaration for why both
    // reading at once corrupts the recording instead of erroring cleanly.
    Application::GetInstance().GetAudioService().SetAudioInputPaused(true);

    auto* codec = Board::GetInstance().GetAudioCodec();
    if (codec != nullptr) {
        // The mic's sample rate does not take effect on its own. This board
        // runs I2S in full duplex with TX as the clock master, and
        // esp_codec_dev only propagates a newly opened RX rate to TX while
        // the output is disabled -- see the "TX is master, set to RX not
        // take effect need reconfig TX also" branch in set_fs()
        // (managed_components/espressif__esp_codec_dev/platform/
        // audio_codec_data_i2s.c). Opening the mic with the speaker still
        // enabled silently leaves the capture running at whatever rate the
        // last playback configured, while StopRecordingAndUpload() goes on
        // declaring input_sample_rate() to the STT. That mismatch is what
        // made recordings come back at wildly different speeds -- one clip
        // slow and deep, the next sped up -- and left the audio garbled
        // enough that Gemini returned confident hallucinations instead of a
        // transcription.
        //
        // A plain EnableInput(true) is not enough to fix it either: it
        // returns early when input is already enabled, skipping the
        // esp_codec_dev_open() that would reconfigure anything at all. So
        // drop output, force a close/open cycle, and only then record.
        // The output mutex is required because EnableOutput() is shared
        // with SpeechAnnouncer and AudioService -- see audio_codec_guard.h.
        std::lock_guard<std::mutex> codec_lock(stackchan::hal::GetAudioCodecMutex());
        codec->EnableOutput(false);
        codec->EnableInput(false);
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
        const bool paced_by_codec = CaptureTick(now);
        // Only delay when the codec read did not already block. While
        // recording, esp_codec_dev_read() waits for a full chunk to arrive
        // (i2s_channel_read with a 1s timeout), so it both paces this loop
        // and sleeps the task -- adding a delay on top of that would put the
        // loop's period above the time its own read covers, which is exactly
        // how the previous version fell behind the mic and lost most of the
        // audio. When nothing was read (not recording, or the read failed)
        // there is no such pacing, so yield explicitly.
        //
        // 1 tick, not a smaller pdMS_TO_TICKS(N): CONFIG_FREERTOS_HZ=100
        // (10ms tick) makes pdMS_TO_TICKS(5) truncate to 0 via integer
        // division, which turned this into vTaskDelay(0) -- a
        // same-priority-only yield, not a real block. At priority 8 that
        // starved core 0's idle task and tripped the 10s task watchdog.
        if (!paced_by_codec) {
            vTaskDelay(1);
        }
    }
}

bool VoiceInputController::CaptureTick(uint32_t now)
{
    bool is_recording;
    uint32_t started_ms;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        is_recording = recording_;
        started_ms = recording_started_ms_;
    }
    if (!is_recording) {
        return false;
    }

    if (HasExceededMaxDuration(now, started_ms, kMaxRecordingMs)) {
        StopRecordingAndUpload(now);
        return false;
    }

    auto* codec = Board::GetInstance().GetAudioCodec();
    if (codec == nullptr) {
        return false;
    }
    // Sized from the mic's own rate and channel count so one read covers
    // kCaptureChunkMs of real time -- see kCaptureChunkMs's declaration for
    // what a fixed size cost us.
    const int rate = codec->input_sample_rate();
    const int channels = codec->input_channels();
    size_t frame_samples = kFallbackFrameSamples;
    if (rate > 0 && channels > 0) {
        frame_samples = static_cast<size_t>(rate) * static_cast<size_t>(channels) * kCaptureChunkMs / 1000u;
    }
    frame_samples = std::clamp<size_t>(frame_samples, 160, kMaxFrameSamples);
    std::vector<int16_t> frame(frame_samples);
#ifdef TACHIKOMA_DEBUG_TIMING
    const int64_t debug_input_data_start_us = esp_timer_get_time();
#endif
    const bool got_frame = codec->InputData(frame);
#ifdef TACHIKOMA_DEBUG_TIMING
    mclog::tagInfo(kTag, "debug_timing InputData_us={} got_frame={}",
                    esp_timer_get_time() - debug_input_data_start_us, got_frame);
#endif
    if (!got_frame) {
        return false;  // no new samples available this tick
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
            return true;  // stopped while we were reading the frame
        }
        // recording_capacity_samples_, not kMaxRecordingSamples: growing past
        // what was actually reserved is the reallocation this avoids.
        const size_t capacity = recording_capacity_samples_;
        const size_t take = ClampToRemainingCapacity(buffer_.size(), frame.size(), capacity);
        buffer_.insert(buffer_.end(), frame.begin(), frame.begin() + static_cast<long>(take));
        cap_reached = buffer_.size() >= capacity;
    }
    if (cap_reached) {
        StopRecordingAndUpload(now);
    }
    return true;
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
#ifdef TACHIKOMA_DEBUG_TIMING
    // Samples actually captured divided by the real press-to-release time is
    // the rate the hardware was truly running at. If it does not match the
    // declared rate above, the clip reaches the STT at the wrong speed and
    // no amount of prompt or model tuning will make it intelligible.
    const uint32_t measured_rate =
        duration_ms > 0 ? static_cast<uint32_t>((static_cast<uint64_t>(pcm.size()) * 1000u) / duration_ms) : 0u;
    mclog::tagInfo(kTag, "debug_timing samples={} hold_ms={} declared_rate={} measured_rate={}",
                   pcm.size(), duration_ms, sample_rate, measured_rate);
#endif

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

    const int64_t upload_start_us = esp_timer_get_time();
    const esp_err_t result = esp_http_client_perform(client);
    const int status = esp_http_client_get_status_code(client);
    const int64_t upload_ms = (esp_timer_get_time() - upload_start_us) / 1000;
    esp_http_client_cleanup(client);

    if (result != ESP_OK) {
        error = (result == ESP_ERR_TIMEOUT) ? VoiceInputErrorCode::Timeout : VoiceInputErrorCode::ConnectionFailed;
        // The known "second recording in a row fails" report has been
        // unreproducible since 2026-07-25 partly because nothing recorded
        // *how* it failed -- esp_err, HTTP status and elapsed time all
        // existed here and none of them were logged. A ~5.7s failure looks
        // very different depending on whether it was a refused connection,
        // a TLS stall or a server error, and free PSRAM matters because an
        // upload now holds a 1.44MB buffer while the next recording is
        // already reserving its own.
        mclog::tagWarn(kTag, "upload failed err={} ({}) status={} bytes={} elapsed_ms={} psram_free={}",
                        static_cast<int>(result), esp_err_to_name(result), status,
                        pcm.size() * sizeof(int16_t), static_cast<int>(upload_ms),
                        heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
        return false;
    }
    if (status == 401 || status == 403) {
        error = VoiceInputErrorCode::AuthenticationFailed;
        mclog::tagWarn(kTag, "upload rejected status={} elapsed_ms={}", status, static_cast<int>(upload_ms));
        return false;
    }
    if (status < 200 || status >= 300) {
        error = VoiceInputErrorCode::ServerError;
        mclog::tagWarn(kTag, "upload server error status={} bytes={} elapsed_ms={}",
                        status, pcm.size() * sizeof(int16_t), static_cast<int>(upload_ms));
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
        // Naming the code, not just "failed": these are eleven different
        // problems with eleven different fixes, and the one that matters
        // (the second-recording failure) has stayed unexplained for exactly
        // as long as this line refused to say which one it hit.
        mclog::tagWarn(kTag, "voice input failed: {}", ToString(error));
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
