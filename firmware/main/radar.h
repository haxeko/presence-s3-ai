// LD2410 on a hardware UART: reader task (parser -> 5 Hz sampler), 200 ms tick timer, radar control task
// (configuration read, engineering mode, re-initialization when the radar stops reporting).
#pragma once

#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "sample.h"

namespace app {

struct TickMsg {
    uint32_t tick;
    bool has_sample;
    presence::Sample sample;
};

// Ticks go to `tick_queue` (one TickMsg per 200 ms, with or without a sample).
esp_err_t radar_start(QueueHandle_t tick_queue);
// Asks the control task to run the initialization again (console / backend command).
void radar_request_reinit();

} // namespace app
