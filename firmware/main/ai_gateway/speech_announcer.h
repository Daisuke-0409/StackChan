/*
 * SPDX-License-Identifier: MIT
 *
 * Polls the Tachikoma Gateway's speech queue for audio pushed from the PC
 * side (e.g. a spoken approval announcement decided outside the device) and
 * plays it on the device speaker. This is intentionally separate from
 * AiGatewayClient's chat request/response flow: polling here is
 * device-initiated, but the content being delivered is PC-initiated -- the
 * device did not ask a question this is an answer to.
 *
 * Polling only proceeds while the state machine is Idle, so a pushed
 * announcement never interrupts an active conversation; if nothing is
 * fetched, the audio simply remains queued server-side for a later poll.
 *
 * Playback writes straight to AudioCodec::OutputData(), bypassing
 * AudioService's idle power-management timer -- output stays on once
 * enabled rather than being power-cycled down. That's fine for short,
 * infrequent announcements; revisit for long or continuous audio output.
 */
#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>

namespace stackchan::ai_gateway {

enum class SpeechAnnounceErrorCode : uint8_t {
    None,
    NotConfigured,
    NetworkUnavailable,
    InvalidDeviceId,
    ConnectionFailed,
    Timeout,
    AuthenticationFailed,
    ServerError,
    InvalidResponse,
    Internal,
};

struct SpeechQueueConfig {
    std::string endpoint;  // full URL to GET, e.g. "https://host:port/v1/speak_queue"
    std::string device_token;
    std::string device_id;
    uint32_t response_timeout_ms = 15000;
};

class SpeechAnnouncer {
public:
    // Stores endpoint/token in a dedicated NVS namespace. Values are never logged.
    bool ConfigureSpeechQueue(const std::string& endpoint, const std::string& device_token,
                              const std::string& device_id = {});
    void Update(uint32_t now);
    void Stop();

    bool IsBusy() const;
    SpeechAnnounceErrorCode GetLastError() const;

private:
    struct WorkerArgs {
        SpeechAnnouncer* announcer;
        SpeechQueueConfig config;
        uint32_t generation;
    };

    static void WorkerTask(void* arg);
    void RunWorker(WorkerArgs* args);
    bool FetchAndPlay(const SpeechQueueConfig& config, bool& had_audio, SpeechAnnounceErrorCode& error);
    void Complete(uint32_t generation, SpeechAnnounceErrorCode error);
    SpeechQueueConfig LoadConfig() const;

    mutable std::mutex mutex_;
    bool busy_ = false;
    uint32_t generation_ = 0;
    uint32_t next_poll_ms_ = 0;
    SpeechAnnounceErrorCode last_error_ = SpeechAnnounceErrorCode::None;
};

SpeechAnnouncer& GetSpeechAnnouncer();

// Exposed for unit testing. See the definition in speech_announcer.cpp for
// why a 0 interface count is treated as "network stack not ready".
bool IsNetworkStackReady(size_t interface_count);

}  // namespace stackchan::ai_gateway
