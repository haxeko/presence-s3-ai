// Presence S3 AI: ESP32-S3 + HLK-LD2410 presence sensor with on-device INT8 model (TensorFlow Lite Micro).
//
// Tasks (core 1 = sensing, core 0 = network):
//   radar_rx   (prio 12, core 1)  UART -> LD2410 parser -> 5 Hz bins
//   esp_timer  tick every 200 ms   closes the bin, queues it to the decision task
//   decision   (prio 8, core 1)   window -> model or threshold method -> presence logic -> offline buffer
//   radar_ctl  (prio 5, core 1)   reads the radar setup, engineering mode, re-init when the radar goes silent
//   net        (prio 4, core 0)   MQTT samples/status/state, desired config and model
//   model_upd  (prio 3, core 0)   download + verify + self-test + activate a model package
#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "console.h"
#include "decision.h"
#include "esp_app_desc.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_memory_utils.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "model_update.h"
#include "net.h"
#include "nvs_flash.h"
#include "radar.h"
#include "sdkconfig.h"
#include "settings.h"

static const char *TAG = "app";

namespace app {

Shared g;

uint32_t uptime_ms()
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

const char *radar_status_name(RadarStatus s)
{
    switch (s) {
    case RadarStatus::Init:
        return "init";
    case RadarStatus::Ok:
        return "ok";
    case RadarStatus::NoData:
        return "no_data";
    case RadarStatus::NotEngineering:
        return "not_engineering";
    case RadarStatus::BadFrames:
        return "bad_frames";
    case RadarStatus::Layout:
        return "unsupported_layout";
    }
    return "?";
}

static void init_shared()
{
    g.lock = xSemaphoreCreateMutex();
    g.ring_lock = xSemaphoreCreateMutex();
    g.model_lock = xSemaphoreCreateRecursiveMutex();
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(g.device_id, sizeof(g.device_id), "s3-%02x%02x%02x", mac[3], mac[4], mac[5]);
    snprintf(g.fw_version, sizeof(g.fw_version), "%s", esp_app_get_description()->version);
    // Offline buffer: PSRAM when present, internal RAM otherwise; never flash
    const size_t n = CONFIG_PRESENCE_BUFFER_SAMPLES;
    presence::Sample *storage = (presence::Sample *)heap_caps_malloc(n * sizeof(presence::Sample),
                                                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    size_t cap = n;
    if (!storage) {
        cap = n > 1000 ? 1000 : n; // no PSRAM: keep internal RAM for Wi-Fi/TLS
        storage = (presence::Sample *)heap_caps_malloc(cap * sizeof(presence::Sample), MALLOC_CAP_8BIT);
    }
    g.ring = new presence::SampleRing(storage, storage ? cap : 0);
    ESP_LOGI(TAG, "offline buffer %u samples (%u bytes, %s)", (unsigned)cap, (unsigned)(cap * sizeof(presence::Sample)),
             storage && esp_ptr_external_ram(storage) ? "PSRAM" : "internal RAM");
}

} // namespace app

extern "C" void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    app::settings_init();
    app::init_shared();
    ESP_LOGI(TAG, "Presence S3 AI %s, device %s, boot %u", app::g.fw_version, app::g.device_id,
             (unsigned)app::boot_count());

    app::model_update_create();
    QueueHandle_t ticks = app::decision_start();
    ESP_ERROR_CHECK(app::radar_start(ticks));
    app::model_update_boot(); // after the radar reported its gate length (max. 3 s)
    app::net_start();
    app::console_start();
}
