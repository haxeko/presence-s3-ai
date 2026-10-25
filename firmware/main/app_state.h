// State shared between the tasks (radar, tick timer, decision, network, model update). Each part has its owner;
// readers take a copy under the matching lock.
#pragma once

#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "ld2410_protocol.h"
#include "model_manager.h"
#include "pipeline.h"
#include "sample_ring.h"

namespace app {

enum class RadarStatus : uint8_t {
    Init = 0,       // starting, engineering mode not confirmed yet
    Ok,             // engineering reports with 9 + 9 gates arrive
    NoData,         // nothing valid for PRESENCE_RADAR_TIMEOUT_MS: wiring, power, baud rate
    NotEngineering, // only basic reports: engineering mode could not be enabled
    BadFrames,      // bytes arrive but no valid frame (baud rate?)
    Layout,         // engineering reports without 9 + 9 gates (unsupported variant/firmware)
};

const char *radar_status_name(RadarStatus s);

struct RadarInfo {
    RadarStatus status;
    bool config_read;       // parameters and version read at start
    char version[24];       // "V2.44.25070917", "" unknown
    uint16_t protocol;      // from the 0x00FF ACK
    uint16_t gate_cm;       // 75 or 20 (0 unknown: radar without the resolution query)
    ld2410::RadarParams params;
    uint32_t engineering_attempts;
    uint32_t reinit_count;
    int64_t last_report_ms;  // last valid engineering report (esp_timer ms)
    uint32_t reports_ok;     // engineering reports with full layout
    uint32_t reports_other;  // basic or partial reports
    uint32_t frames_bad;     // parser rejections
    uint32_t bytes;
    uint32_t uart_overflows; // UART FIFO / ring buffer overflow events (data lost before parsing)
    float report_rate_hz;    // measured over the last status period
};

struct TickStats {
    uint32_t ticks;
    uint32_t samples;
    uint32_t empty_ticks;  // no usable report in the 200 ms bin
    uint32_t queue_drops;  // decision task too slow (should stay 0)
    uint32_t t0_ms;        // uptime of tick 0
    uint32_t boot_id;
};

struct Shared {
    SemaphoreHandle_t lock; // RadarInfo, TickStats, last decision
    RadarInfo radar;
    TickStats ticks;
    presence::Decision last_decision;
    bool have_decision;
    uint8_t method;

    SemaphoreHandle_t ring_lock;
    presence::SampleRing *ring;

    presence::ModelManager *models;
    // Recursive: held by the decision task around a whole pipeline step and by ModelManager around infer() and
    // the switch to a new model, so a step never sees half of a model change.
    SemaphoreHandle_t model_lock;
    char device_id[16];
    char fw_version[32];
};

extern Shared g;

inline void model_lock() { xSemaphoreTakeRecursive(g.model_lock, portMAX_DELAY); }
inline void model_unlock() { xSemaphoreGiveRecursive(g.model_lock); }
inline void lock() { xSemaphoreTake(g.lock, portMAX_DELAY); }
inline void unlock() { xSemaphoreGive(g.lock); }

uint32_t uptime_ms();

} // namespace app
