/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include "tachikoma_motion_types.h"
#include <mutex>

namespace stackchan::tachikoma_motion {

class MotionManager {
public:
    bool SetMotion(MotionType motion);
    bool PlayMotion(MotionType motion);
    bool StopMotion();
    bool CompleteOneShot(MotionType completed_motion);
    MotionState GetState() const;

private:
    mutable std::mutex mutex_;
    MotionState state_;
};

MotionManager& GetMotionManager();

}  // namespace stackchan::tachikoma_motion
