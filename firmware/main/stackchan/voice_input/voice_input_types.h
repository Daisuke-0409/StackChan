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

struct VoiceInputConfig {
    std::string endpoint;  // full URL to POST recorded PCM to, e.g. "https://host:port/v1/transcribe"
    std::string device_token;
    std::string device_id;
    uint32_t response_timeout_ms = 15000;
};

}  // namespace stackchan::voice_input
