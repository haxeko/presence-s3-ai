#include "status.h"

#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "cJSON.h"
#include "decision.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_timer.h"
#include "model_update.h"
#include "net.h"
#include "settings.h"
#include "tls_trust.h"

namespace app {

static const char *state_name(uint8_t s)
{
    return s == presence::kStateOccupied ? "occupied" : (s == presence::kStateEmpty ? "empty" : "uncertain");
}

static const char *decision_name(uint8_t d)
{
    return d == presence::kDecisionOccupied ? "occupied" : (d == presence::kDecisionEmpty ? "empty" : "unknown");
}

static const char *method_name(uint8_t m)
{
    switch (m) {
    case presence::kMethodModel:
        return "model";
    case presence::kMethodThresholdBaseline:
        return "threshold_baseline";
    case presence::kMethodThresholdFactory:
        return "threshold_factory";
    }
    return "none";
}

static const char *evidence_name(presence::Evidence e)
{
    switch (e) {
    case presence::Evidence::High:
        return "high";
    case presence::Evidence::Low:
        return "low";
    case presence::Evidence::Mid:
        return "mid";
    case presence::Evidence::None:
        return "none";
    }
    return "?";
}

static cJSON *gate_array(const uint8_t *v)
{
    cJSON *a = cJSON_CreateArray();
    for (int g = 0; g < presence::kGates; g++) {
        cJSON_AddItemToArray(a, cJSON_CreateNumber(v[g]));
    }
    return a;
}

// float -> 3 decimals (0.7f would otherwise print as 0.699999988)
static double round3(float v)
{
    return (double)(int)(v * 1000.0f + (v >= 0 ? 0.5f : -0.5f)) / 1000.0;
}

static cJSON *params_json(const presence::PresenceParams &p)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "enter", round3(p.enter));
    cJSON_AddNumberToObject(o, "exit", round3(p.exit));
    cJSON_AddNumberToObject(o, "confirm_occupied_ms", p.confirm_occupied_ms);
    cJSON_AddNumberToObject(o, "confirm_empty_ms", p.confirm_empty_ms);
    return o;
}

static void add_presence(cJSON *root, const presence::Decision &d, bool have)
{
    cJSON *p = cJSON_AddObjectToObject(root, "presence");
    cJSON_AddStringToObject(p, "state", have ? state_name(d.state) : "uncertain");
    cJSON_AddStringToObject(p, "decision", have ? decision_name(d.decision) : "unknown");
    cJSON_AddStringToObject(p, "method", method_name(have ? d.method : 0));
    cJSON_AddStringToObject(p, "evidence", have ? evidence_name(d.evidence) : "none");
    if (have && d.usable) {
        // Model score (sum of the occupied classes) or share of samples above the thresholds: NOT a probability
        cJSON_AddNumberToObject(p, "score", round3(d.score));
        model_lock();
        float prob;
        if (d.method == presence::kMethodModel && g.models->active().present &&
            presence::calibrate(g.models->active().contract, d.score, &prob)) {
            cJSON_AddNumberToObject(p, "calibrated_probability", round3(prob));
        }
        model_unlock();
    } else {
        cJSON_AddNullToObject(p, "score");
    }
    cJSON_AddNumberToObject(p, "tick", have ? d.tick : 0);
}

void status_build_state(char *out, size_t size)
{
    lock();
    const presence::Decision d = g.last_decision;
    const bool have = g.have_decision;
    unlock();
    cJSON *root = cJSON_CreateObject();
    add_presence(root, d, have);
    cJSON *p = cJSON_GetObjectItem(root, "presence");
    cJSON *flat = cJSON_Duplicate(p, true);
    cJSON_Delete(root);
    if (!cJSON_PrintPreallocated(flat, out, (int)size, false)) {
        snprintf(out, size, "{\"state\":\"uncertain\"}");
    }
    cJSON_Delete(flat);
}

void status_build(char *out, size_t size)
{
    lock();
    const RadarInfo r = g.radar;
    const TickStats t = g.ticks;
    const presence::Decision d = g.last_decision;
    const bool have = g.have_decision;
    unlock();
    xSemaphoreTake(g.ring_lock, portMAX_DELAY);
    const size_t used = g.ring->size(), cap = g.ring->capacity(), hw = g.ring->high_water();
    const uint32_t dropped = g.ring->dropped();
    xSemaphoreGive(g.ring_lock);

    cJSON *root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "online", true);
    cJSON_AddStringToObject(root, "device_id", g.device_id);
    cJSON_AddStringToObject(root, "fw", g.fw_version);
    cJSON_AddNumberToObject(root, "fw_api", presence::kFirmwareModelApi);
    cJSON_AddStringToObject(root, "idf", esp_get_idf_version());
    cJSON_AddNumberToObject(root, "boot_id", t.boot_id);
    cJSON_AddNumberToObject(root, "boot_count", boot_count());
    cJSON_AddNumberToObject(root, "uptime_s", uptime_ms() / 1000);
    cJSON_AddNumberToObject(root, "rssi", net_rssi());
    char ip[16];
    net_ip(ip, sizeof(ip));
    cJSON_AddStringToObject(root, "ip", ip);
    cJSON_AddBoolToObject(root, "time_synced", net_time_synced());
    cJSON_AddStringToObject(root, "tls_trust", PRESENCE_TLS_TRUST);
    cJSON_AddNumberToObject(root, "heap_free", heap_caps_get_free_size(MALLOC_CAP_8BIT));
    cJSON_AddNumberToObject(root, "heap_min", heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT));

    cJSON *rj = cJSON_AddObjectToObject(root, "radar");
    cJSON_AddStringToObject(rj, "status", radar_status_name(r.status));
    cJSON_AddStringToObject(rj, "version", r.version);
    cJSON_AddNumberToObject(rj, "protocol", r.protocol);
    cJSON_AddNumberToObject(rj, "gate_cm", r.gate_cm);
    const int64_t age = r.last_report_ms > 0 ? esp_timer_get_time() / 1000 - r.last_report_ms : -1;
    cJSON_AddNumberToObject(rj, "last_report_age_ms", (double)age);
    cJSON_AddNumberToObject(rj, "reports_ok", r.reports_ok);
    cJSON_AddNumberToObject(rj, "reports_other", r.reports_other);
    cJSON_AddNumberToObject(rj, "frames_bad", r.frames_bad);
    cJSON_AddNumberToObject(rj, "bytes", r.bytes);
    cJSON_AddNumberToObject(rj, "uart_overflows", r.uart_overflows);
    cJSON_AddNumberToObject(rj, "engineering_attempts", r.engineering_attempts);
    cJSON_AddNumberToObject(rj, "reinit_count", r.reinit_count);
    if (r.config_read) {
        cJSON_AddNumberToObject(rj, "max_moving_gate", r.params.max_moving_gate);
        cJSON_AddNumberToObject(rj, "max_still_gate", r.params.max_still_gate);
        cJSON_AddNumberToObject(rj, "no_one_duration_s", r.params.no_one_duration_s);
        cJSON *th = cJSON_AddObjectToObject(rj, "thresholds");
        cJSON_AddItemToObject(th, "moving", gate_array(r.params.moving_threshold));
        cJSON_AddItemToObject(th, "still", gate_array(r.params.still_threshold));
    }
    cJSON *sj = cJSON_AddObjectToObject(root, "sampler");
    cJSON_AddNumberToObject(sj, "ticks", t.ticks);
    cJSON_AddNumberToObject(sj, "samples", t.samples);
    cJSON_AddNumberToObject(sj, "empty_ticks", t.empty_ticks);
    cJSON_AddNumberToObject(sj, "queue_drops", t.queue_drops);
    cJSON_AddNumberToObject(sj, "sample_rate_hz", t.ticks ? round3(5.0f * t.samples / t.ticks) : 0);
    cJSON_AddNumberToObject(sj, "radar_reports_per_s",
                            t.ticks ? round3((float)((double)r.reports_ok * 1000.0 /
                                                     ((double)t.ticks * presence::kSamplePeriodMs)))
                                    : 0);
    cJSON *bj = cJSON_AddObjectToObject(root, "buffer");
    cJSON_AddNumberToObject(bj, "capacity", cap);
    cJSON_AddNumberToObject(bj, "used", used);
    cJSON_AddNumberToObject(bj, "dropped", dropped);
    cJSON_AddNumberToObject(bj, "high_water", hw);
    cJSON_AddStringToObject(bj, "policy", "drop_oldest");

    cJSON *mj = cJSON_AddObjectToObject(root, "model");
    model_lock();
    const presence::ActiveModel a = g.models->active();
    model_unlock();
    cJSON_AddStringToObject(mj, "method", method_name(have ? d.method : (a.present ? presence::kMethodModel : 0)));
    if (a.present) {
        cJSON *aj = cJSON_AddObjectToObject(mj, "active");
        cJSON_AddStringToObject(aj, "model_id", a.model_id);
        cJSON_AddNumberToObject(aj, "version", a.model_version);
        cJSON_AddNumberToObject(aj, "slot", a.slot);
        cJSON_AddBoolToObject(aj, "simulated_data", (a.flags & presence::kPkgFlagSimulated) != 0);
        cJSON_AddNumberToObject(aj, "arena_used", a.arena_used);
        cJSON_AddNumberToObject(aj, "tflite_bytes", a.tflite_len);
        cJSON_AddNumberToObject(aj, "gate_cm", a.contract.gate_resolution_cm);
        cJSON_AddItemToObject(aj, "recommended", params_json(a.contract.presence));
    } else {
        cJSON_AddNullToObject(mj, "active");
    }
    char phase[16], err[64];
    unsigned uv;
    model_update_status(phase, sizeof(phase), err, sizeof(err), &uv);
    cJSON *uj = cJSON_AddObjectToObject(mj, "update");
    cJSON_AddStringToObject(uj, "phase", phase);
    cJSON_AddNumberToObject(uj, "model_version", uv);
    cJSON_AddStringToObject(uj, "error", err);
    cJSON *slots = cJSON_AddArrayToObject(mj, "slots");
    // Copy of the store's RAM metadata (only the update task changes it; a status built during an update may show
    // the state just before or after the change)
    const presence::ModelMeta meta = model_store_for_status()->meta();
    for (int i = 0; i < 2; i++) {
        cJSON *s = cJSON_CreateObject();
        cJSON_AddStringToObject(s, "state", presence::slot_state_name(meta.slots[i].state));
        cJSON_AddNumberToObject(s, "version", meta.slots[i].model_version);
        cJSON_AddStringToObject(s, "model_id", meta.slots[i].model_id);
        cJSON_AddItemToArray(slots, s);
    }
    if (meta.previous != presence::kNoSlot) {
        cJSON *pj = cJSON_AddObjectToObject(mj, "previous");
        cJSON_AddStringToObject(pj, "model_id", meta.slots[meta.previous].model_id);
        cJSON_AddNumberToObject(pj, "version", meta.slots[meta.previous].model_version);
    }
    add_presence(root, d, have);
    const DeviceConfig c = device_config();
    cJSON *pp = cJSON_GetObjectItem(root, "presence");
    cJSON_AddNumberToObject(pp, "config_rev", c.rev);
    cJSON_AddItemToObject(pp, "params", params_json(have && d.method == presence::kMethodModel
                                                       ? (c.model_params_override || !a.present ? c.model_params
                                                                                                : a.contract.presence)
                                                       : c.threshold_params));
    if (c.baseline) {
        cJSON *bl = cJSON_AddObjectToObject(pp, "baseline");
        cJSON_AddNumberToObject(bl, "id", c.baseline_id);
    }
    cJSON_AddBoolToObject(pp, "prefer_threshold", c.prefer_threshold);
    const char *blocked = model_blocked_reason();
    if (blocked) {
        cJSON_AddStringToObject(pp, "model_blocked", blocked);
    }
    uint32_t eval_last, eval_max;
    decision_eval_us(&eval_last, &eval_max);
    cJSON *ej = cJSON_AddObjectToObject(pp, "eval_us");
    cJSON_AddNumberToObject(ej, "last", eval_last);
    cJSON_AddNumberToObject(ej, "max", eval_max);
    if (!cJSON_PrintPreallocated(root, out, (int)size, false)) {
        snprintf(out, size, "{\"online\":true,\"device_id\":\"%s\",\"error\":\"status too large\"}", g.device_id);
    }
    cJSON_Delete(root);
}

void status_print()
{
    lock();
    const RadarInfo r = g.radar;
    const TickStats t = g.ticks;
    const presence::Decision d = g.last_decision;
    const bool have = g.have_decision;
    unlock();
    printf("device %s fw %s, uptime %u s, MQTT %s\n", g.device_id, g.fw_version, (unsigned)(uptime_ms() / 1000),
           net_mqtt_connected() ? "connected" : "not connected");
    printf("radar %s, %s, gate %u cm, reports ok %u other %u, bad frames %u, UART overflows %u\n",
           radar_status_name(r.status), r.version[0] ? r.version : "version ?", r.gate_cm, (unsigned)r.reports_ok,
           (unsigned)r.reports_other, (unsigned)r.frames_bad, (unsigned)r.uart_overflows);
    printf("ticks %u, samples %u, empty %u\n", (unsigned)t.ticks, (unsigned)t.samples, (unsigned)t.empty_ticks);
    if (have) {
        printf("presence %s (decision %s), method %s, score %.3f\n", state_name(d.state), decision_name(d.decision),
               method_name(d.method), d.usable ? d.score : -1.0f);
    }
    model_lock();
    const presence::ActiveModel a = g.models->active();
    model_unlock();
    if (a.present) {
        printf("model %s v%u (slot %d)\n", a.model_id, (unsigned)a.model_version, a.slot);
    } else {
        printf("no model: threshold method\n");
    }
}

} // namespace app
