/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <cstdint>
#include <string>

namespace stackchan::voice_input {

enum class VoiceInputErrorCode : uint8_t {
    None,
    NotConfigured,
    Busy,
    Cooldown,
    RecordingTooShort,
    NetworkUnavailable,
    ConnectionFailed,
    Timeout,
    AuthenticationFailed,
    ServerError,
    InvalidResponse,
    Internal,
};

// Names the failure so a log line can say which of these eleven it was.
// Kept beside the enum so adding a code without naming it is a compile
// error rather than a silently unlabelled log.
inline const char* ToString(VoiceInputErrorCode error)
{
    switch (error) {
        case VoiceInputErrorCode::None: return "none";
        case VoiceInputErrorCode::NotConfigured: return "not_configured";
        case VoiceInputErrorCode::Busy: return "busy";
        case VoiceInputErrorCode::Cooldown: return "cooldown";
        case VoiceInputErrorCode::RecordingTooShort: return "recording_too_short";
        case VoiceInputErrorCode::NetworkUnavailable: return "network_unavailable";
        case VoiceInputErrorCode::ConnectionFailed: return "connection_failed";
        case VoiceInputErrorCode::Timeout: return "timeout";
        case VoiceInputErrorCode::AuthenticationFailed: return "authentication_failed";
        case VoiceInputErrorCode::ServerError: return "server_error";
        case VoiceInputErrorCode::InvalidResponse: return "invalid_response";
        case VoiceInputErrorCode::Internal: return "internal";
    }
    return "unknown";
}


struct VoiceInputConfig {
    std::string endpoint;  // full URL to POST recorded PCM to, e.g. "https://host:port/v1/transcribe"
    std::string device_token;
    std::string device_id;
    // Covers the worst case this endpoint now has to carry: uploading a
    // 30s recording (1.44 MB) and waiting out the STT call on it. 15s was
    // sized for 5.5s clips and would time out on a long question.
    uint32_t response_timeout_ms = 45000;
};

}  // namespace stackchan::voice_input
