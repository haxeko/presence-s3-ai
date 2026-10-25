// Decision task: every 200 ms tick -> DecisionPipeline (model or threshold method + presence logic), annotates
// the sample with the latest decision, queues it for the backend, drives the optional presence output pin.
// Runs without network: detection stays local.
#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

namespace app {

QueueHandle_t decision_start();
// Applies the current device configuration at the next tick (called after a desired/config message).
void decision_config_changed();
// Incremented on every change of the reported state or decision (network task publishes .../state).
uint32_t decision_state_version();
// Why an installed model is not used ("radar_gate_unknown", "radar_gate_mismatch"), or nullptr.
const char *model_blocked_reason();
// Time of the last and the slowest window evaluation (features + quantization + inference + presence logic), us.
void decision_eval_us(uint32_t *last, uint32_t *max);

} // namespace app
