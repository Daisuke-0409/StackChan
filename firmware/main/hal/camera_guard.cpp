/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "camera_guard.h"

namespace stackchan::hal {

std::timed_mutex& GetCameraMutex()
{
    static std::timed_mutex mutex;
    return mutex;
}

}  // namespace stackchan::hal
