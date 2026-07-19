/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "tachikoma_motion_manager.h"
#include "tachikoma_motion_library.h"

namespace stackchan::tachikoma_motion {

MotionManager& GetMotionManager()
{
    static MotionManager manager;
    return manager;
}

bool MotionManager::SetMotion(MotionType motion)
{
    if (!MotionLibrary::IsLoopMotion(motion)) {
        return false;
    }

    std::lock_guard<std::mutex> lock(mutex_);
    if (state_.playing && state_.playback_mode == MotionPlaybackMode::OneShot) {
        return false;
    }
    if (state_.playing && state_.current_motion == motion) {
        return false;
    }

    const auto& pattern             = MotionLibrary::GetPattern(motion);
    state_.current_motion           = motion;
    state_.previous_loop_motion     = motion;
    state_.playback_mode            = MotionPlaybackMode::Loop;
    state_.priority                 = pattern.config.priority;
    state_.playing                  = true;
    ++state_.generation;
    return true;
}

bool MotionManager::PlayMotion(MotionType motion)
{
    if (!MotionLibrary::IsOneShotMotion(motion)) {
        return false;
    }

    std::lock_guard<std::mutex> lock(mutex_);
    const auto& pattern = MotionLibrary::GetPattern(motion);
    if (state_.playing && state_.current_motion == motion) {
        return false;
    }
    if (state_.playing && state_.playback_mode == MotionPlaybackMode::OneShot &&
        pattern.config.priority <= state_.priority) {
        return false;
    }

    if (state_.playing && state_.playback_mode == MotionPlaybackMode::Loop) {
        state_.previous_loop_motion = state_.current_motion;
    }
    state_.current_motion = motion;
    state_.playback_mode  = MotionPlaybackMode::OneShot;
    state_.priority       = pattern.config.priority;
    state_.playing        = true;
    ++state_.generation;
    return true;
}

bool MotionManager::StopMotion()
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (!state_.playing && state_.current_motion == MotionType::None) {
        return false;
    }

    state_.current_motion = MotionType::None;
    state_.playback_mode  = MotionPlaybackMode::Loop;
    state_.priority       = 0;
    state_.playing        = false;
    ++state_.generation;
    return true;
}

bool MotionManager::CompleteOneShot(MotionType completed_motion)
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (!state_.playing || state_.playback_mode != MotionPlaybackMode::OneShot ||
        state_.current_motion != completed_motion) {
        return false;
    }

    // Phase2-7 deliberately returns one-shots to Idle. previous_loop_motion is retained for a future policy.
    const auto& idle_pattern     = MotionLibrary::GetPattern(MotionType::Idle);
    state_.current_motion       = MotionType::Idle;
    state_.playback_mode        = MotionPlaybackMode::Loop;
    state_.priority             = idle_pattern.config.priority;
    state_.playing              = true;
    ++state_.completion_sequence;
    ++state_.generation;
    return true;
}

MotionState MotionManager::GetState() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return state_;
}

}  // namespace stackchan::tachikoma_motion
