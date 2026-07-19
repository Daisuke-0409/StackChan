/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "tachikoma_motion_library.h"
#include "../utils/random.h"
#include <algorithm>
#include <iterator>

namespace stackchan::tachikoma_motion {

namespace {

constexpr MotionStep Step(int x, int y, int rotation, int yaw, int pitch, int x_jitter, int y_jitter,
                          int rotation_jitter, int yaw_jitter, int pitch_jitter, int min_duration,
                          int max_duration, int min_hold, int max_hold)
{
    return MotionStep{
        static_cast<int16_t>(x),          static_cast<int16_t>(y),
        static_cast<int16_t>(rotation),   static_cast<int16_t>(yaw),
        static_cast<int16_t>(pitch),      static_cast<uint8_t>(x_jitter),
        static_cast<uint8_t>(y_jitter),   static_cast<uint8_t>(rotation_jitter),
        static_cast<uint8_t>(yaw_jitter), static_cast<uint8_t>(pitch_jitter),
        static_cast<uint16_t>(min_duration), static_cast<uint16_t>(max_duration),
        static_cast<uint16_t>(min_hold),  static_cast<uint16_t>(max_hold),
    };
}

constexpr MotionConfig LoopConfig(uint8_t priority, int max_x, int max_y, int max_rotation)
{
    return MotionConfig{MotionPlaybackMode::Loop, priority, static_cast<int16_t>(max_x),
                        static_cast<int16_t>(max_y), static_cast<int16_t>(max_rotation), -30, 30, 30, 60};
}

constexpr MotionConfig OneShotConfig(uint8_t priority, int max_x, int max_y, int max_rotation)
{
    return MotionConfig{MotionPlaybackMode::OneShot, priority, static_cast<int16_t>(max_x),
                        static_cast<int16_t>(max_y), static_cast<int16_t>(max_rotation), -30, 30, 30, 60};
}

// Idle preserves the Phase2-6 sequence: center, look right, center, look left, center and pause.
constexpr MotionStep kIdleSteps[] = {
    Step(0, 0, 0, 0, 30, 0, 0, 0, 0, 0, 300, 450, 600, 1100),
    Step(2, -3, 12, -25, 48, 1, 1, 3, 5, 5, 550, 800, 150, 400),
    Step(0, 0, 0, 0, 30, 0, 0, 0, 0, 0, 550, 800, 400, 900),
    Step(-2, -3, -12, 25, 48, 1, 1, 3, 5, 5, 550, 800, 150, 400),
    Step(0, 0, 0, 0, 30, 0, 0, 0, 0, 0, 550, 800, 650, 1200),
};

// Listening leans slightly upward and spends most of its time still, with small acknowledgements.
constexpr MotionStep kListeningSteps[] = {
    Step(0, -4, 0, 0, 50, 0, 1, 2, 3, 3, 500, 800, 1200, 1800),
    Step(1, -5, 4, -10, 55, 0, 1, 2, 3, 3, 450, 650, 250, 500),
    Step(0, -4, 0, 0, 50, 0, 1, 2, 3, 3, 500, 800, 1300, 2000),
    Step(-1, -5, -4, 10, 55, 0, 1, 2, 3, 3, 450, 650, 250, 500),
};

// Thinking stays at the lowest safe pitch and slowly alternates direction with irregular short pauses.
constexpr MotionStep kThinkingSteps[] = {
    Step(0, 2, 0, 0, 30, 0, 1, 2, 3, 0, 450, 700, 250, 600),
    Step(-3, 3, -15, 22, 30, 1, 1, 3, 5, 0, 700, 1000, 200, 800),
    Step(0, 2, 0, 0, 30, 0, 1, 2, 3, 0, 450, 700, 200, 500),
    Step(3, 3, 15, -22, 30, 1, 1, 3, 5, 0, 700, 1000, 200, 800),
};

// Happy is a short, energetic but safely clamped upward bounce, ending at center.
constexpr MotionStep kHappySteps[] = {
    Step(0, -4, 0, 0, 55, 0, 0, 0, 0, 2, 300, 400, 80, 150),
    Step(-3, -6, -18, 25, 60, 1, 0, 2, 5, 0, 250, 350, 80, 160),
    Step(3, -6, 18, -25, 60, 1, 0, 2, 5, 0, 250, 350, 80, 160),
    Step(0, -5, 0, 0, 58, 0, 1, 2, 3, 2, 250, 400, 100, 200),
    Step(0, 0, 0, 0, 30, 0, 0, 0, 0, 0, 400, 550, 0, 0),
};

// Confused compares right, left, center and right once more, then safely returns to center.
constexpr MotionStep kConfusedSteps[] = {
    Step(4, 1, 20, -28, 35, 0, 1, 3, 2, 3, 600, 800, 400, 700),
    Step(-4, 1, -20, 28, 35, 0, 1, 3, 2, 3, 650, 850, 300, 600),
    Step(0, 1, 0, 0, 32, 0, 1, 2, 3, 2, 450, 650, 300, 600),
    Step(3, 1, 15, -24, 35, 0, 1, 3, 3, 3, 550, 750, 300, 500),
    Step(0, 0, 0, 0, 30, 0, 0, 0, 0, 0, 550, 750, 0, 0),
};

constexpr MotionPattern kPatterns[] = {
    {MotionType::Idle, LoopConfig(10, 3, 4, 15), kIdleSteps, std::size(kIdleSteps)},
    {MotionType::Listening, LoopConfig(20, 2, 6, 8), kListeningSteps, std::size(kListeningSteps)},
    {MotionType::Thinking, LoopConfig(20, 4, 4, 18), kThinkingSteps, std::size(kThinkingSteps)},
    {MotionType::Happy, OneShotConfig(30, 4, 6, 20), kHappySteps, std::size(kHappySteps)},
    {MotionType::Confused, OneShotConfig(30, 4, 3, 22), kConfusedSteps, std::size(kConfusedSteps)},
};

int Jittered(int value, int jitter)
{
    if (jitter == 0) {
        return value;
    }
    return value + stackchan::Random::getInstance().getInt(-jitter, jitter);
}

uint32_t RandomRange(uint32_t minimum, uint32_t maximum)
{
    return static_cast<uint32_t>(stackchan::Random::getInstance().getInt(static_cast<int>(minimum),
                                                                         static_cast<int>(maximum)));
}

}  // namespace

const MotionPattern& MotionLibrary::GetPattern(MotionType motion)
{
    for (const auto& pattern : kPatterns) {
        if (pattern.type == motion) {
            return pattern;
        }
    }
    return kPatterns[0];
}

ResolvedMotionStep MotionLibrary::ResolveStep(const MotionPattern& pattern, size_t step_index)
{
    const auto& step   = pattern.steps[step_index % pattern.step_count];
    const auto& config = pattern.config;

    return ResolvedMotionStep{
        .display_x = static_cast<int16_t>(std::clamp(Jittered(step.display_x, step.display_x_jitter),
                                                     -static_cast<int>(config.max_display_x),
                                                     static_cast<int>(config.max_display_x))),
        .display_y = static_cast<int16_t>(std::clamp(Jittered(step.display_y, step.display_y_jitter),
                                                     -static_cast<int>(config.max_display_y),
                                                     static_cast<int>(config.max_display_y))),
        .display_rotation_tenths = static_cast<int16_t>(
            std::clamp(Jittered(step.display_rotation_tenths, step.rotation_jitter_tenths),
                       -static_cast<int>(config.max_rotation_tenths),
                       static_cast<int>(config.max_rotation_tenths))),
        .servo_yaw_tenths = static_cast<int16_t>(
            std::clamp(Jittered(step.servo_yaw_tenths, step.servo_yaw_jitter_tenths),
                       static_cast<int>(config.min_servo_yaw_tenths),
                       static_cast<int>(config.max_servo_yaw_tenths))),
        .servo_pitch_tenths = static_cast<int16_t>(
            std::clamp(Jittered(step.servo_pitch_tenths, step.servo_pitch_jitter_tenths),
                       static_cast<int>(config.min_servo_pitch_tenths),
                       static_cast<int>(config.max_servo_pitch_tenths))),
        .duration_ms = RandomRange(step.min_duration_ms, step.max_duration_ms),
        .hold_ms     = RandomRange(step.min_hold_ms, step.max_hold_ms),
    };
}

bool MotionLibrary::IsLoopMotion(MotionType motion)
{
    return motion != MotionType::None && GetPattern(motion).config.playback_mode == MotionPlaybackMode::Loop;
}

bool MotionLibrary::IsOneShotMotion(MotionType motion)
{
    return motion != MotionType::None && GetPattern(motion).config.playback_mode == MotionPlaybackMode::OneShot;
}

}  // namespace stackchan::tachikoma_motion
