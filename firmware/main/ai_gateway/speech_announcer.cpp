/*
 * SPDX-License-Identifier: MIT
 */
#include "speech_announcer.h"

#include <audio_codec.h>
#include <board.h>
#include <cstring>
#include <esp_crt_bundle.h>
#include <esp_http_client.h>
#include <esp_netif.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <mooncake_log.h>
#include <settings.h>
#include <string_view>
#include <vector>

#include "hal/audio_codec_guard.h"
#include "hal/hal.h"
#include "stackchan/motion/tachikoma_motion.h"
#include "stackchan/state/tachikoma_state_manager.h"
#include "stackchan/state/tachikoma_state_types.h"

namespace stackchan::ai_gateway {
namespace {
constexpr std::string_view kTag = "SpeechAnnouncer";
// Sized with headroom over the gateway's own MAX_SPEECH_AUDIO_BYTES
// (720000 bytes, ~15s at 24kHz/16-bit/mono): a real Gemini TTS reply
// routinely runs 300-450 KB, and the old 256 KiB here silently truncated
// the HTTP transfer (esp_http_client_perform -> ESP_ERR_NO_MEM) well under
// what the gateway would even agree to serve. 768 KiB bounds ESP32 heap
// usage for a single fetch, not a tuned production limit.
constexpr size_t kMaxAudioBytes = 768 * 1024;
// Two rates, because one rate cannot serve both jobs. When nothing is
// expected, polling is pure overhead and 2s is plenty. But during a
// conversation turn the gateway is actively pushing this device's reply --
// one enqueue per finished sentence -- and every poll interval is dead air
// the user sits through. Measured before this split: a first sentence
// enqueued at 00:33:51.031 was not fetched until 00:33:57.931, ~7s of
// silence, which made the gateway's sentence-at-a-time streaming pointless.
constexpr uint32_t kIdlePollIntervalMs = 2000;
constexpr uint32_t kActivePollIntervalMs = 200;
constexpr char kSettingsNamespace[] = "tachi_speak";  // NVS namespace <= 15 chars

// The gateway tags each reply's audio with how that reply felt, so the body
// can react at the moment it starts speaking rather than after. Values map to
// TachikomaReaction; anything unrecognized is ignored and the device just
// speaks without moving, which is the safe default.
constexpr char kEmotionHeader[] = "X-Tachikoma-Emotion";

struct HttpBuffer {
    std::string body;
    std::string emotion;
    bool overflowed = false;
};

esp_err_t HttpEvent(esp_http_client_event_t* event)
{
    auto* buffer = static_cast<HttpBuffer*>(event->user_data);
    if (buffer == nullptr) {
        return ESP_OK;
    }
    if (event->event_id == HTTP_EVENT_ON_HEADER && event->header_key != nullptr &&
        event->header_value != nullptr && strcasecmp(event->header_key, kEmotionHeader) == 0) {
        buffer->emotion = event->header_value;
    }
    if (event->event_id == HTTP_EVENT_ON_DATA && event->data != nullptr) {
        if (buffer->body.size() + event->data_len > kMaxAudioBytes) {
            buffer->overflowed = true;
            return ESP_ERR_NO_MEM;
        }
        buffer->body.append(static_cast<const char*>(event->data), event->data_len);
    }
    return ESP_OK;
}

// Plays the motion directly instead of going through HappyRequested /
// ConfusedRequested.
//
// Those events move the state machine into Reacting, which does not work
// while speaking: there is no Reacting+SpeechFinished transition rule, so
// the SpeechFinished fired when playback ends would be rejected and the
// device would sit in Speaking until it timed out into Error. Reacting is
// for a reaction that *is* the whole activity; here the reaction has to ride
// on top of one.
//
// Driving the motion manager directly gives exactly that. A one-shot records
// the loop it interrupted and restores it when done, and
// TachikomaStateManager::CheckOneShotCompletion() only raises
// ReactionFinished when the state actually is Reacting, so completing here
// is silent. The state machine stays in Speaking from start to finish.
void PlayEmotionMotion(const std::string& emotion)
{
    if (emotion.empty()) {
        return;
    }
    if (emotion == "happy") {
        tachikoma_motion::PlayMotion(tachikoma_motion::MotionType::Happy);
    } else if (emotion == "confused" || emotion == "sad") {
        tachikoma_motion::PlayMotion(tachikoma_motion::MotionType::Confused);
    } else {
        mclog::tagWarn(kTag, "unknown emotion header value, ignoring");
    }
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

// esp_netif_init() (and the LWIP tcpip thread it starts) is only ever called
// from StackChanWifiStation::Start(), which itself only runs as part of
// xiaozhi's Application/WifiManager boot path. When the device boots
// straight to the Launcher (startAiAgentOnBoot NVS flag off), that path
// never runs, so no esp_netif exists yet. Calling esp_http_client_perform()
// in that state hard-crashes (LWIP asserts on an uninitialized tcpip mbox)
// instead of failing gracefully, so this must be checked *before* touching
// esp_http_client at all -- a 0-interface count is used as a proxy for "the
// network stack was never brought up", independent of which WiFi flow
// (xiaozhi's or StackChanWifiStation's) would have brought it up.
//
// Takes the interface count as a parameter (rather than calling
// esp_netif_get_nr_of_ifs() internally) so the decision logic is testable
// without touching real ESP-IDF network state.
bool IsNetworkStackReady(size_t interface_count)
{
    return interface_count > 0;
}

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

    // Hold off only while the user is actually speaking -- playing over a
    // live recording would both talk across them and feed the mic.
    //
    // Thinking and Speaking used to be excluded here too, on the reasoning
    // that a pushed announcement should not interrupt a conversation. But
    // the conversation's own reply arrives through this same queue, so that
    // guard delayed every answer until the turn had already finished and
    // fallen back to Idle. Thinking is precisely when the reply is expected,
    // and Speaking is when the next sentence of it should follow the one
    // just played, so both now poll -- at the faster rate.
    const auto state = tachikoma_state::GetTachikomaStateManager().GetCurrentState();
    const bool reply_expected = state == tachikoma_state::TachikomaState::Thinking ||
                                state == tachikoma_state::TachikomaState::Speaking;
    const uint32_t poll_interval = reply_expected ? kActivePollIntervalMs : kIdlePollIntervalMs;
    if (state == tachikoma_state::TachikomaState::Listening) {
        std::lock_guard<std::mutex> lock(mutex_);
        next_poll_ms_ = now + poll_interval;
        return;
    }

    const auto config = LoadConfig();
    uint32_t generation;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        next_poll_ms_ = now + poll_interval;
        if (config.endpoint.empty() || config.device_token.empty()) {
            last_error_ = SpeechAnnounceErrorCode::NotConfigured;
            return;
        }
        if (!IsNetworkStackReady(esp_netif_get_nr_of_ifs())) {
            last_error_ = SpeechAnnounceErrorCode::NetworkUnavailable;
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
    auto& state = tachikoma_state::GetTachikomaStateManager();
    // SpeechStarted first: it sets the Speaking state, and the state change
    // applies that state's loop motion. Playing the emotion after means the
    // one-shot is layered on the loop it should return to.
    state.Notify(tachikoma_state::TachikomaEvent::SpeechStarted);
    PlayEmotionMotion(buffer.emotion);
    mclog::tagInfo(kTag, "playing pushed announcement bytes={} emotion={}", buffer.body.size(),
                    buffer.emotion.empty() ? "none" : buffer.emotion.c_str());
    {
        // Held across the whole EnableOutput()+OutputData() sequence, not
        // just each call individually, so AudioService's idle
        // power-management timer can never interrupt this blocking write
        // partway through. See hal/audio_codec_guard.h.
        std::lock_guard<std::mutex> audio_lock(stackchan::hal::GetAudioCodecMutex());
        if (!codec->output_enabled()) {
            codec->EnableOutput(true);
        }
        codec->OutputData(pcm);
    }
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
