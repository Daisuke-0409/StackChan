/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include "tachikoma_state_types.h"

namespace stackchan::tachikoma_state {

class TachikomaStateMachine {
public:
    TachikomaTransitionResult Initialize(uint32_t now);
    TachikomaTransitionResult HandleEvent(TachikomaEvent event, uint32_t now);
    TachikomaTransitionResult ApplyTimeout(uint32_t now);
    bool IsTimedOut(uint32_t now) const;
    void Stop();

    void SetTimeouts(const TachikomaStateTimeouts& timeouts);
    TachikomaStateSnapshot GetSnapshot() const;

private:
    TachikomaTransitionResult TransitionTo(TachikomaState next, uint32_t now,
                                           TachikomaError error = TachikomaError::None,
                                           bool force_motion_reset = false);
    TachikomaTransitionResult StartReaction(TachikomaReaction reaction, uint32_t now,
                                            TachikomaError error = TachikomaError::None);
    TachikomaTransitionResult FinishReaction(uint32_t now);
    uint32_t TimeoutFor(TachikomaState state) const;
    static TachikomaState SanitizeReactionReturnState(TachikomaState state);

    TachikomaState current_               = TachikomaState::Booting;
    TachikomaState previous_              = TachikomaState::Booting;
    TachikomaState reaction_return_state_ = TachikomaState::Idle;
    TachikomaReaction reaction_           = TachikomaReaction::None;
    TachikomaError error_                 = TachikomaError::None;
    TachikomaStateTimeouts timeouts_;
    uint32_t state_entered_ms_            = 0;
    uint32_t transition_sequence_         = 0;
    bool initialized_                     = false;
    bool running_                         = false;
};

}  // namespace stackchan::tachikoma_state
