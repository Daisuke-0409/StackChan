/*
 * SPDX-License-Identifier: MIT
 */
#include "speech_announcer.h"

#include <audio_codec.h>
#include <board.h>
#include <cstring>
#include <esp_crt_bundle.h>
#include <esp_http_client.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <mooncake_log.h>
#include <settings.h>
#include <string_view>
#include <vector>

#include "hal/hal.h"
#include "stackchan/state/tachikoma_state_manager.h"
#include "stackchan/state/tachikoma_state_types.h"

namespace stackchan::ai_gateway {
namespace {
constexpr std::string_view kTag = "SpeechAnnouncer";
// 256 KiB is generous headroom for a spoken phrase at 24kHz/16-bit/mono
// (roughly 5.5s); this bounds ESP32 heap usage for a single fetch, not a
// tuned production limit.
constexpr size_t kMaxAudioBytes = 256 * 1024;
constexpr uint32_t kPollIntervalMs = 2000;
constexpr char kSettingsNamespace[] = "tachi_speak";  // NVS namespace <= 15 chars

struct HttpBuffer {
    std::string body;
    bool overflowed = false;
};

esp_err_t HttpEvent(esp_http_client_event_t* event)
{
    auto* buffer = static_cast<HttpBuffer*>(event->user_data);
    if (event->event_id == HTTP_EVENT_ON_DATA && buffer != nullptr && event->data != nullptr) {
        if (buffer->body.size() + event->data_len > kMaxAudioBytes) {
            buffer->overflowed = true;
            return ESP_ERR_NO_MEM;
        }
        buffer->body.append(static_cast<const char*>(event->data), event->data_len);
    }
    return ESP_OK;
}

// Percent-encodes a device_id for safe use in a URL query string. The
// default device_id is a plain hex MAC string, but a caller-supplied id is
// treated as untrusted input here.
std::string UrlEncode(const std::string& value)
{
    static constexpr char kHex[] = "0123456789ABCDEF";
    std::string out;
    out.reserve(value.size());
    for (unsigned char c : value) {
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_' ||
            c == '.' || c == '~') {
            out.push_back(static_cast<char>(c));
        } else {
            out.push_back('%');
            out.push_back(kHex[(c >> 4) & 0x0F]);
            out.push_back(kHex[c & 0x0F]);
        }
    }
    return out;
}

}  // namespace

SpeechAnnouncer& GetSpeechAnnouncer()
{
    static SpeechAnnouncer announcer;
    return announcer;
}

SpeechQueueConfig SpeechAnnouncer::LoadConfig() const
{
    Settings settings(kSettingsNamespace);
    SpeechQueueConfig config;
    config.endpoint = settings.GetString("url");
    config.device_token = settings.GetString("device_token");
    config.device_id = settings.GetString("device_id", GetHAL().getFactoryMacString(""));
    return config;
}

bool SpeechAnnouncer::ConfigureSpeechQueue(const std::string& endpoint, const std::string& device_token,
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

void SpeechAnnouncer::Update(uint32_t now)
{
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (busy_) {
            return;
        }
        if (static_cast<int32_t>(now - next_poll_ms_) < 0) {
            return;
        }
    }

    // Never fetch/play a pushed announcement while mid-conversation; leave
    // it queued server-side and try again on a later, idle poll.
    if (tachikoma_state::GetTachikomaStateManager().GetCurrentState() != tachikoma_state::TachikomaState::Idle) {
        std::lock_guard<std::mutex> lock(mutex_);
        next_poll_ms_ = now + kPollIntervalMs;
        return;
    }

    const auto config = LoadConfig();
    uint32_t generation;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        next_poll_ms_ = now + kPollIntervalMs;
        if (config.endpoint.empty() || config.device_token.empty()) {
            last_error_ = SpeechAnnounceErrorCode::NotConfigured;
            return;
        }
        busy_ = true;
        generation = ++generation_;
    }

    auto* args = new WorkerArgs{this, config, generation};
    const auto result = xTaskCreate(&SpeechAnnouncer::WorkerTask, "speech_announcer", 8192, args, 2, nullptr);
    if (result != pdPASS) {
        delete args;
        Complete(generation, SpeechAnnounceErrorCode::Internal);
    }
}

void SpeechAnnouncer::WorkerTask(void* arg)
{
    auto* args = static_cast<WorkerArgs*>(arg);
    if (args != nullptr && args->announcer != nullptr) {
        args->announcer->RunWorker(args);
    }
    vTaskDelete(nullptr);
}

void SpeechAnnouncer::RunWorker(WorkerArgs* args)
{
    bool had_audio = false;
    SpeechAnnounceErrorCode error = SpeechAnnounceErrorCode::None;
    FetchAndPlay(args->config, had_audio, error);
    Complete(args->generation, error);
    delete args;
}

bool SpeechAnnouncer::FetchAndPlay(const SpeechQueueConfig& config, bool& had_audio, SpeechAnnounceErrorCode& error)
{
    had_audio = false;
    if (config.device_id.empty()) {
        error = SpeechAnnounceErrorCode::InvalidDeviceId;
        return false;
    }

    if (config.endpoint.rfind("http://", 0) == 0) {
#if !defined(DEVELOPMENT_BUILD)
        error = SpeechAnnounceErrorCode::NotConfigured;
        return false;
#else
        mclog::tagWarn(kTag, "HTTP speech queue is DEVELOPMENT_BUILD-only; use HTTPS in production");
#endif
    }

    const std::string url = config.endpoint + "?device_id=" + UrlEncode(config.device_id);

    HttpBuffer buffer;
    esp_http_client_config_t http_config = {};
    http_config.url = url.c_str();
    http_config.method = HTTP_METHOD_GET;
    http_config.timeout_ms = static_cast<int>(config.response_timeout_ms);
    http_config.event_handler = HttpEvent;
    http_config.user_data = &buffer;
    if (config.endpoint.rfind("https://", 0) == 0) {
        http_config.crt_bundle_attach = esp_crt_bundle_attach;
    }
    esp_http_client_handle_t client = esp_http_client_init(&http_config);
    if (client == nullptr) {
        error = SpeechAnnounceErrorCode::ConnectionFailed;
        return false;
    }
    if (!config.device_token.empty()) {
        std::string auth = "Bearer " + config.device_token;
        esp_http_client_set_header(client, "Authorization", auth.c_str());
    }
    const esp_err_t result = esp_http_client_perform(client);
    const int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (result != ESP_OK) {
        error = (result == ESP_ERR_TIMEOUT) ? SpeechAnnounceErrorCode::Timeout
                                             : SpeechAnnounceErrorCode::ConnectionFailed;
        return false;
    }
    if (buffer.overflowed) {
        error = SpeechAnnounceErrorCode::InvalidResponse;
        return false;
    }
    if (status == 204) {
        // Nothing pending; not an error.
        return true;
    }
    if (status == 401 || status == 403) {
        error = SpeechAnnounceErrorCode::AuthenticationFailed;
        return false;
    }
    if (status < 200 || status >= 300) {
        error = SpeechAnnounceErrorCode::ServerError;
        return false;
    }
    if (buffer.body.empty() || buffer.body.size() % 2 != 0) {
        // Odd-length or empty body on a 2xx response can't be valid 16-bit
        // PCM; refuse to play possibly-truncated audio.
        error = SpeechAnnounceErrorCode::InvalidResponse;
        return false;
    }

    auto* codec = Board::GetInstance().GetAudioCodec();
    if (codec == nullptr) {
        error = SpeechAnnounceErrorCode::Internal;
        return false;
    }

    std::vector<int16_t> pcm(buffer.body.size() / 2);
    std::memcpy(pcm.data(), buffer.body.data(), buffer.body.size());

    // NOTE: this writes straight to AudioCodec, bypassing AudioService's
    // idle power-management timer (see AudioService::PlaySound). Once
    // enabled here, output stays on rather than being power-cycled down.
    // Fine for short, infrequent announcements; revisit (e.g. route through
    // AudioService instead) if this is ever extended to long or continuous
    // audio output.
    if (!codec->output_enabled()) {
        codec->EnableOutput(true);
    }
    auto& state = tachikoma_state::GetTachikomaStateManager();
    state.Notify(tachikoma_state::TachikomaEvent::SpeechStarted);
    mclog::tagInfo(kTag, "playing pushed announcement bytes={}", buffer.body.size());
    codec->OutputData(pcm);
    state.Notify(tachikoma_state::TachikomaEvent::SpeechFinished);
    had_audio = true;
    return true;
}

void SpeechAnnouncer::Complete(uint32_t generation, SpeechAnnounceErrorCode error)
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (generation != generation_) {
        return;
    }
    busy_ = false;
    last_error_ = error;
    if (error != SpeechAnnounceErrorCode::None && error != SpeechAnnounceErrorCode::NotConfigured) {
        mclog::tagWarn(kTag, "speech queue poll failed");
    }
}

void SpeechAnnouncer::Stop()
{
    std::lock_guard<std::mutex> lock(mutex_);
    ++generation_;
    busy_ = false;
}

bool SpeechAnnouncer::IsBusy() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return busy_;
}

SpeechAnnounceErrorCode SpeechAnnouncer::GetLastError() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return last_error_;
}

}  // namespace stackchan::ai_gateway
