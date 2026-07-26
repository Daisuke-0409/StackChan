/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "tachikoma_motion.h"
#include <algorithm>
#include <cmath>
#include <hal/hal.h>

namespace stackchan::tachikoma_motion {

namespace {
// Tenths of a degree. These bound what any motion may ask of the servos --
// they are a policy limit, well inside the hardware's own (hal_servo.cpp
// configures yaw at +-1280 and pitch at 30..870, i.e. +-128 and 3..87
// degrees).
//
// They used to be +-30 yaw and 30..60 pitch: a total of six degrees of head
// turn and three of tilt. That is below the threshold of noticing, which is
// why every gesture read as a twitch no matter how the motion data was
// written. Widened to +-25 degrees of turn and 3..50 of tilt -- clearly
// visible from across a room, still a fifth of the yaw the hardware would
// allow, and unchanged in speed (kServoSpeed) so nothing moves more
// abruptly than before, just further.
constexpr int kServoYawMinTenths   = -750;  // -75 degrees
constexpr int kServoYawMaxTenths   = 750;   // +75 degrees
constexpr int kServoPitchMinTenths = 30;    // Physical lower limit is +3 degrees
// Not 3x like yaw: pitch only has 3..87 degrees to work with, and
// ScsServo::handle_stall permanently narrows the runtime limit for the rest
// of the session whenever it trips, so crowding the hardware end would cost
// range rather than gain it. 70 keeps a margin below 87.
constexpr int kServoPitchMaxTenths = 700;   // +70 degrees
// Servo::moveWithSpeed maps this to spring stiffness as
// k = 10 + (speed/1000)^2 * 640, so it is not a velocity -- it is how hard
// the servo pulls toward the target. At 140 the stiffness is only ~22, which
// takes roughly a second to settle. The idle loop holds each step for
// 550-1200ms so it arrives; an emotion step lasting 180-240ms does not get
// anywhere near its target before the next command overwrites it. That, not
// the angle limits, is why widening the range alone did not make the
// gestures bigger.
//
// Two speeds, because the two families want opposite things. Loops stay soft
// so resting motion still drifts rather than snaps. One-shot emotions use
// 500 -- the same value Motion::lookAtNormalized already defaults to -- for
// stiffness ~170, settling in about 0.3s, which fits inside their steps.
constexpr int kServoSpeedAmbient  = 140;
constexpr int kServoSpeedExpressive = 500;
constexpr uint32_t kDisplayPeriodMs = 33;   // About 25-30 Hz on the existing 20 ms task.
constexpr uint32_t kSwitchDurationMs = 350; // Smooth transition between motion types.

int Interpolate(int from, int to, float progress)
{
    const float eased = progress * progress * (3.0f - 2.0f * progress);
    return static_cast<int>(std::lround(from + (to - from) * eased));
}

ResolvedMotionStep CenterStep()
{
    return ResolvedMotionStep{
        .display_x               = 0,
        .display_y               = 0,
        .display_rotation_tenths = 0,
        .servo_yaw_tenths        = 0,
        .servo_pitch_tenths      = kServoPitchMinTenths,
        .duration_ms             = kSwitchDurationMs,
        .hold_ms                 = 0,
    };
}
}  // namespace

MotionController& GetMotionController()
{
    static MotionController controller;
    return controller;
}

ServoMotion& GetServoMotion()
{
    static ServoMotion servo_motion;
    return servo_motion;
}

void SetMotion(MotionType motion)
{
    GetMotionManager().SetMotion(motion);
}

void PlayMotion(MotionType motion)
{
    GetMotionManager().PlayMotion(motion);
}

void StopMotion()
{
    GetMotionManager().StopMotion();
}

MotionType GetCurrentMotion()
{
    return GetMotionManager().GetState().current_motion;
}

bool IsMotionPlaying()
{
    return GetMotionManager().GetState().playing;
}

MotionFrame MotionController::Update(uint32_t now)
{
    UpdateDevelopmentTestSequence(now);

    auto state = GetMotionManager().GetState();
    if (state.generation != observed_generation_) {
        BeginMotion(state, now);
    }

    const uint32_t phase_elapsed = now - phase_start_ms_;
    if (phase_elapsed >= phase_duration_ms_ + phase_hold_ms_) {
        AdvanceStep(now);
    }

    const uint32_t elapsed = now - phase_start_ms_;
    const bool moving      = phase_duration_ms_ > 0 && elapsed < phase_duration_ms_;
    if (moving && (now - last_display_tick_ms_ >= kDisplayPeriodMs)) {
        const float progress = std::min(1.0f, static_cast<float>(elapsed) / phase_duration_ms_);
        frame_.display_x = static_cast<int16_t>(Interpolate(start_frame_.display_x, target_frame_.display_x, progress));
        frame_.display_y = static_cast<int16_t>(Interpolate(start_frame_.display_y, target_frame_.display_y, progress));
        frame_.display_rotation_tenths = static_cast<int16_t>(
            Interpolate(start_frame_.display_rotation_tenths, target_frame_.display_rotation_tenths, progress));
        frame_.moving         = true;
        last_display_tick_ms_ = now;
        ++frame_.display_sequence;
    } else if (!moving && frame_.moving) {
        frame_.display_x               = target_frame_.display_x;
        frame_.display_y               = target_frame_.display_y;
        frame_.display_rotation_tenths = target_frame_.display_rotation_tenths;
        frame_.moving                  = false;
        ++frame_.display_sequence;
    }

    return frame_;
}

void MotionController::BeginMotion(const MotionState& state, uint32_t now)
{
    observed_generation_ = state.generation;
    step_index_          = 0;

    if (!state.playing || state.current_motion == MotionType::None) {
        pattern_ = nullptr;
        const auto center = CenterStep();
        SetTarget(state, center, kSwitchDurationMs, 0, now);
        return;
    }

    pattern_ = &MotionLibrary::GetPattern(state.current_motion);
    BeginStep(state, 0, now, true);
}

void MotionController::BeginStep(const MotionState& state, size_t step_index, uint32_t now, bool switching_motion)
{
    if (pattern_ == nullptr || pattern_->step_count == 0) {
        return;
    }

    step_index_       = step_index % pattern_->step_count;
    const auto target = MotionLibrary::ResolveStep(*pattern_, step_index_);
    const uint32_t duration = switching_motion ? kSwitchDurationMs : target.duration_ms;
    SetTarget(state, target, duration, target.hold_ms, now);
}

void MotionController::AdvanceStep(uint32_t now)
{
    if (pattern_ == nullptr) {
        return;
    }

    auto state = GetMotionManager().GetState();
    if (state.generation != observed_generation_) {
        BeginMotion(state, now);
        return;
    }

    const size_t next_step = step_index_ + 1;
    if (next_step < pattern_->step_count) {
        BeginStep(state, next_step, now, false);
        return;
    }

    if (pattern_->config.playback_mode == MotionPlaybackMode::Loop) {
        BeginStep(state, 0, now, false);
        return;
    }

    const MotionType completed_motion = state.current_motion;
    if (GetMotionManager().CompleteOneShot(completed_motion)) {
        state = GetMotionManager().GetState();
        BeginMotion(state, now);
    }
}

void MotionController::SetTarget(const MotionState& state, const ResolvedMotionStep& target, uint32_t duration_ms,
                                 uint32_t hold_ms, uint32_t now)
{
    start_frame_                          = frame_;
    target_frame_                         = frame_;
    target_frame_.display_x               = target.display_x;
    target_frame_.display_y               = target.display_y;
    target_frame_.display_rotation_tenths = target.display_rotation_tenths;
    target_frame_.servo_yaw_tenths        = target.servo_yaw_tenths;
    target_frame_.servo_pitch_tenths      = target.servo_pitch_tenths;

    frame_.motion                    = state.current_motion;
    frame_.playback_mode             = state.playback_mode;
    frame_.easing                    = MotionEasing::SmoothStep;
    frame_.frame_index               = static_cast<uint16_t>(step_index_);
    frame_.priority                  = state.priority;
    frame_.duration_ms               = duration_ms;
    frame_.hold_ms                   = hold_ms;
    frame_.active                    = state.playing;
    frame_.moving                    = duration_ms > 0;
    frame_.returning_to_center       = target.display_x == 0 && target.display_y == 0 &&
                                       target.display_rotation_tenths == 0 && target.servo_yaw_tenths == 0 &&
                                       target.servo_pitch_tenths == kServoPitchMinTenths;
    frame_.servo_yaw_tenths          = target.servo_yaw_tenths;
    frame_.servo_pitch_tenths        = target.servo_pitch_tenths;
    phase_start_ms_                  = now;
    phase_duration_ms_               = duration_ms;
    phase_hold_ms_                   = hold_ms;
    last_display_tick_ms_            = now - kDisplayPeriodMs;
    ++frame_.servo_command_sequence;

    if (duration_ms == 0) {
        frame_.display_x               = target.display_x;
        frame_.display_y               = target.display_y;
        frame_.display_rotation_tenths = target.display_rotation_tenths;
        frame_.moving                  = false;
        ++frame_.display_sequence;
    }
}

void MotionController::UpdateDevelopmentTestSequence(uint32_t now)
{
#if defined(DEVELOPMENT_BUILD) && ENABLE_TACHIKOMA_MOTION_TEST_SEQUENCE
    const auto state = GetMotionManager().GetState();
    if (!state.playing || state.current_motion == MotionType::None) {
        test_stage_          = TestStage::Idle;
        test_stage_start_ms_ = 0;
        return;
    }

    if (test_stage_start_ms_ == 0) {
        test_stage_start_ms_ = now;
    }

    switch (test_stage_) {
        case TestStage::Idle:
            if (state.current_motion == MotionType::Idle && now - test_stage_start_ms_ >= 5000) {
                SetMotion(MotionType::Listening);
                test_stage_          = TestStage::Listening;
                test_stage_start_ms_ = now;
            }
            break;
        case TestStage::Listening:
            if (state.current_motion == MotionType::Listening && now - test_stage_start_ms_ >= 5000) {
                SetMotion(MotionType::Thinking);
                test_stage_          = TestStage::Thinking;
                test_stage_start_ms_ = now;
            }
            break;
        case TestStage::Thinking:
            if (state.current_motion == MotionType::Thinking && now - test_stage_start_ms_ >= 5000) {
                PlayMotion(MotionType::Happy);
                test_stage_          = TestStage::Happy;
                test_stage_start_ms_ = 0;
            }
            break;
        case TestStage::Happy:
            if (state.current_motion == MotionType::Idle) {
                test_stage_          = TestStage::PostHappyIdle;
                test_stage_start_ms_ = now;
            }
            break;
        case TestStage::PostHappyIdle:
            if (state.current_motion == MotionType::Idle && now - test_stage_start_ms_ >= 3000) {
                PlayMotion(MotionType::Confused);
                test_stage_          = TestStage::Confused;
                test_stage_start_ms_ = 0;
            }
            break;
        case TestStage::Confused:
            if (state.current_motion == MotionType::Idle) {
                test_stage_          = TestStage::Complete;
                test_stage_start_ms_ = now;
            }
            break;
        case TestStage::Complete:
            break;
    }
#else
    (void)now;
#endif
}

namespace {
// Plain bool rather than atomic: written from the announcer's worker task,
// read from the update task, and a tick's worth of staleness on a
// "hold still" flag is not worth a barrier.
volatile bool g_motion_suppressed = false;
}  // namespace

void SetMotionSuppressed(bool suppressed)
{
    g_motion_suppressed = suppressed;
}

bool IsMotionSuppressed()
{
    return g_motion_suppressed;
}

void ServoMotion::Apply(const MotionFrame& frame, stackchan::motion::Motion& motion)
{
    if (frame.servo_command_sequence == last_command_sequence_ || motion.isModifyLocked()) {
        return;
    }
    if (g_motion_suppressed) {
        // Consume the sequence anyway: on leaving manner mode the head
        // should pick up the motion running *then*, not replay whatever
        // frame it was holding when the mode was switched on.
        last_command_sequence_ = frame.servo_command_sequence;
        return;
    }

    const auto yaw_limits   = motion.yawServo().getAngleLimit();
    const auto pitch_limits = motion.pitchServo().getAngleLimit();
    int yaw = std::clamp<int>(frame.servo_yaw_tenths, kServoYawMinTenths, kServoYawMaxTenths);
    int pitch = std::clamp<int>(frame.servo_pitch_tenths, kServoPitchMinTenths, kServoPitchMaxTenths);
    yaw       = std::clamp(yaw, yaw_limits.x, yaw_limits.y);
    pitch     = std::clamp(pitch, pitch_limits.x, pitch_limits.y);

    // Servo::init() and auto torque release leave the servos unpowered while idle; re-enable them only for a
    // rate-limited motion command. The existing Servo update releases torque again after the move settles.
    motion.setTorqueEnabled(true);
    const bool expressive = frame.motion == MotionType::Happy || frame.motion == MotionType::Confused;
    motion.moveWithSpeed(yaw, pitch, expressive ? kServoSpeedExpressive : kServoSpeedAmbient);
    last_command_sequence_ = frame.servo_command_sequence;
}

}  // namespace stackchan::tachikoma_motion
