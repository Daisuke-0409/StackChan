/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "camera_guard.h"

namespace stackchan::hal {

std::mutex& GetCameraMutex()
{
    static std::mutex mutex;
    return mutex;
}

}  // namespace stackchan::hal
