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
// Deliberately much larger than the idle loop (yaw +-25 -> +-70, and four
// swings instead of two): an emotion nobody notices is not an emotion. The
// idle wobble reads as "alive", this has to read as "pleased" from across a
// room. Durations are shortened alongside the amplitude so the movement is
// bouncy rather than a slow sweep.
constexpr MotionStep kHappySteps[] = {
    Step(0, -8, 0, 0, 70, 0, 0, 0, 0, 2, 200, 280, 40, 90),
    Step(-6, -12, -40, 70, 78, 1, 0, 3, 6, 0, 180, 240, 40, 90),
    Step(6, -12, 40, -70, 78, 1, 0, 3, 6, 0, 180, 240, 40, 90),
    Step(-5, -10, -32, 55, 74, 1, 0, 3, 6, 0, 170, 230, 40, 80),
    Step(5, -10, 32, -55, 74, 1, 0, 3, 6, 0, 170, 230, 40, 80),
    Step(0, -9, 0, 0, 72, 0, 1, 2, 4, 2, 200, 300, 80, 160),
    Step(0, 0, 0, 0, 30, 0, 0, 0, 0, 0, 350, 500, 0, 0),
};

// Confused compares right, left, center and right once more, then safely returns to center.
// Sad reads as a fall, not a wobble: the head lifts slightly, then pitch is
// driven well below the idle centre (30 -> 5) and held there, hanging, before
// creeping back. The slow durations are the point -- speeding this up would
// make it look like a search rather than dejection.
constexpr MotionStep kConfusedSteps[] = {
    Step(0, -4, 0, 0, 46, 0, 1, 2, 2, 2, 350, 500, 150, 300),
    Step(6, 4, 30, -34, 22, 0, 1, 3, 3, 3, 700, 950, 350, 600),
    Step(-6, 6, -30, 34, 12, 0, 1, 3, 3, 3, 750, 1000, 350, 600),
    Step(0, 10, 0, 0, 5, 0, 1, 2, 2, 1, 650, 900, 900, 1400),
    Step(0, 6, 0, 0, 16, 0, 1, 2, 2, 2, 700, 950, 300, 500),
    Step(0, 0, 0, 0, 30, 0, 0, 0, 0, 0, 700, 950, 0, 0),
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
