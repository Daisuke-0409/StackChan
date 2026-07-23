/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "hal.h"
#include <memory>
#include <mooncake_log.h>
#include <nvs_flash.h>
#include <settings.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <stackchan/state/tachikoma_state_manager.h>
#include <ai_gateway/ai_gateway_client.h>
#include <ai_gateway/speech_announcer.h>
#include <stackchan/voice_input/voice_input_controller.h>
#if defined(DEVELOPMENT_BUILD)
#include <ai_gateway/speech_announcer_self_test.h>
#include <stackchan/voice_input/voice_input_controller_self_test.h>
#endif

static std::unique_ptr<Hal> _hal_instance;
static const std::string_view _tag = "HAL";
static bool _stackchan_update_task_started = false;

static void _stackchan_update_task(void* param);

Hal& GetHAL()
{
    if (!_hal_instance) {
        mclog::tagInfo(_tag, "creating hal instance");
        _hal_instance = std::make_unique<Hal>();
    }
    return *_hal_instance.get();
}

void Hal::init()
{
    mclog::tagInfo(_tag, "init");

    // Initialize NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    xiaozhi_board_init();
    xiaozhi_mcp_init();
    head_touch_init();
    // Push-to-talk trigger: Si12T head Press/Release starts/stops recording.
    // Not DEVELOPMENT_BUILD-gated -- this is the real production trigger.
    stackchan::voice_input::GetVoiceInputController().ConnectHeadTouchTrigger();
    io_expander_init();
    rtc_init();
    imu_init();
    servo_init();
    lvgl_init();
    // StateManager lifecycle is owned by HAL. AI.AGENT only sends runtime events.
    auto& state_manager = stackchan::tachikoma_state::GetTachikomaStateManager();
    state_manager.Initialize(millis());
    state_manager.Notify(stackchan::tachikoma_state::TachikomaEvent::InitializationComplete);
    {
        // Process the HAL-owned initialization event before AI.AGENT is started.
        LvglLockGuard lock;
        state_manager.Update(millis());
    }

    // Reuse the existing lightweight update task for state, display, and servo updates.
    // It is started here so StateManager is not gated by AI.AGENT startup.
    if (!_stackchan_update_task_started) {
        const auto result = xTaskCreatePinnedToCore(_stackchan_update_task, "stackchan", 4096, NULL, 3, NULL, 1);
        if (result == pdPASS) {
            _stackchan_update_task_started = true;
        } else {
            mclog::tagError(_tag, "failed to start StackChan update task");
        }
    }

#if defined(DEVELOPMENT_BUILD)
    // Optional local build-time provisioning writes credentials to NVS once.
#if defined(TACHIKOMA_GATEWAY_URL) && defined(TACHIKOMA_DEVICE_TOKEN)
    stackchan::ai_gateway::GetAiGatewayClient().ConfigureGateway(TACHIKOMA_GATEWAY_URL,
                                                                  TACHIKOMA_DEVICE_TOKEN);
#endif
#if defined(TACHIKOMA_SPEAK_QUEUE_URL) && defined(TACHIKOMA_DEVICE_TOKEN)
    // Shares TACHIKOMA_DEVICE_TOKEN with the gateway above: the Tachikoma
    // Gateway server checks every endpoint against one DEVICE_TOKEN env var.
    stackchan::ai_gateway::GetSpeechAnnouncer().ConfigureSpeechQueue(TACHIKOMA_SPEAK_QUEUE_URL,
                                                                      TACHIKOMA_DEVICE_TOKEN);
#endif
#if defined(TACHIKOMA_TRANSCRIBE_QUEUE_URL) && defined(TACHIKOMA_DEVICE_TOKEN)
    // Shares TACHIKOMA_DEVICE_TOKEN with the gateway above: the Tachikoma
    // Gateway server checks every endpoint against one DEVICE_TOKEN env var.
    stackchan::voice_input::GetVoiceInputController().ConfigureTranscribeQueue(TACHIKOMA_TRANSCRIBE_QUEUE_URL,
                                                                                TACHIKOMA_DEVICE_TOKEN);
#endif
    // Development-only offline request proves the AI path without requiring
    // credentials or a network. Production builds never start this mock.
    stackchan::ai_gateway::GetAiGatewayClient().StartDevelopmentMock();

    static bool speech_announcer_self_test_ran = false;
    if (!speech_announcer_self_test_ran) {
        speech_announcer_self_test_ran = true;
        stackchan::ai_gateway::RunSpeechAnnouncerSelfTest();
    }

    static bool voice_input_self_test_ran = false;
    if (!voice_input_self_test_ran) {
        voice_input_self_test_ran = true;
        stackchan::voice_input::RunVoiceInputControllerSelfTest();
    }
#endif
}

/* -------------------------------------------------------------------------- */
/*                                   System                                   */
/* -------------------------------------------------------------------------- */
#include <system_info.h>
#include <esp_ota_ops.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <esp_mac.h>

void Hal::delay(std::uint32_t ms)
{
    vTaskDelay(pdMS_TO_TICKS(ms));
}

std::uint32_t Hal::millis()
{
    return esp_timer_get_time() / 1000;
}

void Hal::feedTheDog()
{
    vTaskDelay(1);
}

std::array<uint8_t, 6> Hal::getFactoryMac()
{
    std::array<uint8_t, 6> mac;
    esp_efuse_mac_get_default(mac.data());
    return mac;
}

std::string Hal::getFactoryMacString(std::string divider)
{
    auto mac = getFactoryMac();
    return fmt::format("{:02X}{}{:02X}{}{:02X}{}{:02X}{}{:02X}{}{:02X}", mac[0], divider, mac[1], divider, mac[2],
                       divider, mac[3], divider, mac[4], divider, mac[5]);
}

void Hal::reboot()
{
    esp_restart();
}

static void _confirm_ota_image_if_stable()
{
    constexpr uint32_t ota_confirm_delay_ms = 20000;
    static bool ota_confirm_checked         = false;
    if (ota_confirm_checked || GetHAL().millis() < ota_confirm_delay_ms) {
        return;
    }
    ota_confirm_checked = true;

    const esp_partition_t* running = esp_ota_get_running_partition();
    if (running == nullptr) {
        mclog::tagError(_tag, "failed to get running partition for ota confirmation");
        return;
    }

    esp_ota_img_states_t ota_state;
    if (esp_ota_get_state_partition(running, &ota_state) != ESP_OK) {
        mclog::tagError(_tag, "failed to get ota state for partition: {}", running->label);
        return;
    }

    mclog::tagInfo(_tag, "ota confirm check: partition={}, state={}", running->label, static_cast<int>(ota_state));
    if (ota_state == ESP_OTA_IMG_PENDING_VERIFY) {
        mclog::tagInfo(_tag, "ota image is stable, marking current app valid");
        esp_ota_mark_app_valid_cancel_rollback();
    }
}

void Hal::updateHeapStatusLog()
{
    _confirm_ota_image_if_stable();

    static uint32_t last_log_tick = 0;
    if (millis() - last_log_tick < 10000) {
        return;
    }
    last_log_tick = millis();
    SystemInfo::PrintHeapStats();
}

/* -------------------------------------------------------------------------- */
/*                                   Xiaozhi                                  */
/* -------------------------------------------------------------------------- */
#include "board/hal_bridge.h"
#include <stackchan/stackchan.h>
#include <apps/common/common.h>
#include <assets/assets.h>

void Hal::xiaozhi_board_init()
{
    mclog::tagInfo(_tag, "xiaozhi board init");

    hal_bridge::xiaozhi_board_init();
}

static void _stackchan_update_task(void* param)
{
    bool is_setup_done = false;
    auto& state_manager = stackchan::tachikoma_state::GetTachikomaStateManager();

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(20));

        tools::update_reminders();

        LvglLockGuard lock;

        if (!hal_bridge::is_xiaozhi_idle()) {
            vTaskDelay(pdMS_TO_TICKS(100));
        }

        const auto now = GetHAL().millis();
        state_manager.Update(now);
        stackchan::ai_gateway::GetAiGatewayClient().Update(now);
        stackchan::ai_gateway::GetSpeechAnnouncer().Update(now);
        stackchan::voice_input::GetVoiceInputController().Update(now);
        hal_bridge::update_tachikoma_motion();
        GetStackChan().update();

        if (!hal_bridge::is_xiaozhi_ready()) {
            continue;
        }

        if (!is_setup_done) {
            // Setup when xiaozhi ready
            GetHAL().startSntp();
            view::create_home_indicator([]() { GetHAL().requestWarmReboot(0); }, 0x81DBBD, 0x134233);
            view::create_status_bar(0x81DBBD, 0x134233);
            is_setup_done = true;
        }

        view::update_home_indicator();
        view::update_status_bar();
    }
}

void Hal::startXiaozhi()
{
    mclog::tagInfo(_tag, "start xiaozhi");

#ifdef TACHIKOMA_DISABLE_XIAOZHI_CLOUD
    // Factory NVS ships live xiaozhi-cloud credentials (MQTT broker login,
    // WebSocket token). Cloud protocol init is compiled out, so nothing
    // reads these anymore; erase them so third-party credentials don't
    // linger on the device. Idempotent, and touches only these two
    // namespaces -- wifi, servo calibration, and tachi_* are unaffected.
    for (const char* ns : {"mqtt", "websocket"}) {
        Settings settings(ns, true);
        settings.EraseAll();
        // Settings::EraseAll() (vendored settings.cc) never sets the
        // dirty_ flag, so ~Settings() skips nvs_commit() and the erase is
        // silently lost on close -- confirmed by re-reading NVS after a
        // real erase, which came back byte-identical. Forcing one write
        // marks the handle dirty so the destructor actually commits.
        settings.SetBool("erased", true);
    }
#endif

    auto& motion = GetStackChan().motion();
    motion.setAutoAngleSyncEnabled(true);
    motion.setAutoTorqueReleaseEnabled(true);

    // Setup reminder handler
    tools::on_reminder_triggered().clear();
    tools::on_reminder_triggered().connect([](int id, std::string_view msg) {
        mclog::tagInfo(_tag, "reminder triggered: id: {}, msg: {}", id, msg);
        {
            LvglLockGuard lock;
            auto& avatar = GetStackChan().avatar();
            avatar.addDecorator(std::make_unique<view::ReminderView>(lv_screen_active(), msg));
        }
        hal_bridge::app_play_sound(OGG_NEW_NOTIFICATION);
    });

    hal_bridge::start_xiaozhi_app();
}

XiaozhiConfig_t Hal::getXiaozhiConfig()
{
    auto bridge_config = hal_bridge::get_xiaozhi_config();
    return XiaozhiConfig_t{
        .idleShutdownTimeSeconds   = bridge_config.idleShutdownTimeSeconds,
        .allowShutdownWhenCharging = bridge_config.allowShutdownWhenCharging,
        .idleRandomMovementLevel   = bridge_config.idleRandomMovementLevel,
        .startAiAgentOnBoot        = bridge_config.startAiAgentOnBoot,
    };
}

void Hal::setXiaozhiConfig(XiaozhiConfig_t config)
{
    hal_bridge::set_xiaozhi_config({
        .idleShutdownTimeSeconds   = config.idleShutdownTimeSeconds,
        .allowShutdownWhenCharging = config.allowShutdownWhenCharging,
        .idleRandomMovementLevel   = config.idleRandomMovementLevel,
        .startAiAgentOnBoot        = config.startAiAgentOnBoot,
    });
}

uint8_t Hal::getBatteryLevel()
{
    return hal_bridge::board_get_battery_level();
}

bool Hal::isBatteryCharging()
{
    return hal_bridge::board_is_battery_charging();
}

void Hal::factoryReset()
{
    mclog::tagInfo(_tag, "start factory reset");
    ESP_ERROR_CHECK(nvs_flash_erase());
    reboot();
}

/* -------------------------------------------------------------------------- */
/*                                   Display                                  */
/* -------------------------------------------------------------------------- */
#include "board/hal_bridge.h"

void Hal::lvglLock()
{
    hal_bridge::disply_lvgl_lock();
}

void Hal::lvglUnlock()
{
    hal_bridge::disply_lvgl_unlock();
}

void Hal::setBackLightBrightness(uint8_t brightness, bool permanent)
{
    hal_bridge::board_set_backlight_brightness(brightness, permanent);
}

uint8_t Hal::getBackLightBrightness()
{
    return hal_bridge::board_get_backlight_brightness();
}

void Hal::setSpeakerVolume(uint8_t volume, bool permanent)
{
    hal_bridge::board_set_speaker_volume(volume, permanent);
}

uint8_t Hal::getSpeakerVolume()
{
    return hal_bridge::board_get_speaker_volume();
}

/* -------------------------------------------------------------------------- */
/*                                    Lvgl                                    */
/* -------------------------------------------------------------------------- */
#include "board/hal_bridge.h"
#include <stackchan/stackchan.h>

static void lvgl_read_cb(lv_indev_t* indev, lv_indev_data_t* data)
{
    hal_bridge::lock();
    auto& bridge_data = hal_bridge::get_data();

    // mclog::tagInfo(_tag, "touchpoint: {}, x: {}, y: {}", bridge_data.touchPoint.num, bridge_data.touchPoint.x,
    //                bridge_data.touchPoint.y);

    if (bridge_data.touchPoint.num == 0) {
        data->state = LV_INDEV_STATE_RELEASED;
    } else {
        data->state   = LV_INDEV_STATE_PRESSED;
        data->point.x = bridge_data.touchPoint.x;
        data->point.y = bridge_data.touchPoint.y;
    }

    hal_bridge::unlock();
}

void Hal::lvgl_init()
{
    mclog::tagInfo(_tag, "lvgl init");

    hal_bridge::disply_lvgl_lock();

    mclog::tagInfo(_tag, "create lvgl touchpad indev");
    lvTouchpad = lv_indev_create();
    lv_indev_set_type(lvTouchpad, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(lvTouchpad, lvgl_read_cb);
    lv_indev_set_group(lvTouchpad, lv_group_get_default());
    lv_indev_set_display(lvTouchpad, hal_bridge::display_get_lvgl_display());

    hal_bridge::disply_lvgl_unlock();
}

/* -------------------------------------------------------------------------- */
/*                                 Warm Reboot                                */
/* -------------------------------------------------------------------------- */
#include <settings.h>
#include <string_view>

static std::string_view _warm_boot_nvs_ns  = "warm_boot";
static std::string_view _warm_boot_nvs_key = "app_index";

void Hal::requestWarmReboot(int appIndex)
{
    mclog::tagInfo(_tag, "warm reboot request to app index: {}", appIndex);

    {
        Settings settings(_warm_boot_nvs_ns.data(), true);
        settings.SetInt(_warm_boot_nvs_key.data(), appIndex);
    }

    delay(100);
    esp_restart();
}

int Hal::getWarmRebootTarget()
{
    Settings settings(_warm_boot_nvs_ns.data(), false);
    return settings.GetInt(_warm_boot_nvs_key.data(), -1);
}

void Hal::clearWarmRebootRequest()
{
    mclog::tagInfo(_tag, "clear warm reboot request");

    Settings settings(_warm_boot_nvs_ns.data(), true);
    settings.SetInt(_warm_boot_nvs_key.data(), -1);
}
