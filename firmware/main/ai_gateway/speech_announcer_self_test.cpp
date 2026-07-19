/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "speech_announcer_self_test.h"

#include "speech_announcer.h"
#include <esp_log.h>

namespace stackchan::ai_gateway {
namespace {

constexpr const char* kTag = "SpeechAnnouncerTest";

bool Expect(bool condition, const char* name)
{
    if (!condition) {
        ESP_LOGE(kTag, "FAIL: %s", name);
        return false;
    }
    return true;
}

}  // namespace

bool RunSpeechAnnouncerSelfTest()
{
    bool passed = true;

    // Regression coverage for the FetchAndPlay() network-availability guard:
    // a 0 esp_netif count (no WiFi/LWIP ever brought up, e.g. boot_ai=0) must
    // be treated as "not ready" so the caller never reaches
    // esp_http_client_perform(), which hard-asserts in that state.
    passed &= Expect(!IsNetworkStackReady(0), "0 interfaces -> not ready");
    passed &= Expect(IsNetworkStackReady(1), "1 interface -> ready");
    passed &= Expect(IsNetworkStackReady(2), "2 interfaces -> ready");

    passed &= Expect(SpeechAnnounceErrorCode::NetworkUnavailable != SpeechAnnounceErrorCode::None,
                     "NetworkUnavailable distinct from None");
    passed &= Expect(SpeechAnnounceErrorCode::NetworkUnavailable != SpeechAnnounceErrorCode::NotConfigured,
                     "NetworkUnavailable distinct from NotConfigured");

    ESP_LOGI(kTag, "Self-test %s", passed ? "PASS" : "FAIL");
    return passed;
}

}  // namespace stackchan::ai_gateway
