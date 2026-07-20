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

#include "voice_input_types.h"

namespace stackchan::voice_input {

// Pure, hardware-independent recording-control logic (Phase 5 Step 4),
// exposed for unit testing.
bool HasExceededMaxDuration(uint32_t now, uint32_t started_ms, uint32_t max_duration_ms);
bool MeetsMinimumDuration(uint32_t duration_ms, uint32_t min_duration_ms);
size_t ClampToRemainingCapacity(size_t current_size, size_t incoming, size_t capacity);

class VoiceInputController {
public:
    // Stores endpoint/token in a dedicated NVS namespace. Values are never logged.
    bool ConfigureTranscribeQueue(const std::string& endpoint, const std::string& device_token,
                                  const std::string& device_id = {});

    // Call from the physical push-to-talk trigger (button/touch zone).
    void OnButtonPressed(uint32_t now);
    void OnButtonReleased(uint32_t now);

    // Call every tick from the same loop that drives SpeechAnnouncer::Update().
    void Update(uint32_t now);
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
    VoiceInputErrorCode last_error_ = VoiceInputErrorCode::None;
};

VoiceInputController& GetVoiceInputController();

}  // namespace stackchan::voice_input
