/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <cstdint>

namespace stackchan::tachikoma_state {

enum class TachikomaState : uint8_t {
    Booting,
    Idle,
    Listening,
    Thinking,
    Speaking,
    Reacting,
    Error,
    Sleeping,
};

enum class TachikomaReaction : uint8_t {
    None,
    Happy,
    Confused,
};

enum class TachikomaEvent : uint8_t {
    InitializationComplete,
    UserSpeechStarted,
    UserSpeechEnded,
    RecognitionStarted,
    RecognitionSucceeded,
    RecognitionFailed,
    AiRequestStarted,
    AiResponseReady,
    AiRequestFailed,
    SpeechStarted,
    SpeechFinished,
    HappyRequested,
    ConfusedRequested,
    SleepRequested,
    WakeRequested,
    Timeout,
    Recover,
    Reset,
    ReactionFinished,
    CommunicationError,
    CriticalError,
    EmergencyStop,
};

enum class TachikomaError : uint8_t {
    None,
    RecognitionFailed,
    AiRequestFailed,
    CommunicationError,
    StateTimeout,
    CriticalError,
    EmergencyStop,
};

struct TachikomaStateTimeouts {
    uint32_t booting_ms   = 10000;
    uint32_t listening_ms = 30000;
    uint32_t thinking_ms  = 60000;
    uint32_t speaking_ms  = 120000;
    uint32_t error_ms     = 4000;
};

struct TachikomaStateSnapshot {
    TachikomaState current_state     = TachikomaState::Booting;
    TachikomaState previous_state    = TachikomaState::Booting;
    TachikomaState reaction_return_state = TachikomaState::Idle;
    TachikomaReaction reaction       = TachikomaReaction::None;
    TachikomaError error             = TachikomaError::None;
    uint32_t state_entered_ms        = 0;
    uint32_t transition_sequence     = 0;
    bool initialized                 = false;
    bool running                     = false;
};

struct TachikomaTransitionResult {
    bool accepted                    = false;
    bool state_changed               = false;
    bool force_motion_reset          = false;
    TachikomaState previous_state    = TachikomaState::Booting;
    TachikomaState current_state     = TachikomaState::Booting;
    TachikomaReaction reaction       = TachikomaReaction::None;
    TachikomaError error             = TachikomaError::None;
};

const char* ToString(TachikomaState state);
const char* ToString(TachikomaReaction reaction);
const char* ToString(TachikomaEvent event);
const char* ToString(TachikomaError error);

}  // namespace stackchan::tachikoma_state
