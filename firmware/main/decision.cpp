#include "decision.h"

#include <string.h>

#include "app_state.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "radar.h"
#include "sdkconfig.h"
#include "settings.h"

namespace app {

static const char *TAG = "decision";

static QueueHandle_t s_ticks;
static volatile bool s_config_changed = true;
static volatile uint32_t s_state_version;
static volatile uint32_t s_eval_us_last;
static volatile uint32_t s_eval_us_max;

void decision_eval_us(uint32_t *last, uint32_t *max)
{
    *last = s_eval_us_last;
    *max = s_eval_us_max;
}

uint32_t decision_state_version()
{
    return s_state_version;
}

void decision_config_changed()
{
    s_config_changed = true;
}

static presence::PipelineConfig pipeline_config()
{
    const DeviceConfig c = device_config();
    presence::PipelineConfig p;
    // Without an explicit override the active model's recommended parameters are used
    p.model_params = c.model_params;
    if (!c.model_params_override && g.models && g.models->active().present) {
        p.model_params = g.models->active().contract.presence;
    }
    p.threshold_params = c.threshold_params;
    p.thresholds = c.thresholds;
    p.baseline_thresholds = c.baseline;
    p.prefer_threshold = c.prefer_threshold;
    // A model trained with another gate length than the radar reports must not run (features differ), nor while the
    // radar's gate length is still unknown (setup not read yet)
    if (model_blocked_reason() != nullptr) {
        p.prefer_threshold = true;
    }
    return p;
}

const char *model_blocked_reason()
{
    lock();
    const uint32_t gate_cm = g.radar.gate_cm;
    unlock();
    model_lock();
    const bool present = g.models && g.models->active().present;
    const uint32_t model_cm = present ? g.models->active().contract.gate_resolution_cm : 0;
    model_unlock();
    if (!present) {
        return nullptr;
    }
    if (gate_cm == 0) {
        return "radar_gate_unknown";
    }
    return model_cm != gate_cm ? "radar_gate_mismatch" : nullptr;
}

static void decision_task(void *)
{
    presence::DecisionPipeline pipeline(g.models, pipeline_config());
    uint32_t active_version = 0;
    uint8_t last_state = 0xFF;
    uint8_t last_decision = 0xFF;
    presence::DecisionInfo info;
    memset(&info, 0, sizeof(info));
    info.pred = -1;
    info.occ_permille = -1;
    TickMsg m;
    for (;;) {
        if (xQueueReceive(s_ticks, &m, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        model_lock();
        const uint32_t version = g.models && g.models->active().present ? g.models->active().model_version : 0;
        if (s_config_changed || version != active_version) {
            s_config_changed = false;
            active_version = version;
            if (!pipeline.set_config(pipeline_config())) {
                ESP_LOGW(TAG, "invalid configuration ignored");
            }
        }
        model_unlock();
        presence::Decision d;
        model_lock();
        const int64_t t0 = esp_timer_get_time();
        const bool decided = pipeline.on_tick(m.tick, m.has_sample ? &m.sample : nullptr, &d);
        const uint32_t dt = (uint32_t)(esp_timer_get_time() - t0);
        model_unlock();
        if (decided) {
            s_eval_us_last = dt;
            if (dt > s_eval_us_max) {
                s_eval_us_max = dt;
            }
            info.method = d.method;
            info.model_version = d.model_version;
            info.pred = d.pred;
            memcpy(info.q, d.q, sizeof(info.q));
            info.occ_permille = d.usable ? (int16_t)(d.score * 1000.0f + 0.5f) : -1;
            info.state = d.state;
            info.decision = d.decision;
            lock();
            g.last_decision = d;
            g.have_decision = true;
            g.method = d.method;
            unlock();
            if (d.state != last_state || d.decision != last_decision) {
                last_state = d.state;
                last_decision = d.decision;
                s_state_version = s_state_version + 1;
#if CONFIG_PRESENCE_OUTPUT_GPIO >= 0
                if (d.decision != presence::kDecisionUnknown) {
                    gpio_set_level((gpio_num_t)CONFIG_PRESENCE_OUTPUT_GPIO, d.decision == presence::kDecisionOccupied);
                }
#endif
            }
        }
        if (m.has_sample) {
            m.sample.info = info;
            m.sample.info.evaluated = decided ? 1 : 0;
            xSemaphoreTake(g.ring_lock, portMAX_DELAY);
            g.ring->push(m.sample);
            xSemaphoreGive(g.ring_lock);
        }
    }
}

QueueHandle_t decision_start()
{
#if CONFIG_PRESENCE_OUTPUT_GPIO >= 0
    gpio_reset_pin((gpio_num_t)CONFIG_PRESENCE_OUTPUT_GPIO);
    gpio_set_direction((gpio_num_t)CONFIG_PRESENCE_OUTPUT_GPIO, GPIO_MODE_OUTPUT);
#endif
    s_ticks = xQueueCreate(32, sizeof(TickMsg));
    // Same core as the radar reader, below it: inference never delays UART reading
    xTaskCreatePinnedToCore(decision_task, "decision", 6144, nullptr, 8, nullptr, 1);
    return s_ticks;
}

} // namespace app
