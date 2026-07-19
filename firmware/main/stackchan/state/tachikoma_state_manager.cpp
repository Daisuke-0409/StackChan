/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "tachikoma_state_manager.h"

#include "tachikoma_state_self_test.h"
#include <esp_log.h>
#include <stackchan/motion/tachikoma_motion.h>

namespace stackchan::tachikoma_state {
namespace {

constexpr const char* kTag = "TachikomaState";

}  // namespace

TachikomaStateManager& GetTachikomaStateManager()
{
    static TachikomaStateManager manager;
    return manager;
}

void TachikomaStateManager::Initialize(uint32_t now)
{
    TachikomaTransitionResult result;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        result = machine_.Initialize(now);
        queue_head_ = 0;
        queue_tail_ = 0;
        queue_count_ = 0;
        observed_motion_completion_sequence_ =
            tachikoma_motion::GetMotionManager().GetState().completion_sequence;
#if defined(DEVELOPMENT_BUILD) && ENABLE_TACHIKOMA_STATE_TEST_SEQUENCE
        test_started_ms_ = now;
        test_stage_ = 0;
#endif
    }

    if (result.state_changed) {
        ESP_LOGI(kTag, "Initialized in %s", ToString(result.current_state));
        ApplyTransition(result);
    }

#if defined(DEVELOPMENT_BUILD)
    static bool self_test_ran = false;
    if (!self_test_ran) {
        self_test_ran = true;
        RunTachikomaStateSelfTest();
    }
#endif
}

bool TachikomaStateManager::Notify(TachikomaEvent event)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto snapshot = machine_.GetSnapshot();
    if (!snapshot.running) {
        return false;
    }

    if (IsHighPriorityEvent(event)) {
        queue_head_ = 0;
        queue_tail_ = 0;
        queue_count_ = 0;
    } else if (queue_count_ == kQueueCapacity) {
        ESP_LOGW(kTag, "Event queue full; rejected %s", ToString(event));
        return false;
    }

    event_queue_[queue_tail_] = event;
    queue_tail_ = (queue_tail_ + 1) % kQueueCapacity;
    ++queue_count_;
    return true;
}

bool TachikomaStateManager::PlayReaction(TachikomaReaction reaction)
{
    switch (reaction) {
        case TachikomaReaction::Happy: return Notify(TachikomaEvent::HappyRequested);
        case TachikomaReaction::Confused: return Notify(TachikomaEvent::ConfusedRequested);
        case TachikomaReaction::None: return false;
    }
    return false;
}

void TachikomaStateManager::Update(uint32_t now)
{
    UpdateDevelopmentTestSequence(now);
    CheckOneShotCompletion();

    TachikomaEvent event;
    size_t processed = 0;
    while (processed < kMaxEventsPerUpdate && PopEvent(event)) {
        TachikomaTransitionResult result;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            result = machine_.HandleEvent(event, now);
        }

        if (result.accepted) {
            if (result.state_changed) {
                ESP_LOGI(kTag, "%s -> %s by %s%s%s", ToString(result.previous_state),
                         ToString(result.current_state), ToString(event),
                         result.reaction != TachikomaReaction::None ? ", reaction=" : "",
                         result.reaction != TachikomaReaction::None ? ToString(result.reaction) : "");
            }
            ApplyTransition(result);
        } else {
            ESP_LOGW(kTag, "Rejected %s in %s", ToString(event), ToString(result.current_state));
        }
        ++processed;
    }

    TachikomaTransitionResult timeout_result;
    bool timed_out = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        timed_out = machine_.IsTimedOut(now);
        if (timed_out) {
            timeout_result = machine_.ApplyTimeout(now);
        }
    }
    if (timed_out && timeout_result.accepted) {
        ESP_LOGW(kTag, "Timeout: %s -> %s", ToString(timeout_result.previous_state),
                 ToString(timeout_result.current_state));
        ApplyTransition(timeout_result);
    }
}

void TachikomaStateManager::Reset()
{
    Notify(TachikomaEvent::Reset);
}

void TachikomaStateManager::Stop()
{
    {
        std::lock_guard<std::mutex> lock(mutex_);
        machine_.Stop();
        queue_head_ = 0;
        queue_tail_ = 0;
        queue_count_ = 0;
    }
    tachikoma_motion::StopMotion();
}

void TachikomaStateManager::SetTimeouts(const TachikomaStateTimeouts& timeouts)
{
    std::lock_guard<std::mutex> lock(mutex_);
    machine_.SetTimeouts(timeouts);
}

TachikomaState TachikomaStateManager::GetCurrentState() const
{
    return GetSnapshot().current_state;
}

TachikomaState TachikomaStateManager::GetPreviousState() const
{
    return GetSnapshot().previous_state;
}

TachikomaStateSnapshot TachikomaStateManager::GetSnapshot() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return machine_.GetSnapshot();
}

bool TachikomaStateManager::PopEvent(TachikomaEvent& event)
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (queue_count_ == 0) {
        return false;
    }
    event = event_queue_[queue_head_];
    queue_head_ = (queue_head_ + 1) % kQueueCapacity;
    --queue_count_;
    return true;
}

void TachikomaStateManager::ApplyTransition(const TachikomaTransitionResult& result)
{
    if (!result.state_changed && !result.force_motion_reset) {
        return;
    }
    ApplyStateMotion(GetSnapshot(), result.force_motion_reset);
}

void TachikomaStateManager::ApplyStateMotion(const TachikomaStateSnapshot& snapshot, bool force_reset)
{
    if (force_reset) {
        tachikoma_motion::StopMotion();
    }

    switch (snapshot.current_state) {
        case TachikomaState::Idle:
            tachikoma_motion::SetMotion(tachikoma_motion::MotionType::Idle);
            break;
        case TachikomaState::Listening:
            tachikoma_motion::SetMotion(tachikoma_motion::MotionType::Listening);
            break;
        case TachikomaState::Thinking:
            tachikoma_motion::SetMotion(tachikoma_motion::MotionType::Thinking);
            break;
        case TachikomaState::Speaking:
            // Phase2 has no Speaking motion yet; Listening is the closest restrained loop fallback.
            tachikoma_motion::SetMotion(tachikoma_motion::MotionType::Listening);
            break;
        case TachikomaState::Reacting:
            if (snapshot.reaction == TachikomaReaction::Happy) {
                tachikoma_motion::PlayMotion(tachikoma_motion::MotionType::Happy);
            } else if (snapshot.reaction == TachikomaReaction::Confused) {
                tachikoma_motion::PlayMotion(tachikoma_motion::MotionType::Confused);
            }
            break;
        case TachikomaState::Booting:
        case TachikomaState::Error:
        case TachikomaState::Sleeping:
            tachikoma_motion::StopMotion();
            break;
    }
}

void TachikomaStateManager::CheckOneShotCompletion()
{
    const auto motion_state = tachikoma_motion::GetMotionManager().GetState();
    if (motion_state.completion_sequence == observed_motion_completion_sequence_) {
        return;
    }
    observed_motion_completion_sequence_ = motion_state.completion_sequence;

    const auto state = GetSnapshot();
    if (state.current_state == TachikomaState::Reacting) {
        Notify(TachikomaEvent::ReactionFinished);
    }
}

void TachikomaStateManager::UpdateDevelopmentTestSequence(uint32_t now)
{
#if defined(DEVELOPMENT_BUILD) && ENABLE_TACHIKOMA_STATE_TEST_SEQUENCE
    const auto snapshot = GetSnapshot();
    if (!snapshot.running) {
        return;
    }
    const uint32_t elapsed = now - test_started_ms_;
    struct TestStep {
        uint32_t at_ms;
        TachikomaEvent event;
    };
    static constexpr TestStep kSteps[] = {
        {1000, TachikomaEvent::InitializationComplete},
        {3500, TachikomaEvent::UserSpeechStarted},
        {6000, TachikomaEvent::UserSpeechEnded},
        {8500, TachikomaEvent::AiResponseReady},
        {11000, TachikomaEvent::SpeechFinished},
        {13500, TachikomaEvent::HappyRequested},
        {17000, TachikomaEvent::ConfusedRequested},
        // Keep Sleeping/Wake outside the Confused one-shot window.
        {28000, TachikomaEvent::SleepRequested},
        {32000, TachikomaEvent::WakeRequested},
    };
    while (test_stage_ < sizeof(kSteps) / sizeof(kSteps[0]) && elapsed >= kSteps[test_stage_].at_ms) {
        ESP_LOGI(kTag, "Development test event: %s", ToString(kSteps[test_stage_].event));
        Notify(kSteps[test_stage_].event);
        ++test_stage_;
    }
#else
    (void)now;
#endif
}

bool TachikomaStateManager::IsHighPriorityEvent(TachikomaEvent event)
{
    return event == TachikomaEvent::Reset || event == TachikomaEvent::CriticalError ||
           event == TachikomaEvent::EmergencyStop;
}

}  // namespace stackchan::tachikoma_state
