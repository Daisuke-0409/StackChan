/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include "tachikoma_motion_types.h"

namespace stackchan::tachikoma_motion {

class MotionLibrary {
public:
    static const MotionPattern& GetPattern(MotionType motion);
    static ResolvedMotionStep ResolveStep(const MotionPattern& pattern, size_t step_index);
    static bool IsLoopMotion(MotionType motion);
    static bool IsOneShotMotion(MotionType motion);
};

}  // namespace stackchan::tachikoma_motion
