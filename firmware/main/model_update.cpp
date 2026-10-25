#include "model_update.h"

#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "cJSON.h"
#include "crypto.h"
#include "decision.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "model_manager.h"
#include "net.h"
#include "partition_region.h"
#include "sdkconfig.h"
#include "settings.h"
#include "tflm_engine.h"
#include "tls_trust.h"
#if CONFIG_PRESENCE_TLS_USE_CRT_BUNDLE
#include "esp_crt_bundle.h"
#endif

namespace app {

static const char *TAG = "model";

#if CONFIG_PRESENCE_MODEL_REQUIRE_SIGNATURE
extern const char k_signing_pub_pem[] asm("_binary_model_signing_pub_pem_start");
#endif

struct Desired {
    char model_id[presence::kPkgModelIdLen + 1];
    uint32_t model_version;
    uint32_t size;
    uint8_t sha256[32];
    uint32_t release_id;
};

static presence::PartitionRegion *s_meta, *s_a, *s_b;
static presence::ModelStore *s_store;
static presence::TflmEngine s_engine_a, s_engine_b;
static QueueHandle_t s_requests;
static SemaphoreHandle_t s_status_lock;
static char s_phase[16] = "idle";
static char s_error[64] = "";
static unsigned s_phase_version;
// Failed attempts of the last desired version: retried at most 3 times, 10 minutes apart
static uint32_t s_failed_version;
static uint8_t s_failed_sha[32];
static int s_failed_count;
static int64_t s_failed_at_ms;

static uint32_t radar_gate_cm()
{
    lock();
    const uint32_t cm = g.radar.gate_cm;
    unlock();
    return cm;
}

static void lock_fn() { model_lock(); }
static void unlock_fn() { model_unlock(); }
static size_t free_heap() { return heap_caps_get_free_size(MALLOC_CAP_8BIT); }
static int64_t now_us() { return esp_timer_get_time(); }

static void *alloc_fn(size_t n)
{
    return heap_caps_aligned_alloc(16, n, MALLOC_CAP_8BIT);
}

static void set_phase(const char *phase, const char *error, unsigned version, uint32_t release_id,
                      const presence::ModelCheck *check = nullptr)
{
    xSemaphoreTake(s_status_lock, portMAX_DELAY);
    snprintf(s_phase, sizeof(s_phase), "%s", phase);
    snprintf(s_error, sizeof(s_error), "%s", error ? error : "");
    s_phase_version = version;
    xSemaphoreGive(s_status_lock);
    char ev[512];
    int n = snprintf(ev, sizeof(ev), "{\"type\":\"model_update\",\"phase\":\"%s\",\"model_version\":%u,\"release_id\":%u",
                     phase, version, (unsigned)release_id);
    if (error && error[0]) {
        n += snprintf(ev + n, sizeof(ev) - n, ",\"error\":\"%s\"", error);
    }
    if (check) {
        const presence::SelfTestReport &r = check->selftest;
        n += snprintf(ev + n, sizeof(ev) - n,
                      ",\"selftest\":{\"vectors\":%u,\"passed\":%s,\"max_feature_diff\":%.3g,\"max_input_diff\":%d,"
                      "\"max_output_diff\":%d,\"inference_us_max\":%u,\"arena_used\":%u},\"detail\":\"%s\"",
                      (unsigned)r.vectors, r.passed ? "true" : "false", r.max_feature_diff, r.max_input_diff,
                      r.max_output_diff, (unsigned)r.inference_us_max, (unsigned)r.arena_used, check->detail);
    }
    snprintf(ev + n, sizeof(ev) - n, "}");
    ESP_LOGI(TAG, "%s", ev);
    net_publish("event", ev, false);
    net_request_status();
}

void model_update_status(char *phase, size_t phase_len, char *error, size_t error_len, unsigned *version)
{
    xSemaphoreTake(s_status_lock, portMAX_DELAY);
    snprintf(phase, phase_len, "%s", s_phase);
    snprintf(error, error_len, "%s", s_error);
    *version = s_phase_version;
    xSemaphoreGive(s_status_lock);
}

static bool parse_desired(const char *json, size_t len, Desired *d)
{
    cJSON *root = cJSON_ParseWithLength(json, len);
    if (!root) {
        return false;
    }
    const cJSON *id = cJSON_GetObjectItem(root, "model_id");
    const cJSON *ver = cJSON_GetObjectItem(root, "model_version");
    const cJSON *size = cJSON_GetObjectItem(root, "size");
    const cJSON *sha = cJSON_GetObjectItem(root, "sha256");
    const cJSON *rel = cJSON_GetObjectItem(root, "release_id");
    bool ok = cJSON_IsString(id) && cJSON_IsNumber(ver) && cJSON_IsNumber(size) && cJSON_IsString(sha) &&
              strlen(id->valuestring) > 0 && strlen(id->valuestring) <= presence::kPkgModelIdLen &&
              presence::hex_decode(sha->valuestring, d->sha256, 32);
    if (ok) {
        // Only safe characters: the id becomes part of the download URL
        for (const char *c = id->valuestring; *c; c++) {
            ok = ok && ((*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') || (*c >= '0' && *c <= '9') ||
                        *c == '-' || *c == '_' || *c == '.');
        }
        snprintf(d->model_id, sizeof(d->model_id), "%s", id->valuestring);
        d->model_version = (uint32_t)ver->valuedouble;
        d->size = (uint32_t)size->valuedouble;
        d->release_id = cJSON_IsNumber(rel) ? (uint32_t)rel->valuedouble : 0;
    }
    cJSON_Delete(root);
    return ok;
}

void model_update_desired(const char *json, size_t len)
{
    if (len == 0) {
        return; // desired model cleared: keep the running one
    }
    Desired d;
    if (!parse_desired(json, len, &d)) {
        set_phase("failed", "invalid desired/model message", 0, 0);
        return;
    }
    xQueueOverwrite(s_requests, &d);
}

// Streams the package into the inactive slot. Returns the slot, or -1 (kind and err filled).
static int download(const Desired &d, presence::ModelError *kind, char *err, size_t err_len)
{
    const NetSettings &ns = net_settings();
    *kind = presence::ModelError::Download;
    if (d.size == 0 || d.size > CONFIG_PRESENCE_MODEL_MAX_PACKAGE_KB * 1024 || d.size > s_a->size()) {
        *kind = presence::ModelError::Size;
        snprintf(err, err_len, "package size %u not allowed", (unsigned)d.size);
        return -1;
    }
    char url[256];
    snprintf(url, sizeof(url), "%s/api/device/models/%s/package", ns.server_url, d.model_id);
    esp_http_client_config_t cfg = {};
    cfg.url = url;
    cfg.timeout_ms = 15000;
    cfg.buffer_size = 2048;
#if PRESENCE_PRIVATE_CA
    cfg.cert_pem = presence_private_ca_pem;
#elif CONFIG_PRESENCE_TLS_USE_CRT_BUNDLE
    cfg.crt_bundle_attach = esp_crt_bundle_attach;
#endif
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        snprintf(err, err_len, "http init");
        return -1;
    }
    if (ns.device_token[0]) {
        char auth[128];
        snprintf(auth, sizeof(auth), "Bearer %s", ns.device_token);
        esp_http_client_set_header(client, "Authorization", auth);
    }
    int slot = -1;
    if (esp_http_client_open(client, 0) != ESP_OK) {
        // TLS errors: mbedTLS code and X.509 flags (0x08 = not signed by a trusted CA, 0x04 = name mismatch)
        int tls_code = 0, tls_flags = 0;
        esp_http_client_get_and_clear_last_tls_error(client, &tls_code, &tls_flags);
        if (tls_code != 0 || tls_flags != 0) {
            snprintf(err, err_len, "tls -0x%x x509 0x%x %.40s", (unsigned)-tls_code, (unsigned)tls_flags,
                     ns.server_url);
        } else {
            snprintf(err, err_len, "cannot connect to %.60s", ns.server_url);
        }
    } else {
        const int64_t len = esp_http_client_fetch_headers(client);
        const int status = esp_http_client_get_status_code(client);
        if (status != 200) {
            snprintf(err, err_len, "HTTP %d", status);
        } else if (len >= 0 && (uint32_t)len != d.size) {
            snprintf(err, err_len, "size %lld, announced %u", (long long)len, (unsigned)d.size);
        } else {
            slot = s_store->inactive_slot();
            if (!s_store->begin_write(slot, d.model_version, d.model_id, d.size, d.sha256)) {
                *kind = presence::ModelError::Flash;
                snprintf(err, err_len, "slot erase/metadata write failed");
                slot = -1;
            } else {
                static uint8_t buf[2048];
                presence::Sha256 sha;
                uint32_t total = 0;
                while (total < d.size) {
                    const int n = esp_http_client_read(client, (char *)buf, sizeof(buf));
                    if (n <= 0 || total + (uint32_t)n > d.size) {
                        break;
                    }
                    if (!s_store->write_chunk(slot, total, buf, (size_t)n)) {
                        *kind = presence::ModelError::Flash;
                        snprintf(err, err_len, "flash write failed");
                        break;
                    }
                    sha.update(buf, (size_t)n);
                    total += (uint32_t)n;
                }
                uint8_t digest[32];
                sha.finish(digest);
                if (total != d.size) {
                    if (!err[0]) {
                        snprintf(err, err_len, "download incomplete (%u/%u bytes)", (unsigned)total, (unsigned)d.size);
                    }
                    s_store->set_state(slot, presence::SlotState::Bad);
                    slot = -1;
                } else if (memcmp(digest, d.sha256, 32) != 0) {
                    *kind = presence::ModelError::Sha256;
                    snprintf(err, err_len, "sha256 differs from the announcement");
                    s_store->set_state(slot, presence::SlotState::Bad);
                    slot = -1;
                }
            }
        }
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return slot;
}

static void apply(const Desired &d)
{
    presence::ModelManager *m = g.models;
    const presence::ActiveModel &act = m->active();
    const presence::ModelMeta &meta = s_store->meta();
    if (act.present && act.model_version == d.model_version && act.slot >= 0 &&
        memcmp(meta.slots[act.slot].pkg_sha256, d.sha256, 32) == 0) {
        set_phase("active", nullptr, d.model_version, d.release_id);
        return;
    }
    if (d.model_version == s_failed_version && memcmp(d.sha256, s_failed_sha, 32) == 0 &&
        (s_failed_count >= 3 || esp_timer_get_time() / 1000 - s_failed_at_ms < 600000)) {
        return; // failed recently: no download loop
    }
    presence::ModelCheck check;
    memset(&check, 0, sizeof(check));
    presence::ModelError e;
    char err[96] = "";
    m->set_radar_gate_cm(radar_gate_cm());
    // The previous model is still in the other slot (rollback from the backend): no download
    const uint8_t prev = meta.previous;
    if (prev != presence::kNoSlot && meta.slots[prev].state == presence::SlotState::Ready &&
        meta.slots[prev].model_version == d.model_version && memcmp(meta.slots[prev].pkg_sha256, d.sha256, 32) == 0) {
        set_phase("verify", nullptr, d.model_version, d.release_id);
        e = m->rollback(&check);
    } else {
        set_phase("download", nullptr, d.model_version, d.release_id);
        presence::ModelError kind;
        const int slot = download(d, &kind, err, sizeof(err));
        if (slot < 0) {
            e = kind;
            snprintf(check.detail, sizeof(check.detail), "%s", err);
        } else {
            set_phase("verify", nullptr, d.model_version, d.release_id);
            e = m->install(slot, d.sha256, &check);
        }
    }
    if (e == presence::ModelError::Ok) {
        s_failed_version = 0;
        s_failed_count = 0;
        decision_config_changed(); // presence parameters of the new model
        set_phase("active", nullptr, d.model_version, d.release_id, &check);
    } else {
        if (d.model_version != s_failed_version || memcmp(d.sha256, s_failed_sha, 32) != 0) {
            s_failed_count = 0;
        }
        s_failed_version = d.model_version;
        memcpy(s_failed_sha, d.sha256, 32);
        s_failed_count++;
        s_failed_at_ms = esp_timer_get_time() / 1000;
        char reason[96];
        snprintf(reason, sizeof(reason), "%s: %s", presence::model_error_name(e), check.detail);
        set_phase("failed", reason, d.model_version, d.release_id, &check);
    }
}

static void update_task(void *)
{
    Desired d;
    for (;;) {
        if (xQueueReceive(s_requests, &d, pdMS_TO_TICKS(60000)) == pdTRUE) {
            apply(d);
        }
    }
}

presence::ModelStore *model_store_for_status()
{
    return s_store;
}

bool model_update_rollback()
{
    presence::ModelCheck check;
    const presence::ModelError e = g.models->rollback(&check);
    if (e == presence::ModelError::Ok) {
        decision_config_changed();
        set_phase("active", nullptr, g.models->active().model_version, 0, &check);
        return true;
    }
    ESP_LOGW(TAG, "rollback: %s (%s)", presence::model_error_name(e), check.detail);
    return false;
}

void model_update_create()
{
    s_status_lock = xSemaphoreCreateMutex();
    s_requests = xQueueCreate(1, sizeof(Desired));
    s_meta = new presence::PartitionRegion(presence::PartitionRegion::find("model_meta"));
    s_a = new presence::PartitionRegion(presence::PartitionRegion::find("model_a"));
    s_b = new presence::PartitionRegion(presence::PartitionRegion::find("model_b"));
    if (!s_meta->valid() || !s_a->valid() || !s_b->valid()) {
        ESP_LOGE(TAG, "partition table without model_meta/model_a/model_b: threshold method only");
    }
    s_store = new presence::ModelStore(s_meta, s_a, s_b);
    presence::ModelManagerConfig cfg = {};
#if CONFIG_PRESENCE_MODEL_REQUIRE_SIGNATURE
    cfg.public_key_pem = k_signing_pub_pem;
#else
    cfg.public_key_pem = nullptr;
    ESP_LOGW(TAG, "model signatures NOT required (LAN test build)");
#endif
    cfg.radar_gate_cm = 0; // set in model_update_boot() once the radar reported its gate length
    cfg.max_package_bytes = CONFIG_PRESENCE_MODEL_MAX_PACKAGE_KB * 1024;
    cfg.free_heap_reserve = CONFIG_PRESENCE_MODEL_HEAP_RESERVE_KB * 1024;
    cfg.free_heap = free_heap;
    cfg.alloc = alloc_fn;
    cfg.release = heap_caps_free;
    cfg.now_us = now_us;
    cfg.lock = lock_fn;
    cfg.unlock = unlock_fn;
    g.models = new presence::ModelManager(*s_store, s_engine_a, s_engine_b, cfg);
}

void model_update_boot()
{
    // The model contract states the radar gate length it was trained with: wait (up to 3 s) for the radar to
    // report its own before checking the stored model.
    for (int i = 0; i < 30 && radar_gate_cm() == 0; i++) {
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    g.models->set_radar_gate_cm(radar_gate_cm());
    presence::ModelCheck check, failed;
    bool rolled = false;
    const presence::ModelError e = g.models->boot(&check, &failed, &rolled);
    if (e == presence::ModelError::Ok) {
        const presence::ActiveModel &a = g.models->active();
        ESP_LOGI(TAG, "model %s v%u active (slot %d, arena %u bytes)%s", a.model_id, (unsigned)a.model_version, a.slot,
                 (unsigned)a.arena_used, (a.flags & presence::kPkgFlagSimulated) ? " - TRAINED ON SIMULATED DATA" : "");
        snprintf(s_phase, sizeof(s_phase), "active");
        s_phase_version = a.model_version;
    } else {
        ESP_LOGW(TAG, "no usable model (%s): threshold method", check.detail);
    }
    if (rolled) {
        ESP_LOGW(TAG, "active model failed at boot (%s: %s): rolled back", presence::model_error_name(failed.error),
                 failed.detail);
        snprintf(s_phase, sizeof(s_phase), "rolled_back");
        snprintf(s_error, sizeof(s_error), "%.16s: %.44s", presence::model_error_name(failed.error), failed.detail);
    }
    xTaskCreatePinnedToCore(update_task, "model_upd", 8192, nullptr, 3, nullptr, 0); // TLS handshake + ECDSA
}

} // namespace app
