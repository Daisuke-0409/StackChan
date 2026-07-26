/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "face_tracker.h"

#include <board.h>
#include <cJSON.h>
#include <cstring>
#include <esp_crt_bundle.h>
#include <esp_heap_caps.h>
#include <esp_http_client.h>
#include <esp_netif.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <jpg/image_to_jpeg.h>
#include <mooncake_log.h>
#include <settings.h>
#include <string_view>

#include "ai_gateway_client.h"
#include "speech_announcer.h"
#include "hal/board/hal_bridge.h"
#include "hal/board/stackchan_camera.h"
#include "hal/camera_guard.h"
#include "hal/hal.h"
#include "stackchan/stackchan.h"
#include "stackchan/state/tachikoma_state_manager.h"
#include "stackchan/state/tachikoma_state_types.h"

namespace stackchan::ai_gateway {
namespace {
constexpr std::string_view kTag = "FaceTracker";
constexpr char kSettingsNamespace[] = "tachi_vision";  // NVS namespace <= 15 chars

// Fast enough that the head follows rather than lurches after; slow enough
// that a 320x240 JPEG upload and a round trip fit comfortably inside it.
constexpr uint32_t kActivePollIntervalMs = 700;
// While idle the point is only to notice that somebody has walked up, not to
// track them. Rare on purpose: this is a camera in someone's home.
constexpr uint32_t kIdlePollIntervalMs = 6000;

// Below this the face is too small to be the person being spoken to -- more
// likely someone crossing the room behind them -- and chasing it would make
// the head wander mid-sentence.
constexpr float kMinFaceAreaRatio = 0.010f;
// Ignore movements smaller than this, so the servos are not driven for every
// breath the person takes.
constexpr float kDeadzone = 0.08f;
constexpr int kLookSpeed = 400;

constexpr int kJpegQuality = 20;
constexpr size_t kMaxResponseBytes = 2048;

struct HttpBuffer {
    std::string body;
};

esp_err_t HttpEvent(esp_http_client_event_t* event)
{
    auto* buffer = static_cast<HttpBuffer*>(event->user_data);
    if (event->event_id == HTTP_EVENT_ON_DATA && buffer != nullptr && event->data != nullptr) {
        if (buffer->body.size() + event->data_len <= kMaxResponseBytes) {
            buffer->body.append(static_cast<const char*>(event->data), event->data_len);
        }
    }
    return ESP_OK;
}

FaceTrackerConfig LoadConfig()
{
    FaceTrackerConfig config;
    Settings settings(kSettingsNamespace, false);
    config.endpoint     = settings.GetString("url", "");
    config.device_token = settings.GetString("device_token", "");
    config.device_id    = settings.GetString("device_id", GetHAL().getFactoryMacString(""));
    return config;
}

struct WorkerArgs {
    FaceTracker* tracker;
    FaceTrackerConfig config;
    uint8_t* jpeg;
    size_t jpeg_len;
    uint32_t generation;
};

// Grabs one frame and JPEG-encodes it. Returns nullptr on any failure; the
// caller must free() a non-null result.
uint8_t* CaptureJpeg(size_t& out_len)
{
    out_len = 0;
    auto* camera = static_cast<StackChanCamera*>(hal_bridge::board_get_camera());
    if (camera == nullptr) {
        return nullptr;
    }
    uint8_t* jpeg = nullptr;
    size_t jpeg_len = 0;
    {
        // The frame pointer is only valid until the next capture, and the
        // app's WebSocket stream drives the same camera. See camera_guard.h.
        std::lock_guard<std::mutex> camera_lock(stackchan::hal::GetCameraMutex());
        if (!camera->StreamCaptures()) {
            return nullptr;
        }
        if (!image_to_jpeg(const_cast<uint8_t*>(camera->GetFrameData()), camera->GetFrameSize(),
                           camera->GetFrameWidth(), camera->GetFrameHeight(),
                           static_cast<v4l2_pix_fmt_t>(camera->GetFrameFormat()), kJpegQuality,
                           &jpeg, &jpeg_len)) {
            return nullptr;
        }
    }
    out_len = jpeg_len;
    return jpeg;
}

bool ParseLookAt(const std::string& body, float& x, float& y, float& area)
{
    cJSON* root = cJSON_Parse(body.c_str());
    if (root == nullptr) {
        return false;
    }
    bool ok = false;
    cJSON* look = cJSON_GetObjectItem(root, "look_at");
    if (cJSON_IsObject(look)) {
        cJSON* jx = cJSON_GetObjectItem(look, "x");
        cJSON* jy = cJSON_GetObjectItem(look, "y");
        cJSON* ja = cJSON_GetObjectItem(root, "area_ratio");
        if (cJSON_IsNumber(jx) && cJSON_IsNumber(jy)) {
            x    = static_cast<float>(jx->valuedouble);
            y    = static_cast<float>(jy->valuedouble);
            area = cJSON_IsNumber(ja) ? static_cast<float>(ja->valuedouble) : 1.0f;
            ok   = true;
        }
    }
    cJSON_Delete(root);
    return ok;
}
}  // namespace

FaceTracker& FaceTracker::GetInstance()
{
    static FaceTracker instance;
    return instance;
}

FaceTracker& GetFaceTracker()
{
    return FaceTracker::GetInstance();
}

void FaceTracker::ConfigureVision(const std::string& endpoint, const std::string& device_token,
                                  const std::string& device_id)
{
    Settings settings(kSettingsNamespace, true);
    settings.SetString("url", endpoint);
    settings.SetString("device_token", device_token);
    if (!device_id.empty()) {
        settings.SetString("device_id", device_id);
    }
}

void FaceTracker::SetMotionAllowed(bool allowed)
{
    std::lock_guard<std::mutex> lock(mutex_);
    motion_allowed_ = allowed;
}

bool FaceTracker::IsMotionAllowed() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return motion_allowed_;
}

FaceTrackerErrorCode FaceTracker::GetLastError() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return last_error_;
}

void FaceTracker::Update(uint32_t now)
{
    const auto state = tachikoma_state::GetTachikomaStateManager().GetCurrentState();
    const bool conversing = state == tachikoma_state::TachikomaState::Listening ||
                            state == tachikoma_state::TachikomaState::Thinking ||
                            state == tachikoma_state::TachikomaState::Speaking ||
                            state == tachikoma_state::TachikomaState::Reacting;
    // Asleep means asleep: no capture at all.
    if (state == tachikoma_state::TachikomaState::Sleeping ||
        state == tachikoma_state::TachikomaState::Booting) {
        return;
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (busy_) {
            return;
        }
        if (static_cast<int32_t>(now - next_poll_ms_) < 0) {
            return;
        }
        next_poll_ms_ = now + (conversing ? kActivePollIntervalMs : kIdlePollIntervalMs);
    }
    RunOnce(now);
}

void FaceTracker::RunOnce(uint32_t now)
{
    (void)now;
    const auto config = LoadConfig();
    if (config.endpoint.empty() || config.device_token.empty()) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = FaceTrackerErrorCode::NotConfigured;
        return;
    }
    if (!IsNetworkStackReady(esp_netif_get_nr_of_ifs())) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = FaceTrackerErrorCode::NetworkUnavailable;
        return;
    }

    size_t jpeg_len = 0;
    uint8_t* jpeg = CaptureJpeg(jpeg_len);
    if (jpeg == nullptr || jpeg_len == 0) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = FaceTrackerErrorCode::EncodeFailed;
        return;
    }

    uint32_t generation;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        busy_      = true;
        generation = ++generation_;
    }

    // The upload blocks for as long as the network takes, which must not
    // happen on the shared update task -- that task also drives LVGL, the
    // state machine and the motion loop.
    auto* args = new WorkerArgs{this, config, jpeg, jpeg_len, generation};
    if (xTaskCreate(&FaceTracker::WorkerTask, "face_tracker", 6144, args, 2, nullptr) != pdPASS) {
        free(jpeg);
        delete args;
        std::lock_guard<std::mutex> lock(mutex_);
        busy_       = false;
        last_error_ = FaceTrackerErrorCode::Internal_;
    }
}

void FaceTracker::WorkerTask(void* arg)
{
    auto* args    = static_cast<WorkerArgs*>(arg);
    auto* tracker = args->tracker;
    auto error    = FaceTrackerErrorCode::None;

    HttpBuffer buffer;
    esp_http_client_config_t http_config = {};
    http_config.url           = args->config.endpoint.c_str();
    http_config.method        = HTTP_METHOD_POST;
    http_config.timeout_ms    = static_cast<int>(args->config.response_timeout_ms);
    http_config.event_handler = HttpEvent;
    http_config.user_data     = &buffer;
    if (args->config.endpoint.rfind("https://", 0) == 0) {
        http_config.crt_bundle_attach = esp_crt_bundle_attach;
    }

    esp_http_client_handle_t client = esp_http_client_init(&http_config);
    if (client == nullptr) {
        error = FaceTrackerErrorCode::ConnectionFailed;
    } else {
        const std::string auth = "Bearer " + args->config.device_token;
        esp_http_client_set_header(client, "Authorization", auth.c_str());
        esp_http_client_set_header(client, "X-Device-Id", args->config.device_id.c_str());
        esp_http_client_set_header(client, "Content-Type", "image/jpeg");
        esp_http_client_set_post_field(client, reinterpret_cast<const char*>(args->jpeg),
                                       static_cast<int>(args->jpeg_len));
        const esp_err_t result = esp_http_client_perform(client);
        const int status       = esp_http_client_get_status_code(client);
        esp_http_client_cleanup(client);

        if (result != ESP_OK) {
            error = FaceTrackerErrorCode::ConnectionFailed;
        } else if (status != 200) {
            error = FaceTrackerErrorCode::InvalidResponse;
        } else {
            float x = 0.0f, y = 0.0f, area = 0.0f;
            if (!ParseLookAt(buffer.body, x, y, area)) {
                // No face in frame is a normal answer, not a failure.
                error = FaceTrackerErrorCode::None;
            } else if (area < kMinFaceAreaRatio) {
                mclog::tagInfo(kTag, "face too small to be the speaker, ignoring");
            } else if (std::abs(x) < kDeadzone && std::abs(y) < kDeadzone) {
                // Already looking at them.
            } else if (!tracker->IsMotionAllowed()) {
                mclog::tagInfo(kTag, "manner mode: face located but not turning");
            } else {
                // The gateway's y grows downward (image convention) while a
                // positive pitch raises the head, so it is inverted here.
                GetStackChan().motion().lookAtNormalized(x, -y, kLookSpeed);
                mclog::tagInfo(kTag, "looking at x={} y={}", static_cast<int>(x * 100),
                                static_cast<int>(-y * 100));
            }
        }
    }

    free(args->jpeg);
    {
        std::lock_guard<std::mutex> lock(tracker->mutex_);
        if (args->generation == tracker->generation_) {
            tracker->busy_      = false;
            tracker->last_error_ = error;
        }
    }
    delete args;
    vTaskDelete(nullptr);
}

}  // namespace stackchan::ai_gateway
