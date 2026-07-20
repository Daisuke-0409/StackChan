/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "voice_input_controller_self_test.h"

#include "voice_input_controller.h"
#include <esp_log.h>

namespace stackchan::voice_input {
namespace {

constexpr const char* kTag = "VoiceInputTest";

bool Expect(bool condition, const char* name)
{
    if (!condition) {
        ESP_LOGE(kTag, "FAIL: %s", name);
        return false;
    }
    return true;
}

}  // namespace

bool RunVoiceInputControllerSelfTest()
{
    bool passed = true;

    // HasExceededMaxDuration: Phase 5 Step 4 recording-control cap.
    passed &= Expect(!HasExceededMaxDuration(500, 0, 8000), "500ms elapsed, 8000ms cap -> not exceeded");
    passed &= Expect(HasExceededMaxDuration(8000, 0, 8000), "exactly at cap -> exceeded");
    passed &= Expect(HasExceededMaxDuration(9000, 0, 8000), "past cap -> exceeded");
    // uint32_t millis() wraps around every ~49.7 days; only 21ms actually
    // elapsed here even though `now` is numerically smaller than `started_ms`.
    passed &= Expect(!HasExceededMaxDuration(5, 0xFFFFFFF0u, 8000), "millis() wraparound handled safely");

    // MeetsMinimumDuration: rejects too-short/noise clips.
    passed &= Expect(!MeetsMinimumDuration(0, 300), "0ms does not meet 300ms minimum");
    passed &= Expect(!MeetsMinimumDuration(299, 300), "299ms does not meet 300ms minimum");
    passed &= Expect(MeetsMinimumDuration(300, 300), "exactly at minimum meets it");
    passed &= Expect(MeetsMinimumDuration(301, 300), "above minimum meets it");

    // ClampToRemainingCapacity: bounds the recording buffer, mirroring
    // SpeechAnnouncer's kMaxAudioBytes cap, without growing past it.
    passed &= Expect(ClampToRemainingCapacity(0, 320, 1000) == 320, "room for the whole frame");
    passed &= Expect(ClampToRemainingCapacity(900, 320, 1000) == 100, "frame truncated to remaining capacity");
    passed &= Expect(ClampToRemainingCapacity(1000, 320, 1000) == 0, "already at capacity -> nothing taken");
    passed &= Expect(ClampToRemainingCapacity(1200, 320, 1000) == 0, "past capacity -> nothing taken");
    passed &= Expect(ClampToRemainingCapacity(0, 0, 1000) == 0, "empty frame -> nothing taken");

    passed &= Expect(VoiceInputErrorCode::NetworkUnavailable != VoiceInputErrorCode::None,
                     "NetworkUnavailable distinct from None");
    passed &= Expect(VoiceInputErrorCode::RecordingTooShort != VoiceInputErrorCode::NetworkUnavailable,
                     "RecordingTooShort distinct from NetworkUnavailable");

    // A chatter-scale press (well under the debounce threshold used by
    // StopRecordingAndUpload) must still be rejected -- this is the same
    // kMinRecordingMs guard exercised above, reused as-is for the head-touch
    // trigger rather than adding a second threshold.
    passed &= Expect(!MeetsMinimumDuration(50, 300), "50ms chatter-scale press rejected by the debounce guard");

    // MapHeadTouchGesture: Phase 5 Step 6 head-touch trigger. Only Press and
    // Release may ever start/stop a recording; Swipe/None must map to no
    // action so this can never fire on the gesture HeadPetModifier already
    // owns (SwipeForward/SwipeBackward).
    passed &= Expect(MapHeadTouchGesture(HeadPetGesture::Press) == TriggerAction::Press,
                     "Press maps to the Press trigger action");
    passed &= Expect(MapHeadTouchGesture(HeadPetGesture::Release) == TriggerAction::Release,
                     "Release maps to the Release trigger action");
    passed &= Expect(MapHeadTouchGesture(HeadPetGesture::SwipeForward) == TriggerAction::None,
                     "SwipeForward (HeadPetModifier's gesture) maps to no action");
    passed &= Expect(MapHeadTouchGesture(HeadPetGesture::SwipeBackward) == TriggerAction::None,
                     "SwipeBackward (HeadPetModifier's gesture) maps to no action");
    passed &= Expect(MapHeadTouchGesture(HeadPetGesture::None) == TriggerAction::None,
                     "None maps to no action");

    ESP_LOGI(kTag, "Self-test %s", passed ? "PASS" : "FAIL");
    return passed;
}

}  // namespace stackchan::voice_input
