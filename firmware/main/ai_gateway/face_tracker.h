/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * Points the head at whoever is being talked to, and lets the gateway know
 * whose face it is looking at.
 *
 * The device does no vision work. It captures a frame, JPEG-encodes it with
 * the same encoder the app stream uses, and POSTs it to /v1/vision; the
 * gateway answers with where to look, already normalized to -1..1 with the
 * origin at the centre of the frame. That is exactly what
 * Motion::lookAtNormalized takes, so nothing here has to know about faces,
 * lenses, or frame sizes -- and the recognition that decides *who* it is
 * comes out of the same inference on the far end, for free.
 *
 * Capture only happens during a conversation, and rarely while idle. A robot
 * that films its owner continuously is a different product than one that
 * looks up when spoken to.
 */
#pragma once

#include <cstdint>
#include <mutex>
#include <string>

namespace stackchan::ai_gateway {

struct FaceTrackerConfig {
    std::string endpoint;      // full URL to POST, e.g. "http://host:8080/v1/vision"
    std::string device_token;
    std::string device_id;
    uint32_t response_timeout_ms = 8000;
};

enum class FaceTrackerErrorCode : uint8_t {
    None = 0,
    NotConfigured,
    NetworkUnavailable,
    CameraUnavailable,
    EncodeFailed,
    ConnectionFailed,
    InvalidResponse,
    Internal_,
};

class FaceTracker {
public:
    static FaceTracker& GetInstance();

    // Writes endpoint/token to NVS once, like the other gateway clients.
    void ConfigureVision(const std::string& endpoint, const std::string& device_token,
                         const std::string& device_id = "");

    // Drive from the shared update task. Decides on its own whether this
    // tick is due, based on the conversation state.
    void Update(uint32_t now);

    // Suppresses head movement without suppressing anything else -- see
    // MannerMode in speech_announcer. Tracking still runs so identification
    // keeps working; only the servo command is skipped.
    void SetMotionAllowed(bool allowed);
    bool IsMotionAllowed() const;

    FaceTrackerErrorCode GetLastError() const;

private:
    FaceTracker() = default;

    void RunOnce(uint32_t now);
    static void WorkerTask(void* arg);

    mutable std::mutex mutex_;
    bool busy_                  = false;
    bool motion_allowed_        = true;
    uint32_t next_poll_ms_      = 0;
    uint32_t generation_        = 0;
    FaceTrackerErrorCode last_error_ = FaceTrackerErrorCode::None;
};

FaceTracker& GetFaceTracker();

}  // namespace stackchan::ai_gateway
