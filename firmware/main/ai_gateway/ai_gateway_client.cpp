/*
 * SPDX-License-Identifier: MIT
 */
#include "ai_gateway_client.h"

#include <algorithm>
#include <cJSON.h>
#include <cstring>
#include <esp_http_client.h>
#include <esp_crt_bundle.h>
#include <esp_netif.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <mooncake_log.h>
#include <settings.h>
#include <string_view>

#include "ai_gateway/speech_announcer.h"
#include "hal/hal.h"
#include "stackchan/state/tachikoma_state_manager.h"
#include "stackchan/state/tachikoma_state_types.h"

namespace stackchan::ai_gateway {
namespace {
constexpr std::string_view kTag = "AiGateway";
constexpr size_t kMaxInputBytes = 512;
constexpr size_t kMaxResponseBytes = 4096;
constexpr uint32_t kMockDelayMs = 1200;
constexpr uint32_t kMinSpeechMs = 700;
constexpr uint32_t kMaxSpeechMs = 12000;
// The boot-time development probe (StartDevelopmentMock) races WiFi/LWIP
// bring-up, so a worker may start before the tcpip thread exists; bound how
// long it waits for the stack instead of failing that first request outright.
constexpr uint32_t kNetworkReadyPollMs = 500;
constexpr uint32_t kNetworkReadyTimeoutMs = 15000;
constexpr char kSettingsNamespace[] = "tachi_gateway";  // NVS namespace <= 15 chars

struct HttpBuffer {
    std::string body;
};

esp_err_t HttpEvent(esp_http_client_event_t* event)
{
    auto* buffer = static_cast<HttpBuffer*>(event->user_data);
    if (event->event_id == HTTP_EVENT_ON_DATA && buffer != nullptr && event->data != nullptr) {
        if (buffer->body.size() + event->data_len > kMaxResponseBytes) {
            return ESP_ERR_NO_MEM;
        }
        buffer->body.append(static_cast<const char*>(event->data), event->data_len);
    }
    return ESP_OK;
}

const char* ErrorName(AiErrorCode error)
{
    switch (error) {
    case AiErrorCode::InvalidInput: return "invalid_input";
    case AiErrorCode::Busy: return "busy";
    case AiErrorCode::NotConfigured: return "not_configured";
    case AiErrorCode::WifiUnavailable: return "wifi_unavailable";
    case AiErrorCode::ConnectionFailed: return "connection_failed";
    case AiErrorCode::Timeout: return "timeout";
    case AiErrorCode::AuthenticationFailed: return "authentication_failed";
    case AiErrorCode::RateLimited: return "rate_limited";
    case AiErrorCode::ServerError: return "server_error";
    case AiErrorCode::InvalidResponse: return "invalid_response";
    case AiErrorCode::Cancelled: return "cancelled";
    case AiErrorCode::Internal: return "internal";
    default: return "none";
    }
}

// Blocks the worker task (never the caller) until the LWIP/netif stack
// exists, polling because there is no event to subscribe to for "some netif
// was created" that works for both WiFi bring-up paths (xiaozhi's and
// StackChanWifiStation's). Readiness here means the tcpip thread exists,
// not that an IP was acquired -- a connect attempt without an IP fails
// cleanly through the normal retry path.
bool WaitForNetworkStack()
{
    uint32_t waited_ms = 0;
    while (!IsNetworkStackReady(esp_netif_get_nr_of_ifs())) {
        if (waited_ms >= kNetworkReadyTimeoutMs) {
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(kNetworkReadyPollMs));
        waited_ms += kNetworkReadyPollMs;
    }
    return true;
}
}  // namespace

AiGatewayClient& GetAiGatewayClient()
{
    static AiGatewayClient client;
    return client;
}

std::string AiGatewayClient::MakeId(const char* prefix, uint32_t sequence)
{
    return std::string(prefix) + "-" + std::to_string(sequence);
}

AiGatewayConfig AiGatewayClient::LoadConfig() const
{
    Settings settings(kSettingsNamespace);
    AiGatewayConfig config;
    config.endpoint = settings.GetString("url");
    config.device_token = settings.GetString("device_token");
    config.device_id = settings.GetString("device_id", GetHAL().getFactoryMacString(""));
    return config;
}

bool AiGatewayClient::ConfigureGateway(const std::string& endpoint, const std::string& device_token,
                                       const std::string& device_id)
{
    if (endpoint.empty() || device_token.empty() || endpoint.size() > 255 || device_token.size() > 255) {
        return false;
    }
    Settings settings(kSettingsNamespace, true);
    settings.SetString("url", endpoint);
    settings.SetString("device_token", device_token);
    if (!device_id.empty()) {
        settings.SetString("device_id", device_id);
    }
    return true;
}

bool AiGatewayClient::StartRequest(const std::string& text)
{
    return StartRequestInternal(text, false);
}

bool AiGatewayClient::StartRequestInternal(const std::string& text, bool force_mock)
{
    if (text.empty() || text.size() > kMaxInputBytes) {
        mclog::tagWarn(kTag, "request rejected: invalid input length={}", text.size());
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = AiErrorCode::InvalidInput;
        return false;
    }

    AiRequest request;
    uint32_t generation;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (busy_) {
            last_error_ = AiErrorCode::Busy;
            return false;
        }
        ++sequence_;
        generation = ++generation_;
        request.text = text;
        request.request_id = MakeId("req", sequence_);
        request.session_id = MakeId("session", sequence_);
        busy_ = true;
        speech_pending_ = false;
        last_error_ = AiErrorCode::None;
    }

    auto* args = new WorkerArgs{this, std::move(request), generation, force_mock};
    const auto result = xTaskCreate(&AiGatewayClient::WorkerTask, "ai_gateway", 8192, args, 2, nullptr);
    if (result != pdPASS) {
        delete args;
        Complete(generation, request, nullptr, AiErrorCode::Internal);
        return false;
    }

    tachikoma_state::GetTachikomaStateManager().Notify(tachikoma_state::TachikomaEvent::AiRequestStarted);
    mclog::tagInfo(kTag, "request started id={} session={}", request.request_id, request.session_id);
    return true;
}

void AiGatewayClient::StartDevelopmentMock()
{
#if defined(DEVELOPMENT_BUILD)
    if (IsBusy()) {
        return;
    }
    // A configured endpoint switches the boot probe to real communication;
    // without one, keep the offline mock so development remains self-testable.
    const auto config = LoadConfig();
    (void)StartRequestInternal("こんにちは", config.endpoint.empty());
#endif
}

void AiGatewayClient::WorkerTask(void* arg)
{
    auto* args = static_cast<WorkerArgs*>(arg);
    if (args != nullptr && args->client != nullptr) {
        args->client->RunWorker(args);
    }
    vTaskDelete(nullptr);
}

void AiGatewayClient::RunWorker(WorkerArgs* args)
{
    AiResponse response;
    AiErrorCode error = AiErrorCode::None;
    bool success = false;

    if (args->force_mock) {
        vTaskDelay(pdMS_TO_TICKS(kMockDelayMs));
        response.text = "こんにちは。タチコマ接続テストは成功です。";
        response.request_id = args->request.request_id;
        response.session_id = args->request.session_id;
        response.is_final = true;
        success = true;
    } else {
        const auto config = LoadConfig();
        if (config.endpoint.empty()) {
            error = AiErrorCode::NotConfigured;
        } else if (!WaitForNetworkStack()) {
            // esp_http_client_perform() on an uninitialized LWIP stack
            // hard-crashes (tcpip_send_msg_wait_sem asserts on the missing
            // mbox) instead of returning an error, so the stack must exist
            // before any HTTP attempt -- the same guard SpeechAnnouncer
            // takes before touching esp_http_client.
            error = AiErrorCode::WifiUnavailable;
        } else {
            const uint8_t attempts = static_cast<uint8_t>(config.retries + 1);
            for (uint8_t attempt = 0; attempt < attempts; ++attempt) {
                success = PerformHttp(config, args->request, response, error);
                if (success || error == AiErrorCode::AuthenticationFailed ||
                    error == AiErrorCode::InvalidResponse || error == AiErrorCode::InvalidInput ||
                    error == AiErrorCode::RateLimited) {
                    break;
                }
                if (attempt + 1 < attempts) {
                    vTaskDelay(pdMS_TO_TICKS(250 * (attempt + 1)));
                }
            }
        }
    }

    Complete(args->generation, args->request, success ? &response : nullptr, success ? AiErrorCode::None : error);
    delete args;
}

bool AiGatewayClient::PerformHttp(const AiGatewayConfig& config, const AiRequest& request,
                                  AiResponse& response, AiErrorCode& error)
{
    if (config.endpoint.rfind("http://", 0) == 0) {
#if !defined(DEVELOPMENT_BUILD)
        error = AiErrorCode::NotConfigured;
        return false;
#else
        mclog::tagWarn(kTag, "HTTP gateway is DEVELOPMENT_BUILD-only; use HTTPS in production");
#endif
    }

    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "text", request.text.c_str());
    cJSON_AddStringToObject(root, "request_id", request.request_id.c_str());
    cJSON_AddStringToObject(root, "session_id", request.session_id.c_str());
    cJSON_AddStringToObject(root, "device_id", config.device_id.c_str());
    char* payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload == nullptr) {
        error = AiErrorCode::Internal;
        return false;
    }

    HttpBuffer buffer;
    esp_http_client_config_t http_config = {};
    http_config.url = config.endpoint.c_str();
    http_config.method = HTTP_METHOD_POST;
    http_config.timeout_ms = static_cast<int>(config.response_timeout_ms);
    http_config.event_handler = HttpEvent;
    http_config.user_data = &buffer;
    if (config.endpoint.rfind("https://", 0) == 0) {
        http_config.crt_bundle_attach = esp_crt_bundle_attach;
    }
    esp_http_client_handle_t client = esp_http_client_init(&http_config);
    if (client == nullptr) {
        cJSON_free(payload);
        error = AiErrorCode::ConnectionFailed;
        return false;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    if (!config.device_token.empty()) {
        std::string auth = "Bearer " + config.device_token;
        esp_http_client_set_header(client, "Authorization", auth.c_str());
    }
    esp_http_client_set_post_field(client, payload, static_cast<int>(strlen(payload)));
    const esp_err_t result = esp_http_client_perform(client);
    const int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    cJSON_free(payload);

    if (result != ESP_OK) {
        error = (result == ESP_ERR_TIMEOUT) ? AiErrorCode::Timeout : AiErrorCode::ConnectionFailed;
        return false;
    }
    if (status == 401 || status == 403) {
        error = AiErrorCode::AuthenticationFailed;
        return false;
    }
    if (status == 408 || status == 504) {
        error = AiErrorCode::Timeout;
        return false;
    }
    if (status == 429) {
        error = AiErrorCode::RateLimited;
        return false;
    }
    if (status < 200 || status >= 300) {
        error = AiErrorCode::ServerError;
        return false;
    }

    cJSON* parsed = cJSON_ParseWithLength(buffer.body.data(), buffer.body.size());
    if (parsed == nullptr) {
        error = AiErrorCode::InvalidResponse;
        return false;
    }
    const auto* text = cJSON_GetObjectItemCaseSensitive(parsed, "text");
    const auto* request_id = cJSON_GetObjectItemCaseSensitive(parsed, "request_id");
    const auto* session_id = cJSON_GetObjectItemCaseSensitive(parsed, "session_id");
    const auto* is_final = cJSON_GetObjectItemCaseSensitive(parsed, "is_final");
    if (!cJSON_IsString(text) || text->valuestring == nullptr || strlen(text->valuestring) > kMaxResponseBytes) {
        cJSON_Delete(parsed);
        error = AiErrorCode::InvalidResponse;
        return false;
    }
    response.text = text->valuestring;
    response.request_id = cJSON_IsString(request_id) ? request_id->valuestring : request.request_id;
    response.session_id = cJSON_IsString(session_id) ? session_id->valuestring : request.session_id;
    response.is_final = !cJSON_IsFalse(is_final);
    cJSON_Delete(parsed);
    return true;
}

void AiGatewayClient::Complete(uint32_t generation, const AiRequest& request, const AiResponse* response,
                               AiErrorCode error)
{
    bool accepted = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!busy_ || generation != generation_) {
            return;
        }
        busy_ = false;
        last_error_ = error;
        if (response != nullptr && error == AiErrorCode::None) {
            last_response_ = *response;
            speech_pending_ = true;
            const uint32_t duration = std::clamp<uint32_t>(300 + static_cast<uint32_t>(response->text.size()) * 45,
                                                           kMinSpeechMs, kMaxSpeechMs);
            speech_deadline_ms_ = GetHAL().millis() + duration;
            accepted = true;
        }
    }

    auto& state = tachikoma_state::GetTachikomaStateManager();
    if (accepted) {
        mclog::tagInfo(kTag, "response ready id={} bytes={}", request.request_id, response->text.size());
        state.Notify(tachikoma_state::TachikomaEvent::AiResponseReady);
    } else {
        mclog::tagWarn(kTag, "request failed id={} error={}", request.request_id, ErrorName(error));
        state.Notify(tachikoma_state::TachikomaEvent::AiRequestFailed);
    }
}

void AiGatewayClient::Update(uint32_t now)
{
    bool finish = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (speech_pending_ && static_cast<int32_t>(now - speech_deadline_ms_) >= 0) {
            speech_pending_ = false;
            finish = true;
        }
    }
    if (finish) {
        tachikoma_state::GetTachikomaStateManager().Notify(tachikoma_state::TachikomaEvent::SpeechFinished);
    }
}

void AiGatewayClient::Stop()
{
    bool was_busy = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        was_busy = busy_ || speech_pending_;
        ++generation_;
        busy_ = false;
        speech_pending_ = false;
        last_error_ = AiErrorCode::Cancelled;
    }
    if (was_busy) {
        tachikoma_state::GetTachikomaStateManager().Notify(tachikoma_state::TachikomaEvent::Reset);
    }
}

bool AiGatewayClient::IsBusy() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return busy_ || speech_pending_;
}

AiResponse AiGatewayClient::GetLastResponse() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return last_response_;
}

AiErrorCode AiGatewayClient::GetLastError() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return last_error_;
}

}  // namespace stackchan::ai_gateway
