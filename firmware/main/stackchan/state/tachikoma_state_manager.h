/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include "tachikoma_state_machine.h"

#include <array>
#include <cstddef>
#include <mutex>

#ifndef ENABLE_TACHIKOMA_STATE_TEST_SEQUENCE
#define ENABLE_TACHIKOMA_STATE_TEST_SEQUENCE 0
#endif

namespace stackchan::tachikoma_state {

class TachikomaStateManager {
public:
    void Initialize(uint32_t now);
    bool Notify(TachikomaEvent event);
    bool PlayReaction(TachikomaReaction reaction);
    void Update(uint32_t now);
    void Reset();
    void Stop();
    void SetTimeouts(const TachikomaStateTimeouts& timeouts);

    TachikomaState GetCurrentState() const;
    TachikomaState GetPreviousState() const;
    TachikomaStateSnapshot GetSnapshot() const;

private:
    static constexpr size_t kQueueCapacity = 16;
    static constexpr size_t kMaxEventsPerUpdate = 8;

    bool PopEvent(TachikomaEvent& event);
    void ApplyTransition(const TachikomaTransitionResult& result);
    void ApplyStateMotion(const TachikomaStateSnapshot& snapshot, bool force_reset);
    void CheckOneShotCompletion();
    void UpdateDevelopmentTestSequence(uint32_t now);
    static bool IsHighPriorityEvent(TachikomaEvent event);

    mutable std::mutex mutex_;
    TachikomaStateMachine machine_;
    std::array<TachikomaEvent, kQueueCapacity> event_queue_{};
    size_t queue_head_ = 0;
    size_t queue_tail_ = 0;
    size_t queue_count_ = 0;
    uint32_t observed_motion_completion_sequence_ = 0;

#if defined(DEVELOPMENT_BUILD) && ENABLE_TACHIKOMA_STATE_TEST_SEQUENCE
    uint32_t test_started_ms_ = 0;
    uint8_t test_stage_ = 0;
#endif
};

TachikomaStateManager& GetTachikomaStateManager();

}  // namespace stackchan::tachikoma_state
