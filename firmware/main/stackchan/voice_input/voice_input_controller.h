/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * Push-to-talk logic layer for Phase 5: captures a bounded clip of mic PCM
 * while a button is held, uploads it to the Tachikoma Gateway's transcribe
 * endpoint, and hands the recognized text to AiGatewayClient::StartRequest()
 * so it flows through the existing chat pipeline unchanged.
 *
 * Mic capture goes through Board::GetInstance().GetAudioCodec()'s existing
 * InputData()/EnableInput() API (the same class AudioCodec already exposes
 * for xiaozhi's own voice pipeline; see AudioService::ReadAudioData()), not
 * a new hardware abstraction.
 *
 * State transitions reuse the existing TachikomaStateManager event set:
 * a button press fires UserSpeechStarted (Idle/Speaking -> Listening);
 * release fires UserSpeechEnded (Listening -> Thinking). A failure at any
 * point (recording too short, network unavailable, upload/transcription
 * error) fires AiRequestFailed, the same event AiGatewayClient already uses
 * for a failed chat request, so a stuck Thinking state always resolves the
 * same way regardless of which stage failed.
 */
#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

#include <hal/hal.h>

#include "stackchan/state/tachikoma_state_types.h"
#include "voice_input_types.h"

namespace stackchan::voice_input {

// Pure, hardware-independent recording-control logic (Phase 5 Step 4),
// exposed for unit testing.
bool HasExceededMaxDuration(uint32_t now, uint32_t started_ms, uint32_t max_duration_ms);
bool MeetsMinimumDuration(uint32_t duration_ms, uint32_t min_duration_ms);
size_t ClampToRemainingCapacity(size_t current_size, size_t incoming, size_t capacity);

// This board's mic is configured with AUDIO_INPUT_REFERENCE=true (see
// hal/board/config.h), which makes AudioCodec::input_channels() 2: channel
// 0 is the real microphone, channel 1 an echo-cancellation reference feed
// -- AudioCodec::InputData() returns them raw-interleaved, undocumented at
// the call site. Uploading that as declared-mono (the Content-Type this
// class sends has always said "channels=1") spliced a much quieter,
// unrelated signal into every other sample -- confirmed on real hardware
// via a saved WAV: channel-0 samples ran ~13x the RMS of channel-1's.
// Extracting channel 0 here keeps the rest of this file's mono assumption
// (buffer_, kMaxRecordingSamples, the "channels=1" upload header) actually
// true instead of just declared. channels<=1 returns the input unchanged.
std::vector<int16_t> DownmixToChannel0(const std::vector<int16_t>& interleaved, int channels);

// Phase 5 Step 6 physical trigger: the Si12T head-touch sensor already
// detects Press/Release (see hal_head_touch.cpp) and delivers them via
// Hal::onHeadPetGesture, whose only other subscriber (HeadPetModifier)
// reacts to SwipeForward/SwipeBackward/Release, not Press. Mapping only
// Press/Release to a trigger action, and nothing else, is what guarantees
// this can never fire on a swipe -- kept as a pure function so that
// guarantee is unit-testable without touching the real Signal/HAL.
enum class TriggerAction { None, Press, Release };
TriggerAction MapHeadTouchGesture(HeadPetGesture gesture);

// Hands-free continuation. On by default; the settings app can turn it off
// for anyone who would rather press to talk every time.
bool IsContinuousConversationEnabled();
void SetContinuousConversationEnabled(bool enabled);

class VoiceInputController {
public:
    // Stores endpoint/token in a dedicated NVS namespace. Values are never logged.
    bool ConfigureTranscribeQueue(const std::string& endpoint, const std::string& device_token,
                                  const std::string& device_id = {});

    // Call from the physical push-to-talk trigger (button/touch zone).
    void OnButtonPressed(uint32_t now);
    void OnButtonReleased(uint32_t now);

    // Subscribes to the Si12T head-touch signal so a Press/Release on the
    // robot's head starts/stops recording. Idempotent: calling more than
    // once has no additional effect.
    void ConnectHeadTouchTrigger();

    // Call every tick from the same loop that drives SpeechAnnouncer::Update().
    // Only tracks the post-speech cooldown window now -- the actual mic
    // capture loop runs on its own task, see StartRecordingTask().
    void Update(uint32_t now);

    // Spins up the dedicated FreeRTOS task that owns the mic-capture loop
    // while recording_ is true. Call once from HAL init, alongside
    // ConnectHeadTouchTrigger(). Split out of the shared _stackchan_update_task
    // (hal.cpp): that task's ~20ms nominal tick was measured running 30-85ms
    // in practice (LVGL/state/motion work sharing the same loop), and since
    // the capture loop only ever drains one fixed 20ms frame per call
    // regardless of elapsed time, a slow tick meant permanently lost audio --
    // see firmware/README.md's recording-duration investigation. Idempotent:
    // calling more than once has no additional effect.
    void StartRecordingTask();

    void Stop();

    bool IsRecording() const;
    bool IsBusy() const;
    VoiceInputErrorCode GetLastError() const;

private:
    struct WorkerArgs {
        VoiceInputController* controller;
        VoiceInputConfig config;
        std::vector<int16_t> pcm;
        uint32_t sample_rate;
        uint32_t generation;
    };

    static void WorkerTask(void* arg);
    void RunWorker(WorkerArgs* args);
    static void RecordingTaskEntry(void* arg);
    void RecordingTask();
    // Returns true when it performed a blocking codec read, i.e. when the
    // call itself already paced the caller and no extra delay is wanted.
    bool CaptureTick(uint32_t now);
    bool UploadAndTranscribe(const VoiceInputConfig& config, const std::vector<int16_t>& pcm, uint32_t sample_rate,
                             std::string& text, VoiceInputErrorCode& error);
    void StopRecordingAndUpload(uint32_t now);
    void Complete(uint32_t generation, VoiceInputErrorCode error, const std::string& text);
    VoiceInputConfig LoadConfig() const;

    mutable std::mutex mutex_;
    bool recording_ = false;
    bool busy_ = false;
    uint32_t generation_ = 0;
    uint32_t recording_started_ms_ = 0;
    std::vector<int16_t> buffer_;
    // What buffer_ was actually able to reserve for the recording in flight,
    // which may be below kMaxRecordingSamples if PSRAM was short at the time.
    // Capture is clamped to this, not to the constant.
    size_t recording_capacity_samples_ = 0;
    VoiceInputErrorCode last_error_ = VoiceInputErrorCode::None;

    // Post-speech cooldown (see kPostSpeechCooldownMs in the .cpp): a
    // Speaking -> Idle edge, detected in Update(), arms cooldown_until_ms_
    // so a Press within the window right after playback ends is ignored.
    // Guards against the speaker's own vibration reaching the head-touch
    // sensor and being misread as a real Press. last_observed_state_ is
    // Update()'s only concern -- OnButtonPressed() doesn't touch it.
    tachikoma_state::TachikomaState last_observed_state_ = tachikoma_state::TachikomaState::Booting;
    uint32_t cooldown_until_ms_ = 0;

    // Set once from ConnectHeadTouchTrigger() at boot; never touched
    // concurrently, so no mutex_ protection needed.
    bool head_touch_connected_ = false;
    size_t head_touch_connection_ = 0;

    // Set once from StartRecordingTask() at boot; same single-caller
    // reasoning as head_touch_connected_ above.
    bool recording_task_started_ = false;

    // --- hands-free follow-up ---------------------------------------------
    // After a reply finishes, the conversation stays open for a while: the
    // user can just keep talking instead of holding the head for every turn.
    // The head touch is what *starts* a conversation; this is what lets it
    // continue.
    //
    // Speech detection is done here rather than through the AFE's VAD
    // because AudioService owns the mic while its processor runs, and
    // recording already has to take that mic away. Reading frames we are
    // already reading and measuring their level avoids handing the codec
    // back and forth several times a second.
    bool follow_up_open_ = false;
    uint32_t follow_up_until_ms_ = 0;
    bool follow_up_speech_ = false;      // currently inside an utterance
    uint32_t follow_up_speech_start_ms_ = 0;
    uint32_t follow_up_quiet_since_ms_ = 0;
    // A short ring of recent audio, so an utterance does not lose the
    // syllable that crossed the threshold in the first place.
    std::vector<int16_t> follow_up_preroll_;
    size_t follow_up_preroll_pos_ = 0;
    bool follow_up_preroll_filled_ = false;

    void OpenFollowUp(uint32_t now);
    void CloseFollowUp(const char* why);
    void FollowUpTick(uint32_t now, const std::vector<int16_t>& frame);
};

VoiceInputController& GetVoiceInputController();

}  // namespace stackchan::voice_input
