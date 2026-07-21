/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * Shared exclusion lock for direct AudioCodec output access.
 *
 * xiaozhi's own AudioService (audio_service.cc) and this project's
 * SpeechAnnouncer both call AudioCodec::EnableOutput()/OutputData()
 * directly, from independent FreeRTOS tasks, with no coordination between
 * them: AudioService's playback loop and its idle power-management timer
 * (CheckAndUpdateAudioPowerState(), which can call EnableOutput(false) on
 * its own schedule) on one side, and SpeechAnnouncer::FetchAndPlay()'s
 * multi-second blocking OutputData() call on the other. Without
 * serialization, the power timer can disable output -- or otherwise touch
 * the codec's state -- while a blocking write is in flight, corrupting the
 * I2S driver's internal state. This was the confirmed root cause of a real
 * device crash/reboot: a voice notification pushed to the device while
 * xiaozhi's Application was active rebooted it.
 *
 * Every direct EnableOutput()/OutputData() call site in both
 * SpeechAnnouncer and AudioService must hold this lock for the full
 * duration of its codec access. It intentionally does NOT wrap
 * esp_timer_stop()/esp_timer_start_periodic() calls on
 * audio_power_timer_: esp_timer_stop() blocks until any in-flight timer
 * callback returns, and that callback itself takes this lock, so holding
 * the lock across an esp_timer_stop() call would deadlock.
 *
 * Scoped to output (EnableOutput/OutputData) only, matching the crash this
 * fixes; EnableInput/InputData access is not currently protected.
 */
#pragma once

#include <mutex>

namespace stackchan::hal {

std::mutex& GetAudioCodecMutex();

}  // namespace stackchan::hal
