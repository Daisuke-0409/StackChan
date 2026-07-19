/*
 * SPDX-License-Identifier: MIT
 *
 * Provider-neutral client for the Tachikoma Gateway.  The ESP32 owns only
 * request/session identifiers; conversation history remains on the gateway.
 */
#pragma once

#include <cstdint>
#include <mutex>
#include <string>

namespace stackchan::ai_gateway {

enum class AiErrorCode : uint8_t {
    None,
    InvalidInput,
    Busy,
    NotConfigured,
    WifiUnavailable,
    ConnectionFailed,
    Timeout,
    AuthenticationFailed,
    RateLimited,
    ServerError,
    InvalidResponse,
    Cancelled,
    Internal,
};

struct AiRequest {
    std::string text;
    std::string request_id;
    std::string session_id;
};

struct AiResponse {
    std::string text;
    std::string request_id;
    std::string session_id;
    bool is_final = true;
};

struct AiGatewayConfig {
    std::string endpoint;
    std::string device_token;
    std::string device_id;
    uint32_t connect_timeout_ms = 10000;
    uint32_t response_timeout_ms = 30000;
    uint8_t retries = 1;
};

class AiGatewayClient {
public:
    // Stores endpoint/token in the dedicated NVS namespace. Values are never logged.
    bool ConfigureGateway(const std::string& endpoint, const std::string& device_token,
                          const std::string& device_id = {});
    bool StartRequest(const std::string& text);
    bool SubmitText(const std::string& text) { return StartRequest(text); }
    void Update(uint32_t now);
    void Stop();
    void StartDevelopmentMock();

    bool IsBusy() const;
    AiResponse GetLastResponse() const;
    AiErrorCode GetLastError() const;

private:
    struct WorkerArgs {
        AiGatewayClient* client;
        AiRequest request;
        uint32_t generation;
        bool force_mock;
    };

    bool StartRequestInternal(const std::string& text, bool force_mock);
    static void WorkerTask(void* arg);
    void RunWorker(WorkerArgs* args);
    bool PerformHttp(const AiGatewayConfig& config, const AiRequest& request, AiResponse& response,
                     AiErrorCode& error);
    void Complete(uint32_t generation, const AiRequest& request, const AiResponse* response,
                  AiErrorCode error);
    AiGatewayConfig LoadConfig() const;
    static std::string MakeId(const char* prefix, uint32_t sequence);

    mutable std::mutex mutex_;
    bool busy_ = false;
    bool speech_pending_ = false;
    uint32_t speech_deadline_ms_ = 0;
    uint32_t sequence_ = 0;
    uint32_t generation_ = 0;
    AiResponse last_response_;
    AiErrorCode last_error_ = AiErrorCode::None;
};

AiGatewayClient& GetAiGatewayClient();

}  // namespace stackchan::ai_gateway
