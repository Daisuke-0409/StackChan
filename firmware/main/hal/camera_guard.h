/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * Shared exclusion lock for StackChanCamera frame capture.
 *
 * StreamCaptures() dequeues a v4l2 buffer and then frees and reallocates
 * frame_.data, and GetFrameData() hands out a pointer straight into that
 * allocation. Two callers doing this at once is a use-after-free: one can be
 * reading the buffer the other just released, and both are driving
 * VIDIOC_DQBUF/VIDIOC_QBUF on the same fd.
 *
 * There were no two callers until face tracking was added -- the app's
 * WebSocket stream was the only one, and it only runs while the app is
 * connected. It is still the only one most of the time, which is exactly the
 * kind of race that shows up once, on someone else's desk, with the app open.
 *
 * Every StreamCaptures() call and every use of the frame it produced must
 * hold this for the whole sequence, not just the capture: the pointer is
 * only valid until the next capture.
 */
#pragma once

#include <mutex>

namespace stackchan::hal {

std::mutex& GetCameraMutex();

}  // namespace stackchan::hal
