#include "net.h"

#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "app_state.h"
#include "decision.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "model_update.h"
#include "mqtt_client.h"
#include "radar.h"
#include "sample_codec.h"
#include "sdkconfig.h"
#include "settings.h"
#include "status.h"
#include "tls_trust.h"
#include "web_config.h"

#if CONFIG_PRESENCE_NET_QEMU_OPENETH
#include "esp_eth.h"
#else
#include "esp_wifi.h"
#endif
#if CONFIG_PRESENCE_TLS_USE_CRT_BUNDLE
#include "esp_crt_bundle.h"
#endif

namespace app {

static const char *TAG = "net";

static esp_mqtt_client_handle_t s_mqtt;
static volatile bool s_ip;
static volatile bool s_mqtt_connected;
static volatile bool s_status_requested = true;
static volatile bool s_ha_discovery_pending;
static char s_ip_text[16] = "";
static char s_prefix[96];

// Batches published but not yet acknowledged. Owned by the network task only: the MQTT event handler forwards
// PUBACKs through a queue (no lock shared with esp_mqtt_client_publish, which could deadlock).
struct InFlight {
    int msg_id;
    uint32_t last_seq;
    int64_t sent_ms;
};
static constexpr int kMaxInFlight = 4;
static InFlight s_inflight[kMaxInFlight];
static int s_inflight_n;
static uint32_t s_next_seq; // first sample not sent yet
static bool s_next_seq_valid;
static QueueHandle_t s_acks;         // msg_id of acknowledged publishes
static volatile bool s_reset_tx;     // connection changed: resend whatever is still buffered
#if !CONFIG_PRESENCE_NET_QEMU_OPENETH
static esp_timer_handle_t s_wifi_retry;
#endif

bool net_mqtt_connected()
{
    return s_mqtt_connected;
}

bool net_time_synced()
{
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    return tv.tv_sec > 1700000000; // after 2023: SNTP answered
}

int net_rssi()
{
#if CONFIG_PRESENCE_NET_QEMU_OPENETH
    return 0;
#else
    wifi_ap_record_t ap;
    return esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
#endif
}

void net_ip(char *buf, size_t len)
{
    snprintf(buf, len, "%s", s_ip_text);
}

void net_request_status()
{
    s_status_requested = true;
}

bool net_publish(const char *suffix, const char *json, bool retain)
{
    if (!s_mqtt_connected) {
        return false;
    }
    char topic[160];
    snprintf(topic, sizeof(topic), "%s/%s", s_prefix, suffix);
    return esp_mqtt_client_publish(s_mqtt, topic, json, 0, 1, retain ? 1 : 0) >= 0;
}

#if CONFIG_PRESENCE_HA_DISCOVERY
// Home Assistant MQTT Discovery: the device appears by itself (retained config messages, sent on every connect).
// Entities read the device's own retained topics; availability follows the status topic and its LWT.
static bool publish_ha_discovery()
{
    struct Entity {
        const char *component;
        const char *object;
        const char *body;
    };
    static const Entity kEntities[] = {
        { "binary_sensor", "occupancy",
          "\"name\":\"Có người\",\"dev_cla\":\"occupancy\",\"stat_t\":\"~/state\","
          "\"val_tpl\":\"{{ 'ON' if value_json.decision == 'occupied' else 'OFF' }}\"" },
        { "sensor", "state",
          "\"name\":\"Trạng thái\",\"icon\":\"mdi:account-question\",\"stat_t\":\"~/state\","
          "\"val_tpl\":\"{{ {'occupied': 'Có người', 'empty': 'Trống', 'uncertain': 'Chưa chắc'}"
          ".get(value_json.state, value_json.state) }}\"" },
        { "sensor", "radar",
          "\"name\":\"Radar\",\"ent_cat\":\"diagnostic\",\"icon\":\"mdi:radar\",\"stat_t\":\"~/status\","
          "\"val_tpl\":\"{% if value_json.radar is defined %}{{ value_json.radar.status }}{% endif %}\"" },
        { "sensor", "rssi",
          "\"name\":\"Wi-Fi\",\"ent_cat\":\"diagnostic\",\"dev_cla\":\"signal_strength\","
          "\"unit_of_meas\":\"dBm\",\"stat_t\":\"~/status\","
          "\"val_tpl\":\"{% if value_json.rssi is defined %}{{ value_json.rssi }}{% endif %}\"" },
    };
    static char payload[1024];
    char topic[160];
    for (const Entity &e : kEntities) {
        snprintf(topic, sizeof(topic), "%s/%s/%s/%s/config", CONFIG_PRESENCE_HA_DISCOVERY_PREFIX, e.component,
                 g.device_id, e.object);
        const int n = snprintf(
            payload, sizeof(payload),
            "{\"~\":\"%s\",%s,\"uniq_id\":\"%s_%s\",\"avty_t\":\"~/status\","
            "\"avty_tpl\":\"{{ 'online' if value_json.online else 'offline' }}\","
            "\"dev\":{\"ids\":[\"%s\"],\"name\":\"Cảm biến hiện diện %s\",\"mf\":\"presence-s3-ai\","
            "\"mdl\":\"ESP32-S3 + LD2410\",\"sw\":\"%s\"}}",
            s_prefix, e.body, g.device_id, e.object, g.device_id, g.device_id, g.fw_version);
        if (n <= 0 || n >= (int)sizeof(payload) || esp_mqtt_client_publish(s_mqtt, topic, payload, n, 1, 1) < 0) {
            return false;
        }
    }
    ESP_LOGI(TAG, "Home Assistant discovery published (%s/...)", CONFIG_PRESENCE_HA_DISCOVERY_PREFIX);
    return true;
}
#endif

static void reset_inflight()
{
    s_inflight_n = 0;
    s_next_seq_valid = false;
}

static void process_acks()
{
    int msg_id;
    while (xQueueReceive(s_acks, &msg_id, 0) == pdTRUE) {
        for (int i = 0; i < s_inflight_n; i++) {
            if (s_inflight[i].msg_id == msg_id) {
                const uint32_t seq = s_inflight[i].last_seq;
                s_inflight[i] = s_inflight[--s_inflight_n];
                xSemaphoreTake(g.ring_lock, portMAX_DELAY);
                g.ring->consume_through(seq);
                xSemaphoreGive(g.ring_lock);
                break;
            }
        }
    }
}

static void handle_message(const char *topic, int topic_len, const char *data, int data_len)
{
    const size_t plen = strlen(s_prefix);
    if (topic_len <= (int)plen + 1 || strncmp(topic, s_prefix, plen) != 0) {
        return;
    }
    const char *suffix = topic + plen + 1;
    const int slen = topic_len - (int)plen - 1;
    auto is = [&](const char *s) { return slen == (int)strlen(s) && strncmp(suffix, s, slen) == 0; };
    if (is("desired/config")) {
        char err[64];
        if (data_len > 0 && device_config_apply_json(data, data_len, err, sizeof(err))) {
            decision_config_changed();
            s_status_requested = true;
        } else if (data_len > 0) {
            ESP_LOGW(TAG, "desired/config rejected: %s", err);
            char ev[128];
            snprintf(ev, sizeof(ev), "{\"type\":\"config\",\"ok\":false,\"error\":\"%s\"}", err);
            net_publish("event", ev, false);
        }
    } else if (is("desired/model")) {
        model_update_desired(data, data_len);
    } else if (is("cmd")) {
        char buf[160];
        const int n = data_len < (int)sizeof(buf) - 1 ? data_len : (int)sizeof(buf) - 1;
        memcpy(buf, data, n);
        buf[n] = '\0';
        if (strstr(buf, "\"reboot\"")) {
            net_publish("event", "{\"type\":\"cmd\",\"cmd\":\"reboot\",\"ok\":true}", false);
            vTaskDelay(pdMS_TO_TICKS(500));
            esp_restart();
        } else if (strstr(buf, "\"radar_reinit\"")) {
            radar_request_reinit();
        } else if (strstr(buf, "\"republish\"") || strstr(buf, "\"ping\"")) {
            s_status_requested = true;
        }
    }
}

static void mqtt_event(void *, esp_event_base_t, int32_t id, void *data)
{
    esp_mqtt_event_handle_t ev = (esp_mqtt_event_handle_t)data;
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED: {
        s_mqtt_connected = true;
        s_reset_tx = true;
        char topic[160];
        static const char *const subs[] = { "desired/config", "desired/model", "cmd" };
        for (const char *sfx : subs) {
            snprintf(topic, sizeof(topic), "%s/%s", s_prefix, sfx);
            esp_mqtt_client_subscribe(s_mqtt, topic, 1);
        }
        s_status_requested = true;
        s_ha_discovery_pending = true;
        ESP_LOGI(TAG, "MQTT connected");
        break;
    }
    case MQTT_EVENT_DISCONNECTED:
        s_mqtt_connected = false;
        s_reset_tx = true;
        ESP_LOGW(TAG, "MQTT disconnected");
        break;
    case MQTT_EVENT_PUBLISHED:
        xQueueSend(s_acks, &ev->msg_id, 0);
        break;
    case MQTT_EVENT_DATA:
        // Control messages are small; a fragmented message (current_data_offset > 0) is ignored
        if (ev->current_data_offset == 0 && ev->data_len == ev->total_data_len) {
            handle_message(ev->topic, ev->topic_len, ev->data, ev->data_len);
        }
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGW(TAG, "MQTT error");
        break;
    default:
        break;
    }
}

static void start_mqtt()
{
    const NetSettings &ns = net_settings();
    static char lwt_topic[160];
    snprintf(lwt_topic, sizeof(lwt_topic), "%s/status", s_prefix);
    esp_mqtt_client_config_t cfg = {};
    cfg.broker.address.uri = ns.mqtt_uri;
#if PRESENCE_PRIVATE_CA
    cfg.broker.verification.certificate = presence_private_ca_pem;
#elif CONFIG_PRESENCE_TLS_USE_CRT_BUNDLE
    cfg.broker.verification.crt_bundle_attach = esp_crt_bundle_attach;
#endif
    cfg.credentials.client_id = g.device_id;
    // Broker ACL (deployment/mosquitto/config/acl) gives each device only presence/v1/dev/<username>/#: the
    // username defaults to the device id
    if (ns.mqtt_user[0] || ns.mqtt_pass[0]) {
        cfg.credentials.username = ns.mqtt_user[0] ? ns.mqtt_user : g.device_id;
        cfg.credentials.authentication.password = ns.mqtt_pass;
    }
    cfg.session.keepalive = 30;
    cfg.session.last_will.topic = lwt_topic;
    cfg.session.last_will.msg = "{\"online\":false,\"reason\":\"lwt\"}";
    cfg.session.last_will.qos = 1;
    cfg.session.last_will.retain = 1;
    cfg.network.reconnect_timeout_ms = 5000;
    cfg.buffer.size = 2048;
    cfg.buffer.out_size = 6144;
    cfg.outbox.limit = 24 * 1024; // bounded: the sample buffer, not the outbox, absorbs outages
    s_mqtt = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_mqtt, MQTT_EVENT_ANY, mqtt_event, nullptr);
    esp_mqtt_client_start(s_mqtt);
}

static void on_ip(void *, esp_event_base_t, int32_t, void *data)
{
    ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
    snprintf(s_ip_text, sizeof(s_ip_text), IPSTR, IP2STR(&e->ip_info.ip));
    ESP_LOGI(TAG, "IP %s", s_ip_text);
    if (!s_ip) {
        s_ip = true;
        esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG(CONFIG_PRESENCE_SNTP_SERVER);
        esp_netif_sntp_init(&sntp);
        start_mqtt();
    }
}

#if !CONFIG_PRESENCE_NET_QEMU_OPENETH
static void on_wifi(void *, esp_event_base_t, int32_t id, void *)
{
    static int retries;
    if (id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (id == WIFI_EVENT_STA_DISCONNECTED) {
        s_ip_text[0] = '\0';
        // Back off 1, 2, 4 ... 30 s (a timer: the event loop is never blocked); detection keeps running locally
        const int delay_s = retries < 5 ? (1 << retries) : 30;
        retries++;
        esp_timer_stop(s_wifi_retry);
        esp_timer_start_once(s_wifi_retry, (uint64_t)delay_s * 1000000);
    } else if (id == WIFI_EVENT_STA_CONNECTED) {
        retries = 0;
    }
}
#endif

static void start_link()
{
#if CONFIG_PRESENCE_NET_QEMU_OPENETH
    // QEMU only: OpenCores Ethernet MAC (-nic user,model=open_eth)
    esp_netif_config_t ncfg = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *netif = esp_netif_new(&ncfg);
    esp_netif_set_hostname(netif, g.device_id);
    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.autonego_timeout_ms = 100;
    esp_eth_mac_t *mac = esp_eth_mac_new_openeth(&mac_cfg);
    esp_eth_phy_t *phy = esp_eth_phy_new_dp83848(&phy_cfg);
    esp_eth_config_t eth_cfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth = nullptr;
    ESP_ERROR_CHECK(esp_eth_driver_install(&eth_cfg, &eth));
    ESP_ERROR_CHECK(esp_netif_attach(netif, esp_eth_new_netif_glue(eth)));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_ip, nullptr));
    ESP_ERROR_CHECK(esp_eth_start(eth));
#else
    const NetSettings &ns = net_settings();
    if (!ns.wifi_ssid[0]) {
        ESP_LOGW(TAG, "no Wi-Fi SSID: set it with the console (config set wifi_ssid ...) - detection runs offline");
        return;
    }
    const esp_timer_create_args_t targs = { .callback = [](void *) { esp_wifi_connect(); }, .arg = nullptr,
                                            .dispatch_method = ESP_TIMER_TASK, .name = "wifi_retry",
                                            .skip_unhandled_events = true };
    ESP_ERROR_CHECK(esp_timer_create(&targs, &s_wifi_retry));
    // Hostname = device id: the router's client list shows where the configuration page is (http://<ip>/)
    esp_netif_set_hostname(esp_netif_create_default_wifi_sta(), g.device_id);
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_ip, nullptr));
    wifi_config_t wc = {};
    // 32-byte SSID and 64-byte password fields are not NUL-terminated when full
    memcpy(wc.sta.ssid, ns.wifi_ssid, strnlen(ns.wifi_ssid, sizeof(wc.sta.ssid)));
    memcpy(wc.sta.password, ns.wifi_pass, strnlen(ns.wifi_pass, sizeof(wc.sta.password)));
    wc.sta.threshold.authmode = ns.wifi_pass[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
#endif
}

static uint64_t epoch_ms()
{
    if (!net_time_synced()) {
        return 0;
    }
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    return (uint64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

// Sends the next batch of buffered samples. Returns true when something was sent.
static bool send_samples(char *json, size_t json_size, presence::Sample *batch)
{
    if (!s_mqtt_connected || s_inflight_n >= kMaxInFlight || esp_mqtt_client_get_outbox_size(s_mqtt) > 16 * 1024) {
        return false;
    }
    xSemaphoreTake(g.ring_lock, portMAX_DELAY);
    size_t n = s_next_seq_valid ? g.ring->peek_from(s_next_seq, batch, CONFIG_PRESENCE_BATCH_SAMPLES)
                                : g.ring->peek(batch, CONFIG_PRESENCE_BATCH_SAMPLES);
    xSemaphoreGive(g.ring_lock);
    if (n == 0) {
        return false;
    }
    lock();
    const presence::BatchHeader h{ g.device_id, g.ticks.boot_id, g.ticks.t0_ms, uptime_ms(), epoch_ms() };
    unlock();
    size_t len = 0;
    n = presence::encode_batch(h, batch, n, json, json_size, &len);
    if (n == 0) {
        return false;
    }
    char topic[160];
    snprintf(topic, sizeof(topic), "%s/samples", s_prefix);
    const int id = esp_mqtt_client_publish(s_mqtt, topic, json, (int)len, 1, 0);
    if (id < 0) {
        return false;
    }
    // The PUBACK is handled by this task later (process_acks), so this entry is always recorded first
    s_inflight[s_inflight_n++] = InFlight{ id, batch[n - 1].seq, esp_timer_get_time() / 1000 };
    s_next_seq = batch[n - 1].seq + 1;
    s_next_seq_valid = true;
    return true;
}

static void net_task(void *)
{
    static char json[5120];
    static presence::Sample batch[40];
    static char status[3072];
    int64_t last_status = 0;
    int64_t last_send = 0;
    uint32_t state_version = 0;
    for (;;) {
        const int64_t now = esp_timer_get_time() / 1000;
        if (s_reset_tx) {
            s_reset_tx = false;
            reset_inflight();
            xQueueReset(s_acks);
        }
        process_acks();
        // An acknowledgement that never came (should not happen over TCP): send again from the oldest sample
        for (int i = 0; i < s_inflight_n; i++) {
            if (now - s_inflight[i].sent_ms > 30000) {
                ESP_LOGW(TAG, "no PUBACK for 30 s, resending buffered samples");
                reset_inflight();
                break;
            }
        }
        if (s_mqtt_connected) {
#if CONFIG_PRESENCE_HA_DISCOVERY
            if (s_ha_discovery_pending && publish_ha_discovery()) {
                s_ha_discovery_pending = false;
            }
#endif
            if (s_status_requested || now - last_status > 15000) {
                s_status_requested = false;
                last_status = now;
                status_build(status, sizeof(status));
                net_publish("status", status, true);
            }
            const uint32_t sv = decision_state_version();
            if (sv != state_version) {
                state_version = sv;
                status_build_state(status, sizeof(status));
                net_publish("state", status, true);
            }
            // One batch per second normally (5 samples); faster while catching up after an outage
            xSemaphoreTake(g.ring_lock, portMAX_DELAY);
            const size_t backlog = g.ring->size();
            xSemaphoreGive(g.ring_lock);
            if (now - last_send >= 1000 || backlog > (size_t)CONFIG_PRESENCE_BATCH_SAMPLES) {
                if (send_samples(json, sizeof(json), batch)) {
                    last_send = now;
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void net_start()
{
    snprintf(s_prefix, sizeof(s_prefix), "%s/dev/%s", net_settings().topic_prefix, g.device_id);
    s_acks = xQueueCreate(16, sizeof(int));
    start_link();
    web_config_start();
    // Core 0 with the network stack; radar and decisions stay on core 1
    xTaskCreatePinnedToCore(net_task, "net", 8192, nullptr, 4, nullptr, 0); // MQTT over TLS encrypts here
}

} // namespace app
