#include "radar.h"

#include <string.h>

#include "app_state.h"
#include "decision.h"
#include "driver/uart.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "ld2410_parser.h"
#include "ld2410_protocol.h"
#include "sampler.h"
#include "sdkconfig.h"
#include "settings.h"

namespace app {

static const char *TAG = "radar";

static constexpr int kRxBufSize = 4096; // ~160 ms of 256000 baud: covers flash writes and busy periods
static constexpr int kReadChunk = 256;
static constexpr int kAckTimeoutMs = 1000;
static constexpr int kConfigAttempts = 3;

static uart_port_t s_uart;
static QueueHandle_t s_uart_events;
static QueueHandle_t s_ack_queue;
static QueueHandle_t s_tick_queue;
static SemaphoreHandle_t s_cmd_mutex;
static portMUX_TYPE s_sampler_mux = portMUX_INITIALIZER_UNLOCKED;
static presence::Sampler s_sampler;
static ld2410::Parser *s_parser;
static uint32_t s_tick;
static volatile bool s_reinit_requested;
static uint32_t s_basic_reports_since_init;

static void on_report(const ld2410::Report &r, void *)
{
    bool used;
    taskENTER_CRITICAL(&s_sampler_mux);
    used = s_sampler.add_report(r);
    taskEXIT_CRITICAL(&s_sampler_mux);
    lock();
    if (used) {
        g.radar.reports_ok++;
        g.radar.last_report_ms = esp_timer_get_time() / 1000;
    } else {
        g.radar.reports_other++;
        if (!r.engineering) {
            s_basic_reports_since_init++;
        }
    }
    unlock();
}

static void on_ack(const ld2410::Ack &a, void *)
{
    xQueueSend(s_ack_queue, &a, 0); // dropped when nobody waits
}

// Reader task: only reads and parses, never blocks on anything else.
static void reader_task(void *)
{
    static uint8_t buf[kReadChunk];
    for (;;) {
        uart_event_t ev;
        if (xQueueReceive(s_uart_events, &ev, pdMS_TO_TICKS(50)) == pdTRUE) {
            if (ev.type == UART_FIFO_OVF || ev.type == UART_BUFFER_FULL) {
                uart_flush_input(s_uart);
                xQueueReset(s_uart_events);
                s_parser->reset();
                lock();
                g.radar.uart_overflows++;
                unlock();
                continue;
            }
        }
        size_t avail = 0;
        uart_get_buffered_data_len(s_uart, &avail);
        while (avail > 0) {
            const int n = uart_read_bytes(s_uart, buf, avail < sizeof(buf) ? avail : sizeof(buf), 0);
            if (n <= 0) {
                break;
            }
            s_parser->feed(buf, (size_t)n);
            avail -= (size_t)n;
        }
        const ld2410::ParserStats &st = s_parser->stats();
        const uint32_t bad = st.bad_footer + st.bad_length + st.bad_payload;
        lock();
        g.radar.bytes = st.bytes;
        g.radar.frames_bad = bad;
        unlock();
    }
}

// 200 ms tick: closes the bin and hands it to the decision task (light work, esp_timer task context).
static void tick_cb(void *)
{
    TickMsg m;
    m.tick = s_tick++;
    taskENTER_CRITICAL(&s_sampler_mux);
    m.has_sample = s_sampler.close_tick(m.tick, &m.sample);
    taskEXIT_CRITICAL(&s_sampler_mux);
    if (xQueueSend(s_tick_queue, &m, 0) != pdTRUE) {
        lock();
        g.ticks.queue_drops++;
        unlock();
    }
    lock();
    g.ticks.ticks++;
    if (m.has_sample) {
        g.ticks.samples++;
    } else {
        g.ticks.empty_ticks++;
    }
    unlock();
}

static esp_err_t command(uint16_t cmd, const uint8_t *value, size_t len, ld2410::Ack *ack_out)
{
    uint8_t frame[32];
    const size_t n = ld2410::build_command(cmd, value, len, frame, sizeof(frame));
    if (n == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    xSemaphoreTake(s_cmd_mutex, portMAX_DELAY);
    xQueueReset(s_ack_queue);
    uart_write_bytes(s_uart, frame, n);
    const int64_t deadline = esp_timer_get_time() / 1000 + kAckTimeoutMs;
    esp_err_t err = ESP_ERR_TIMEOUT;
    ld2410::Ack ack;
    for (;;) {
        const int64_t left = deadline - esp_timer_get_time() / 1000;
        if (left <= 0 || xQueueReceive(s_ack_queue, &ack, pdMS_TO_TICKS(left)) != pdTRUE) {
            break;
        }
        if (ack.command != (cmd | ld2410::kAckFlag)) {
            continue; // late ACK of an earlier command
        }
        err = ack.status == 0 ? ESP_OK : ESP_FAIL;
        if (ack_out) {
            *ack_out = ack;
        }
        break;
    }
    xSemaphoreGive(s_cmd_mutex);
    return err;
}

static esp_err_t command_retry(uint16_t cmd, const uint8_t *value, size_t len, ld2410::Ack *ack)
{
    esp_err_t err = ESP_FAIL;
    for (int i = 0; i < kConfigAttempts && err != ESP_OK; i++) {
        err = command(cmd, value, len, ack);
    }
    return err;
}

// Reads the radar setup and enables engineering mode; the radar's stored configuration is never changed.
static bool init_radar()
{
    static const uint8_t enable_value[] = { 0x01, 0x00 };
    ld2410::Ack ack;
    lock();
    g.radar.engineering_attempts++;
    unlock();
    if (command_retry(ld2410::kCmdEnableConfig, enable_value, sizeof(enable_value), &ack) != ESP_OK) {
        ESP_LOGW(TAG, "no answer to 'enable configuration' (wiring, power, baud %d?)", radar_settings().baud);
        return false;
    }
    uint16_t proto = 0, bufsize = 0;
    ld2410::decode_enable_config(ack, &proto, &bufsize);
    ld2410::RadarParams params;
    memset(&params, 0, sizeof(params));
    const bool have_params = command(ld2410::kCmdReadParams, nullptr, 0, &ack) == ESP_OK &&
                             ld2410::decode_params(ack, &params);
    ld2410::RadarVersion ver;
    char vtext[24] = "";
    if (command(ld2410::kCmdReadVersion, nullptr, 0, &ack) == ESP_OK && ld2410::decode_version(ack, &ver)) {
        ld2410::format_version(ver, vtext, sizeof(vtext));
    }
    uint16_t gate_cm = 0;
    if (command_retry(ld2410::kCmdReadResolution, nullptr, 0, &ack) == ESP_OK) {
        ld2410::decode_resolution(ack, &gate_cm);
    } else {
        gate_cm = 75; // radars without the query (LD2410, older firmware) use 0.75 m gates
    }
    const esp_err_t eng = command_retry(ld2410::kCmdEngineeringOn, nullptr, 0, nullptr);
    const esp_err_t end = command_retry(ld2410::kCmdEndConfig, nullptr, 0, nullptr);
    lock();
    g.radar.protocol = proto;
    g.radar.gate_cm = gate_cm;
    g.radar.config_read = have_params;
    if (have_params) {
        g.radar.params = params;
    }
    snprintf(g.radar.version, sizeof(g.radar.version), "%s", vtext);
    unlock();
    decision_config_changed(); // the gate length decides whether the model may run
    ESP_LOGI(TAG, "radar %s, protocol %u, gate %u cm, engineering %s, config %s", vtext[0] ? vtext : "(version ?)",
             proto, gate_cm, eng == ESP_OK ? "on" : "FAILED", end == ESP_OK ? "closed" : "NOT closed");
    return eng == ESP_OK && end == ESP_OK;
}

static void control_task(void *)
{
    vTaskDelay(pdMS_TO_TICKS(600)); // radar boot time after power-up
    int64_t last_init_ms = 0;
    bool engineering_confirmed = false;
    bool setup_read = false; // version, gate length and parameters answered at least once
    for (;;) {
        const int64_t now = esp_timer_get_time() / 1000;
        lock();
        const int64_t last = g.radar.last_report_ms;
        const uint32_t other = g.radar.reports_other;
        const uint32_t bytes = g.radar.bytes;
        const RadarStatus prev = g.radar.status;
        unlock();
        RadarStatus st;
        if (last > 0 && now - last < CONFIG_PRESENCE_RADAR_TIMEOUT_MS) {
            st = RadarStatus::Ok;
            engineering_confirmed = true;
        } else if (last == 0 && now < 5000) {
            st = RadarStatus::Init;
        } else if (s_basic_reports_since_init > 10) {
            st = RadarStatus::NotEngineering; // reports arrive, but without per-gate energies
        } else if (other > 10 && last == 0) {
            st = RadarStatus::Layout; // engineering reports without 9 + 9 gates
        } else if (bytes > 512 && last == 0 && other == 0) {
            st = RadarStatus::BadFrames;
        } else {
            st = RadarStatus::NoData; // never treated as an empty room
        }
        if (st != prev) {
            ESP_LOGW(TAG, "radar status %s -> %s", radar_status_name(prev), radar_status_name(st));
            lock();
            g.radar.status = st;
            unlock();
        }

        // (Re)initialize at start, when asked, and every 10 s while the radar is not delivering engineering reports,
        // or while its setup was never read: a radar that powered up after the ESP32 (or was still in engineering mode
        // from before a reset) sends reports, but its gate length is unknown and the model must not run
        const bool due = last_init_ms == 0 || s_reinit_requested ||
                         (st != RadarStatus::Ok && st != RadarStatus::Init && now - last_init_ms > 10000) ||
                         (st == RadarStatus::Ok && !setup_read && now - last_init_ms > 10000);
        if (due) {
            s_reinit_requested = false;
            last_init_ms = now;
            s_basic_reports_since_init = 0;
            if (engineering_confirmed) {
                lock();
                g.radar.reinit_count++;
                unlock();
            }
            setup_read = init_radar() || setup_read;
        }
        vTaskDelay(pdMS_TO_TICKS(250));
    }
}

void radar_request_reinit()
{
    s_reinit_requested = true;
}

esp_err_t radar_start(QueueHandle_t tick_queue)
{
    const RadarSettings &rs = radar_settings();
    s_uart = (uart_port_t)rs.uart_num;
    s_tick_queue = tick_queue;
    s_ack_queue = xQueueCreate(4, sizeof(ld2410::Ack));
    s_cmd_mutex = xSemaphoreCreateMutex();
    static ld2410::Parser parser(on_report, on_ack, nullptr);
    s_parser = &parser;
    const uart_config_t cfg = {
        .baud_rate = rs.baud,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = 0,
        .source_clk = UART_SCLK_DEFAULT,
        .flags = {},
    };
    // ESP_INTR_FLAG_IRAM with CONFIG_UART_ISR_IN_IRAM: bytes keep arriving into the ring buffer while flash is
    // written (model download, NVS), so radar data is not lost during those operations.
    ESP_RETURN_ON_ERROR(uart_driver_install(s_uart, kRxBufSize, 0, 16, &s_uart_events, ESP_INTR_FLAG_IRAM), TAG,
                        "uart install");
    ESP_RETURN_ON_ERROR(uart_param_config(s_uart, &cfg), TAG, "uart config");
    ESP_RETURN_ON_ERROR(uart_set_pin(s_uart, rs.tx_gpio, rs.rx_gpio, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE), TAG,
                        "uart pins");
    lock();
    g.radar.status = RadarStatus::Init;
    g.ticks.boot_id = esp_random();
    g.ticks.t0_ms = uptime_ms();
    unlock();
    // Reader on core 1 with high priority: the network stack runs on core 0
    xTaskCreatePinnedToCore(reader_task, "radar_rx", 4096, nullptr, 12, nullptr, 1);
    xTaskCreatePinnedToCore(control_task, "radar_ctl", 4096, nullptr, 5, nullptr, 1);
    const esp_timer_create_args_t targs = { .callback = tick_cb, .arg = nullptr, .dispatch_method = ESP_TIMER_TASK,
                                            .name = "tick", .skip_unhandled_events = false };
    esp_timer_handle_t timer;
    ESP_RETURN_ON_ERROR(esp_timer_create(&targs, &timer), TAG, "timer");
    ESP_RETURN_ON_ERROR(esp_timer_start_periodic(timer, presence::kSamplePeriodMs * 1000), TAG, "timer start");
    ESP_LOGI(TAG, "LD2410 on UART%d RX=GPIO%d TX=GPIO%d %d baud", rs.uart_num, rs.rx_gpio, rs.tx_gpio, rs.baud);
    return ESP_OK;
}

} // namespace app
