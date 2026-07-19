/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "tachikoma_state_machine.h"

#include <array>

namespace stackchan::tachikoma_state {
namespace {

struct TransitionRule {
    TachikomaState from;
    TachikomaEvent event;
    TachikomaState to;
};

constexpr std::array<TransitionRule, 19> kTransitionRules = {{
    {TachikomaState::Booting, TachikomaEvent::InitializationComplete, TachikomaState::Idle},
    {TachikomaState::Idle, TachikomaEvent::UserSpeechStarted, TachikomaState::Listening},
    {TachikomaState::Idle, TachikomaEvent::RecognitionStarted, TachikomaState::Thinking},
    {TachikomaState::Idle, TachikomaEvent::AiRequestStarted, TachikomaState::Thinking},
    // Added for Phase 5 pushed-audio announcements (e.g. SpeechAnnouncer):
    // a PC-initiated announcement can arrive while the device is Idle, not
    // only mid-conversation.
    {TachikomaState::Idle, TachikomaEvent::SpeechStarted, TachikomaState::Speaking},
    {TachikomaState::Listening, TachikomaEvent::UserSpeechEnded, TachikomaState::Thinking},
    {TachikomaState::Listening, TachikomaEvent::RecognitionSucceeded, TachikomaState::Thinking},
    {TachikomaState::Listening, TachikomaEvent::AiResponseReady, TachikomaState::Speaking},
    {TachikomaState::Listening, TachikomaEvent::SpeechFinished, TachikomaState::Idle},
    {TachikomaState::Thinking, TachikomaEvent::AiResponseReady, TachikomaState::Speaking},
    {TachikomaState::Thinking, TachikomaEvent::SpeechStarted, TachikomaState::Speaking},
    {TachikomaState::Speaking, TachikomaEvent::SpeechFinished, TachikomaState::Idle},
    {TachikomaState::Speaking, TachikomaEvent::UserSpeechStarted, TachikomaState::Listening},
    {TachikomaState::Idle, TachikomaEvent::SleepRequested, TachikomaState::Sleeping},
    {TachikomaState::Sleeping, TachikomaEvent::WakeRequested, TachikomaState::Idle},
    {TachikomaState::Error, TachikomaEvent::Recover, TachikomaState::Idle},
    {TachikomaState::Listening, TachikomaEvent::RecognitionStarted, TachikomaState::Listening},
    {TachikomaState::Thinking, TachikomaEvent::AiRequestStarted, TachikomaState::Thinking},
    {TachikomaState::Speaking, TachikomaEvent::SpeechStarted, TachikomaState::Speaking},
}};

}  // namespace

const char* ToString(TachikomaState state)
{
    switch (state) {
        case TachikomaState::Booting: return "Booting";
        case TachikomaState::Idle: return "Idle";
        case TachikomaState::Listening: return "Listening";
        case TachikomaState::Thinking: return "Thinking";
        case TachikomaState::Speaking: return "Speaking";
        case TachikomaState::Reacting: return "Reacting";
        case TachikomaState::Error: return "Error";
        case TachikomaState::Sleeping: return "Sleeping";
    }
    return "UnknownState";
}

const char* ToString(TachikomaReaction reaction)
{
    switch (reaction) {
        case TachikomaReaction::None: return "None";
        case TachikomaReaction::Happy: return "Happy";
        case TachikomaReaction::Confused: return "Confused";
    }
    return "UnknownReaction";
}

const char* ToString(TachikomaEvent event)
{
    switch (event) {
        case TachikomaEvent::InitializationComplete: return "InitializationComplete";
        case TachikomaEvent::UserSpeechStarted: return "UserSpeechStarted";
        case TachikomaEvent::UserSpeechEnded: return "UserSpeechEnded";
        case TachikomaEvent::RecognitionStarted: return "RecognitionStarted";
        case TachikomaEvent::RecognitionSucceeded: return "RecognitionSucceeded";
        case TachikomaEvent::RecognitionFailed: return "RecognitionFailed";
        case TachikomaEvent::AiRequestStarted: return "AiRequestStarted";
        case TachikomaEvent::AiResponseReady: return "AiResponseReady";
        case TachikomaEvent::AiRequestFailed: return "AiRequestFailed";
        case TachikomaEvent::SpeechStarted: return "SpeechStarted";
        case TachikomaEvent::SpeechFinished: return "SpeechFinished";
        case TachikomaEvent::HappyRequested: return "HappyRequested";
        case TachikomaEvent::ConfusedRequested: return "ConfusedRequested";
        case TachikomaEvent::SleepRequested: return "SleepRequested";
        case TachikomaEvent::WakeRequested: return "WakeRequested";
        case TachikomaEvent::Timeout: return "Timeout";
        case TachikomaEvent::Recover: return "Recover";
        case TachikomaEvent::Reset: return "Reset";
        case TachikomaEvent::ReactionFinished: return "ReactionFinished";
        case TachikomaEvent::CommunicationError: return "CommunicationError";
        case TachikomaEvent::CriticalError: return "CriticalError";
        case TachikomaEvent::EmergencyStop: return "EmergencyStop";
    }
    return "UnknownEvent";
}

const char* ToString(TachikomaError error)
{
    switch (error) {
        case TachikomaError::None: return "None";
        case TachikomaError::RecognitionFailed: return "RecognitionFailed";
        case TachikomaError::AiRequestFailed: return "AiRequestFailed";
        case TachikomaError::CommunicationError: return "CommunicationError";
        case TachikomaError::StateTimeout: return "StateTimeout";
        case TachikomaError::CriticalError: return "CriticalError";
        case TachikomaError::EmergencyStop: return "EmergencyStop";
    }
    return "UnknownError";
}

TachikomaTransitionResult TachikomaStateMachine::Initialize(uint32_t now)
{
    if (initialized_ && running_) {
        return {.accepted = true,
                .previous_state = current_,
                .current_state = current_,
                .reaction = reaction_,
                .error = error_};
    }

    current_               = TachikomaState::Booting;
    previous_              = TachikomaState::Booting;
    reaction_return_state_ = TachikomaState::Idle;
    reaction_              = TachikomaReaction::None;
    error_                 = TachikomaError::None;
    state_entered_ms_      = now;
    initialized_           = true;
    running_               = true;
    ++transition_sequence_;
    return {.accepted = true,
            .state_changed = true,
            .force_motion_reset = true,
            .previous_state = TachikomaState::Booting,
            .current_state = current_};
}

TachikomaTransitionResult TachikomaStateMachine::HandleEvent(TachikomaEvent event, uint32_t now)
{
    if (!initialized_ || !running_) {
        return {};
    }

    if (event == TachikomaEvent::Reset) {
        reaction_ = TachikomaReaction::None;
        return TransitionTo(TachikomaState::Idle, now, TachikomaError::None, true);
    }
    if (event == TachikomaEvent::CriticalError) {
        reaction_ = TachikomaReaction::None;
        return TransitionTo(TachikomaState::Error, now, TachikomaError::CriticalError, true);
    }
    if (event == TachikomaEvent::EmergencyStop) {
        reaction_ = TachikomaReaction::None;
        return TransitionTo(TachikomaState::Error, now, TachikomaError::EmergencyStop, true);
    }
    if (event == TachikomaEvent::CommunicationError) {
        reaction_ = TachikomaReaction::None;
        return TransitionTo(TachikomaState::Error, now, TachikomaError::CommunicationError, true);
    }
    if (event == TachikomaEvent::AiRequestFailed) {
        reaction_ = TachikomaReaction::None;
        return TransitionTo(TachikomaState::Error, now, TachikomaError::AiRequestFailed, true);
    }
    if (event == TachikomaEvent::HappyRequested) {
        return StartReaction(TachikomaReaction::Happy, now);
    }
    if (event == TachikomaEvent::ConfusedRequested) {
        return StartReaction(TachikomaReaction::Confused, now);
    }
    if (event == TachikomaEvent::RecognitionFailed) {
        return StartReaction(TachikomaReaction::Confused, now, TachikomaError::RecognitionFailed);
    }
    if (event == TachikomaEvent::ReactionFinished) {
        return FinishReaction(now);
    }
    if (event == TachikomaEvent::Timeout) {
        return ApplyTimeout(now);
    }

    for (const auto& rule : kTransitionRules) {
        if (rule.from == current_ && rule.event == event) {
            return TransitionTo(rule.to, now);
        }
    }

    if ((current_ == TachikomaState::Idle && event == TachikomaEvent::WakeRequested) ||
        (current_ == TachikomaState::Sleeping && event == TachikomaEvent::SleepRequested)) {
        return {.accepted = true,
                .previous_state = current_,
                .current_state = current_,
                .reaction = reaction_,
                .error = error_};
    }

    return {.previous_state = current_, .current_state = current_, .reaction = reaction_, .error = error_};
}

TachikomaTransitionResult TachikomaStateMachine::ApplyTimeout(uint32_t now)
{
    if (!IsTimedOut(now)) {
        return {.previous_state = current_, .current_state = current_, .reaction = reaction_, .error = error_};
    }

    const bool was_reacting = current_ == TachikomaState::Reacting;
    reaction_ = TachikomaReaction::None;
    switch (current_) {
        case TachikomaState::Thinking:
            return TransitionTo(TachikomaState::Error, now, TachikomaError::StateTimeout, true);
        case TachikomaState::Booting:
        case TachikomaState::Listening:
        case TachikomaState::Speaking:
        case TachikomaState::Reacting:
        case TachikomaState::Error:
            return TransitionTo(TachikomaState::Idle, now, TachikomaError::StateTimeout, was_reacting);
        case TachikomaState::Idle:
        case TachikomaState::Sleeping:
            break;
    }
    return {.previous_state = current_, .current_state = current_, .reaction = reaction_, .error = error_};
}

bool TachikomaStateMachine::IsTimedOut(uint32_t now) const
{
    if (!initialized_ || !running_) {
        return false;
    }
    const uint32_t timeout_ms = TimeoutFor(current_);
    return timeout_ms != 0 && now - state_entered_ms_ >= timeout_ms;
}

void TachikomaStateMachine::Stop()
{
    running_ = false;
}

void TachikomaStateMachine::SetTimeouts(const TachikomaStateTimeouts& timeouts)
{
    timeouts_ = timeouts;
}

TachikomaStateSnapshot TachikomaStateMachine::GetSnapshot() const
{
    return {.current_state = current_,
            .previous_state = previous_,
            .reaction_return_state = reaction_return_state_,
            .reaction = reaction_,
            .error = error_,
            .state_entered_ms = state_entered_ms_,
            .transition_sequence = transition_sequence_,
            .initialized = initialized_,
            .running = running_};
}

TachikomaTransitionResult TachikomaStateMachine::TransitionTo(TachikomaState next, uint32_t now,
                                                              TachikomaError error, bool force_motion_reset)
{
    TachikomaTransitionResult result = {.accepted = true,
                                        .force_motion_reset = force_motion_reset,
                                        .previous_state = current_,
                                        .current_state = next,
                                        .reaction = reaction_,
                                        .error = error};
    error_ = error;
    if (current_ == next && !force_motion_reset) {
        return result;
    }

    previous_         = current_;
    current_          = next;
    state_entered_ms_ = now;
    result.state_changed = true;
    ++transition_sequence_;
    return result;
}

TachikomaTransitionResult TachikomaStateMachine::StartReaction(TachikomaReaction reaction, uint32_t now,
                                                               TachikomaError error)
{
    if (reaction == TachikomaReaction::None || current_ == TachikomaState::Booting ||
        current_ == TachikomaState::Sleeping || current_ == TachikomaState::Error ||
        current_ == TachikomaState::Reacting) {
        return {.previous_state = current_, .current_state = current_, .reaction = reaction_, .error = error_};
    }

    reaction_return_state_ = SanitizeReactionReturnState(current_);
    reaction_ = reaction;
    return TransitionTo(TachikomaState::Reacting, now, error);
}

TachikomaTransitionResult TachikomaStateMachine::FinishReaction(uint32_t now)
{
    if (current_ != TachikomaState::Reacting || reaction_ == TachikomaReaction::None) {
        return {.previous_state = current_, .current_state = current_, .reaction = reaction_, .error = error_};
    }

    const auto completed_reaction = reaction_;
    reaction_ = TachikomaReaction::None;
    auto result = TransitionTo(SanitizeReactionReturnState(reaction_return_state_), now);
    result.reaction = completed_reaction;
    return result;
}

uint32_t TachikomaStateMachine::TimeoutFor(TachikomaState state) const
{
    switch (state) {
        case TachikomaState::Booting: return timeouts_.booting_ms;
        case TachikomaState::Listening: return timeouts_.listening_ms;
        case TachikomaState::Thinking: return timeouts_.thinking_ms;
        case TachikomaState::Speaking: return timeouts_.speaking_ms;
        case TachikomaState::Error: return timeouts_.error_ms;
        case TachikomaState::Idle:
        case TachikomaState::Reacting:
        case TachikomaState::Sleeping:
            return 0;
    }
    return 0;
}

TachikomaState TachikomaStateMachine::SanitizeReactionReturnState(TachikomaState state)
{
    switch (state) {
        case TachikomaState::Idle:
        case TachikomaState::Listening:
        case TachikomaState::Thinking:
        case TachikomaState::Speaking:
            return state;
        default:
            return TachikomaState::Idle;
    }
}

}  // namespace stackchan::tachikoma_state
