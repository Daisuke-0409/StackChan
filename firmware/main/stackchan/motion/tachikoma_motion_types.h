/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <cstddef>
#include <cstdint>

namespace stackchan::tachikoma_motion {

enum class MotionType : uint8_t {
    None,
    Idle,
    Listening,
    Thinking,
    Happy,
    Confused,
};

enum class MotionPlaybackMode : uint8_t {
    Loop,
    OneShot,
};

enum class MotionEasing : uint8_t {
    SmoothStep,
};

struct MotionFrame {
    MotionType motion                   = MotionType::None;
    MotionPlaybackMode playback_mode   = MotionPlaybackMode::Loop;
    MotionEasing easing                 = MotionEasing::SmoothStep;
    int16_t display_x                   = 0;
    int16_t display_y                   = 0;
    int16_t display_rotation_tenths     = 0;
    int16_t servo_yaw_tenths            = 0;
    int16_t servo_pitch_tenths          = 30;
    uint16_t frame_index                = 0;
    uint8_t priority                    = 0;
    uint32_t duration_ms                = 0;
    uint32_t hold_ms                    = 0;
    uint32_t display_sequence           = 0;
    uint32_t servo_command_sequence     = 0;
    bool active                         = false;
    bool moving                         = false;
    bool returning_to_center            = false;
};

struct MotionConfig {
    MotionPlaybackMode playback_mode;
    uint8_t priority;
    int16_t max_display_x;
    int16_t max_display_y;
    int16_t max_rotation_tenths;
    int16_t min_servo_yaw_tenths;
    int16_t max_servo_yaw_tenths;
    int16_t min_servo_pitch_tenths;
    int16_t max_servo_pitch_tenths;
};

struct MotionStep {
    int16_t display_x;
    int16_t display_y;
    int16_t display_rotation_tenths;
    int16_t servo_yaw_tenths;
    int16_t servo_pitch_tenths;
    uint8_t display_x_jitter;
    uint8_t display_y_jitter;
    uint8_t rotation_jitter_tenths;
    uint8_t servo_yaw_jitter_tenths;
    uint8_t servo_pitch_jitter_tenths;
    uint16_t min_duration_ms;
    uint16_t max_duration_ms;
    uint16_t min_hold_ms;
    uint16_t max_hold_ms;
};

struct ResolvedMotionStep {
    int16_t display_x;
    int16_t display_y;
    int16_t display_rotation_tenths;
    int16_t servo_yaw_tenths;
    int16_t servo_pitch_tenths;
    uint32_t duration_ms;
    uint32_t hold_ms;
};

struct MotionPattern {
    MotionType type;
    MotionConfig config;
    const MotionStep* steps;
    size_t step_count;
};

struct MotionState {
    MotionType current_motion         = MotionType::None;
    MotionType previous_loop_motion   = MotionType::Idle;
    MotionPlaybackMode playback_mode = MotionPlaybackMode::Loop;
    uint8_t priority                  = 0;
    uint32_t generation              = 0;
    // Incremented whenever a one-shot motion reaches its terminal frame.
    // StateManager uses this sequence to enqueue the return transition without
    // polling motion internals or calling display/servo adapters directly.
    uint32_t completion_sequence     = 0;
    bool playing                     = false;
};

}  // namespace stackchan::tachikoma_motion
