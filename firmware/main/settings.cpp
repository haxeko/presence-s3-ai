#include "settings.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "sdkconfig.h"

namespace app {

static const char *TAG = "settings";
static const char *NS = "presence";

static NetSettings s_net;
static RadarSettings s_radar;
static DeviceConfig s_cfg;
static SemaphoreHandle_t s_lock;
static uint32_t s_boot_count;

struct StrKey {
    const char *key;
    char *dst;
    size_t size;
    const char *def;
};

static StrKey s_str_keys[] = {
    { "wifi_ssid", s_net.wifi_ssid, sizeof(s_net.wifi_ssid), CONFIG_PRESENCE_WIFI_SSID },
    { "wifi_pass", s_net.wifi_pass, sizeof(s_net.wifi_pass), CONFIG_PRESENCE_WIFI_PASSWORD },
    { "mqtt_uri", s_net.mqtt_uri, sizeof(s_net.mqtt_uri), CONFIG_PRESENCE_MQTT_URI },
    { "mqtt_user", s_net.mqtt_user, sizeof(s_net.mqtt_user), CONFIG_PRESENCE_MQTT_USERNAME },
    { "mqtt_pass", s_net.mqtt_pass, sizeof(s_net.mqtt_pass), CONFIG_PRESENCE_MQTT_PASSWORD },
    { "server_url", s_net.server_url, sizeof(s_net.server_url), CONFIG_PRESENCE_SERVER_URL },
    { "device_token", s_net.device_token, sizeof(s_net.device_token), CONFIG_PRESENCE_DEVICE_TOKEN },
    { "topic_prefix", s_net.topic_prefix, sizeof(s_net.topic_prefix), CONFIG_PRESENCE_TOPIC_PREFIX },
};

struct IntKey {
    const char *key;
    int *dst;
    int def;
    int min;
    int max;
};

static IntKey s_int_keys[] = {
    { "rx_gpio", &s_radar.rx_gpio, CONFIG_PRESENCE_RADAR_RX_GPIO, 0, 48 },
    { "tx_gpio", &s_radar.tx_gpio, CONFIG_PRESENCE_RADAR_TX_GPIO, 0, 48 },
    { "baud", &s_radar.baud, CONFIG_PRESENCE_RADAR_BAUD, 9600, 921600 },
};

static void default_device_config(DeviceConfig *c)
{
    memset(c, 0, sizeof(*c));
    c->model_params = presence::model_presence_defaults();
    c->threshold_params = presence::threshold_presence_defaults();
    c->thresholds = presence::factory_thresholds();
}

static bool parse_params(const cJSON *o, presence::PresenceParams *p)
{
    const cJSON *en = cJSON_GetObjectItem(o, "enter");
    const cJSON *ex = cJSON_GetObjectItem(o, "exit");
    const cJSON *co = cJSON_GetObjectItem(o, "confirm_occupied_ms");
    const cJSON *ce = cJSON_GetObjectItem(o, "confirm_empty_ms");
    if (!cJSON_IsNumber(en) || !cJSON_IsNumber(ex) || !cJSON_IsNumber(co) || !cJSON_IsNumber(ce)) {
        return false;
    }
    presence::PresenceParams r{ (float)en->valuedouble, (float)ex->valuedouble, (uint32_t)co->valuedouble,
                                (uint32_t)ce->valuedouble };
    if (!r.valid()) {
        return false;
    }
    *p = r;
    return true;
}

static bool parse_gate_list(const cJSON *arr, uint8_t out[presence::kGates])
{
    if (!cJSON_IsArray(arr) || cJSON_GetArraySize(arr) != presence::kGates) {
        return false;
    }
    for (int g = 0; g < presence::kGates; g++) {
        const cJSON *v = cJSON_GetArrayItem(arr, g);
        if (!cJSON_IsNumber(v) || v->valuedouble < 0 || v->valuedouble > 100) {
            return false;
        }
        out[g] = (uint8_t)v->valuedouble;
    }
    return true;
}

static bool parse_device_config(const char *json, size_t len, DeviceConfig *out, char *err, size_t err_len)
{
    cJSON *root = cJSON_ParseWithLength(json, len);
    if (!root) {
        snprintf(err, err_len, "invalid json");
        return false;
    }
    DeviceConfig c;
    default_device_config(&c);
    bool ok = true;
    const cJSON *rev = cJSON_GetObjectItem(root, "rev");
    c.rev = cJSON_IsNumber(rev) ? (uint32_t)rev->valuedouble : 0;
    const cJSON *pm = cJSON_GetObjectItem(root, "presence_model");
    if (pm && !cJSON_IsNull(pm)) {
        ok = ok && parse_params(pm, &c.model_params);
        c.model_params_override = true;
    }
    const cJSON *pt = cJSON_GetObjectItem(root, "presence_threshold");
    if (pt && !cJSON_IsNull(pt)) {
        ok = ok && parse_params(pt, &c.threshold_params);
    }
    const cJSON *thr = cJSON_GetObjectItem(root, "thresholds");
    if (thr && !cJSON_IsNull(thr)) {
        ok = ok && parse_gate_list(cJSON_GetObjectItem(thr, "moving"), c.thresholds.moving) &&
             parse_gate_list(cJSON_GetObjectItem(thr, "still"), c.thresholds.still);
    }
    const cJSON *bl = cJSON_GetObjectItem(root, "baseline");
    if (bl && cJSON_IsObject(bl)) {
        const cJSON *id = cJSON_GetObjectItem(bl, "id");
        c.baseline = true;
        c.baseline_id = cJSON_IsNumber(id) ? (uint32_t)id->valuedouble : 0;
    }
    c.prefer_threshold = cJSON_IsTrue(cJSON_GetObjectItem(root, "prefer_threshold"));
    cJSON_Delete(root);
    if (!ok) {
        snprintf(err, err_len, "invalid presence parameters or thresholds");
        return false;
    }
    *out = c;
    return true;
}

void settings_init()
{
    s_lock = xSemaphoreCreateMutex();
    s_radar.uart_num = CONFIG_PRESENCE_RADAR_UART_NUM;
    for (StrKey &k : s_str_keys) {
        snprintf(k.dst, k.size, "%s", k.def);
    }
    for (IntKey &k : s_int_keys) {
        *k.dst = k.def;
    }
    default_device_config(&s_cfg);
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "NVS unavailable: Kconfig defaults only");
        return;
    }
    for (StrKey &k : s_str_keys) {
        size_t n = k.size;
        nvs_get_str(h, k.key, k.dst, &n); // missing key: default stays
    }
    for (IntKey &k : s_int_keys) {
        int32_t v;
        if (nvs_get_i32(h, k.key, &v) == ESP_OK && v >= k.min && v <= k.max) {
            *k.dst = v;
        }
    }
    size_t n = 0;
    if (nvs_get_str(h, "dcfg", nullptr, &n) == ESP_OK && n > 1 && n < 4096) {
        char *buf = (char *)malloc(n);
        char err[48];
        if (buf && nvs_get_str(h, "dcfg", buf, &n) == ESP_OK && !parse_device_config(buf, n - 1, &s_cfg, err, sizeof(err))) {
            ESP_LOGW(TAG, "stored device config ignored: %s", err);
            default_device_config(&s_cfg);
        }
        free(buf);
    }
    // Boot counter: one NVS write per boot
    uint32_t bc = 0;
    nvs_get_u32(h, "boots", &bc);
    s_boot_count = bc + 1;
    nvs_set_u32(h, "boots", s_boot_count);
    nvs_commit(h);
    nvs_close(h);
}

const NetSettings &net_settings()
{
    return s_net;
}

const RadarSettings &radar_settings()
{
    return s_radar;
}

uint32_t boot_count()
{
    return s_boot_count;
}

bool settings_set(const char *key, const char *value)
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = false;
    for (StrKey &k : s_str_keys) {
        if (strcmp(k.key, key) == 0 && strlen(value) < k.size) {
            ok = nvs_set_str(h, k.key, value) == ESP_OK;
        }
    }
    for (IntKey &k : s_int_keys) {
        if (strcmp(k.key, key) == 0) {
            char *end = nullptr;
            const long v = strtol(value, &end, 10);
            if (end && *end == '\0' && v >= k.min && v <= k.max) {
                ok = nvs_set_i32(h, k.key, (int32_t)v) == ESP_OK;
            }
        }
    }
    ok = ok && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

bool settings_clear()
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    for (StrKey &k : s_str_keys) {
        nvs_erase_key(h, k.key);
    }
    for (IntKey &k : s_int_keys) {
        nvs_erase_key(h, k.key);
    }
    nvs_erase_key(h, "dcfg");
    const bool ok = nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

void settings_print()
{
    for (const StrKey &k : s_str_keys) {
        const bool secret = strstr(k.key, "pass") || strstr(k.key, "token");
        printf("  %-13s %s\n", k.key, secret ? (k.dst[0] ? "(set)" : "(empty)") : k.dst);
    }
    for (const IntKey &k : s_int_keys) {
        printf("  %-13s %d\n", k.key, *k.dst);
    }
    printf("  uart          UART%d\n", s_radar.uart_num);
}

DeviceConfig device_config()
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const DeviceConfig c = s_cfg;
    xSemaphoreGive(s_lock);
    return c;
}

bool device_config_apply_json(const char *json, size_t len, char *err, size_t err_len)
{
    DeviceConfig c;
    if (!parse_device_config(json, len, &c, err, err_len)) {
        return false;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const bool changed = c.rev != s_cfg.rev || memcmp(&c, &s_cfg, sizeof(c)) != 0;
    s_cfg = c;
    xSemaphoreGive(s_lock);
    if (changed && len < 4000) {
        // Stored so the configuration also applies after a restart without network
        nvs_handle_t h;
        if (nvs_open(NS, NVS_READWRITE, &h) == ESP_OK) {
            char *copy = (char *)malloc(len + 1);
            if (copy) {
                memcpy(copy, json, len);
                copy[len] = '\0';
                nvs_set_str(h, "dcfg", copy);
                nvs_commit(h);
                free(copy);
            }
            nvs_close(h);
        }
        ESP_LOGI(TAG, "device config rev %u applied", (unsigned)c.rev);
    }
    return true;
}

} // namespace app
