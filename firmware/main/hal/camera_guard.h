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
 *
 * It is a timed_mutex, and callers must treat "could not take it in time" as
 * an ordinary outcome -- skip this frame and come back on the next tick.
 *
 * The reason is that the thing being guarded can block forever. StreamCaptures()
 * waits on VIDIOC_DQBUF until a frame arrives, and if the pipeline stops
 * delivering it never returns; see the note in ai_gateway/face_tracker.cpp
 * about the device going silent on the gateway. With a plain mutex that stall
 * spreads: whoever holds it hangs, and then every other camera user hangs
 * behind it -- the app's video freezes because face tracking is stuck, or the
 * other way round. A bounded wait keeps the damage to the one caller that is
 * actually stuck, which is the difference between a dropped frame and a robot
 * that has to be power-cycled.
 */
#pragma once

#include <mutex>

namespace stackchan::hal {

// How long a camera user should wait before giving up on this frame. Long
// enough to ride out a normal capture by the other caller (a capture plus
// JPEG encode is tens of milliseconds), short enough that the face tracker's
// ~700ms cadence and the app's video both just drop a frame instead of
// stalling their task.
constexpr int kCameraLockTimeoutMs = 300;

std::timed_mutex& GetCameraMutex();

}  // namespace stackchan::hal
