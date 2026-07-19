/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "tachikoma_state_self_test.h"

#include "tachikoma_state_machine.h"
#include <esp_log.h>

namespace stackchan::tachikoma_state {
namespace {

constexpr const char* kTag = "TachikomaStateTest";

bool Expect(bool condition, const char* name)
{
    if (!condition) {
        ESP_LOGE(kTag, "FAIL: %s", name);
        return false;
    }
    return true;
}

}  // namespace

bool RunTachikomaStateSelfTest()
{
    bool passed = true;
    TachikomaStateMachine machine;
    uint32_t now = 100;

    passed &= Expect(machine.Initialize(now).current_state == TachikomaState::Booting, "initialize -> Booting");
    passed &= Expect(machine.Initialize(now).accepted, "initialize is idempotent");
    passed &= Expect(machine.HandleEvent(TachikomaEvent::InitializationComplete, ++now).current_state ==
                         TachikomaState::Idle,
                     "Booting -> Idle");
    passed &= Expect(machine.HandleEvent(TachikomaEvent::UserSpeechStarted, ++now).current_state ==
                         TachikomaState::Listening,
                     "Idle -> Listening");
    passed &= Expect(machine.HandleEvent(TachikomaEvent::UserSpeechEnded, ++now).current_state ==
                         TachikomaState::Thinking,
                     "Listening -> Thinking");
    passed &= Expect(machine.HandleEvent(TachikomaEvent::AiResponseReady, ++now).current_state ==
                         TachikomaState::Speaking,
                     "Thinking -> Speaking");
    passed &= Expect(machine.HandleEvent(TachikomaEvent::SpeechFinished, ++now).current_state ==
                         TachikomaState::Idle,
                     "Speaking -> Idle");

    auto reaction = machine.HandleEvent(TachikomaEvent::HappyRequested, ++now);
    passed &= Expect(reaction.current_state == TachikomaState::Reacting &&
                         reaction.reaction == TachikomaReaction::Happy,
                     "Happy reaction starts");
    passed &= Expect(machine.HandleEvent(TachikomaEvent::ReactionFinished, ++now).current_state ==
                         TachikomaState::Idle,
                     "Happy returns to Idle");

    machine.HandleEvent(TachikomaEvent::UserSpeechStarted, ++now);
    reaction = machine.HandleEvent(TachikomaEvent::RecognitionFailed, ++now);
    passed &= Expect(reaction.current_state == TachikomaState::Reacting &&
                         reaction.reaction == TachikomaReaction::Confused,
                     "Recognition failure starts Confused");
    passed &= Expect(machine.HandleEvent(TachikomaEvent::ReactionFinished, ++now).current_state ==
                         TachikomaState::Listening,
                     "Confused returns to prior state");

    const auto invalid = machine.HandleEvent(TachikomaEvent::WakeRequested, ++now);
    passed &= Expect(!invalid.accepted && invalid.current_state == TachikomaState::Listening,
                     "invalid transition rejected");

    TachikomaStateTimeouts timeouts;
    timeouts.listening_ms = 10;
    machine.SetTimeouts(timeouts);
    const auto timeout = machine.ApplyTimeout(now + 10);
    passed &= Expect(timeout.accepted && timeout.current_state == TachikomaState::Idle,
                     "Listening timeout -> Idle");

    const auto sleep = machine.HandleEvent(TachikomaEvent::SleepRequested, ++now);
    passed &= Expect(sleep.accepted && sleep.current_state == TachikomaState::Sleeping,
                     "Idle -> Sleeping");
    const auto wake = machine.HandleEvent(TachikomaEvent::WakeRequested, ++now);
    passed &= Expect(wake.accepted && wake.current_state == TachikomaState::Idle,
                     "Sleeping -> Idle");

    machine.HandleEvent(TachikomaEvent::HappyRequested, ++now);
    const auto reset = machine.HandleEvent(TachikomaEvent::Reset, ++now);
    passed &= Expect(reset.accepted && reset.current_state == TachikomaState::Idle && reset.force_motion_reset,
                     "Reset interrupts reaction");

    machine.Stop();
    machine.Stop();
    passed &= Expect(!machine.GetSnapshot().running, "Stop is idempotent");
    passed &= Expect(machine.Initialize(++now).accepted && machine.GetSnapshot().running,
                     "Initialize after Stop");

    ESP_LOGI(kTag, "Self-test %s", passed ? "PASS" : "FAIL");
    return passed;
}

}  // namespace stackchan::tachikoma_state
