/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include "motion.h"
#include "tachikoma_motion_library.h"
#include "tachikoma_motion_manager.h"
#include "tachikoma_motion_types.h"
#include <cstdint>

#ifndef ENABLE_TACHIKOMA_MOTION_TEST_SEQUENCE
#define ENABLE_TACHIKOMA_MOTION_TEST_SEQUENCE 0
#endif

namespace stackchan::tachikoma_motion {

class MotionController {
public:
    MotionFrame Update(uint32_t now);

private:
    void BeginMotion(const MotionState& state, uint32_t now);
    void BeginStep(const MotionState& state, size_t step_index, uint32_t now, bool switching_motion);
    void AdvanceStep(uint32_t now);
    void SetTarget(const MotionState& state, const ResolvedMotionStep& target, uint32_t duration_ms,
                   uint32_t hold_ms, uint32_t now);
    void UpdateDevelopmentTestSequence(uint32_t now);

    const MotionPattern* pattern_ = nullptr;
    size_t step_index_            = 0;
    uint32_t observed_generation_ = UINT32_MAX;
    MotionFrame frame_;
    MotionFrame start_frame_;
    MotionFrame target_frame_;
    uint32_t phase_start_ms_       = 0;
    uint32_t phase_duration_ms_    = 0;
    uint32_t phase_hold_ms_        = 0;
    uint32_t last_display_tick_ms_ = 0;

#if defined(DEVELOPMENT_BUILD) && ENABLE_TACHIKOMA_MOTION_TEST_SEQUENCE
    enum class TestStage : uint8_t {
        Idle,
        Listening,
        Thinking,
        Happy,
        PostHappyIdle,
        Confused,
        Complete,
    };
    TestStage test_stage_         = TestStage::Idle;
    uint32_t test_stage_start_ms_ = 0;
#endif
};

class ServoMotion {
public:
    void Apply(const MotionFrame& frame, stackchan::motion::Motion& motion);

private:
    uint32_t last_command_sequence_ = 0;
};

MotionController& GetMotionController();
ServoMotion& GetServoMotion();

void SetMotion(MotionType motion);
void PlayMotion(MotionType motion);
void StopMotion();
MotionType GetCurrentMotion();
bool IsMotionPlaying();

}  // namespace stackchan::tachikoma_motion
