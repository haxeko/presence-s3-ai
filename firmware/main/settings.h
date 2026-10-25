// Runtime settings: Kconfig defaults, overridden by values stored in NVS (serial console "config set", or the
// backend's desired configuration). NVS is written only when a value changes, never per sample.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "pipeline.h"

namespace app {

struct NetSettings {
    char wifi_ssid[33];
    char wifi_pass[65];
    char mqtt_uri[128];
    char mqtt_user[64];
    char mqtt_pass[64];
    char server_url[128];
    char device_token[96];
    char topic_prefix[48];
};

struct RadarSettings {
    int uart_num;
    int rx_gpio;
    int tx_gpio;
    int baud;
};

// Desired configuration from the backend (retained MQTT presence/v1/dev/<id>/desired/config)
struct DeviceConfig {
    uint32_t rev;
    bool model_params_override; // false: the active model's recommended parameters
    presence::PresenceParams model_params;
    presence::PresenceParams threshold_params;
    presence::Thresholds thresholds;
    bool baseline;
    uint32_t baseline_id;
    bool prefer_threshold;
};

void settings_init();
const NetSettings &net_settings();
const RadarSettings &radar_settings();

// Console keys: wifi_ssid wifi_pass mqtt_uri mqtt_user mqtt_pass server_url device_token topic_prefix rx_gpio
// tx_gpio baud. Takes effect after a restart.
bool settings_set(const char *key, const char *value);
bool settings_clear();
void settings_print();

DeviceConfig device_config();
// Parses the desired/config JSON; stores it in NVS when its revision changed. Returns false when invalid.
bool device_config_apply_json(const char *json, size_t len, char *err, size_t err_len);
uint32_t boot_count();

} // namespace app
