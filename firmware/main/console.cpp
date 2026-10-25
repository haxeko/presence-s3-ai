// Serial console (UART0 or USB, 115200): configuration without rebuilding the firmware.
//   status                       summary of radar, presence, model, network
//   config show                  network and radar settings (secrets hidden)
//   config set <key> <value>     stored in NVS, applied after "reboot"
//   config clear                 back to the Kconfig defaults
//   mem                          heap (internal RAM, PSRAM) and the free stack of each task: measurements on the board
//   radar_reinit | rollback | reboot
#include "console.h"

#include <stdio.h>
#include <string.h>

#include "decision.h"
#include "esp_console.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "model_update.h"
#include "radar.h"
#include "sdkconfig.h"
#include "settings.h"
#include "status.h"

namespace app {

static int cmd_status(int, char **)
{
    status_print();
    return 0;
}

static int cmd_config(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "show") == 0) {
        settings_print();
        return 0;
    }
    if (argc == 4 && strcmp(argv[1], "set") == 0) {
        if (settings_set(argv[2], argv[3])) {
            printf("saved; type 'reboot' to apply\n");
            return 0;
        }
        printf("unknown key or invalid value\n");
        return 1;
    }
    if (argc == 2 && strcmp(argv[1], "clear") == 0) {
        printf(settings_clear() ? "cleared; type 'reboot'\n" : "failed\n");
        return 0;
    }
    printf("usage: config show | config set <key> <value> | config clear\n"
           "keys: wifi_ssid wifi_pass mqtt_uri mqtt_user mqtt_pass server_url device_token topic_prefix rx_gpio "
           "tx_gpio baud\n");
    return 1;
}

static int cmd_mem(int, char **)
{
    const uint32_t caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
    printf("internal RAM: free %u, lowest since boot %u, largest block %u bytes\n",
           (unsigned)heap_caps_get_free_size(caps), (unsigned)heap_caps_get_minimum_free_size(caps),
           (unsigned)heap_caps_get_largest_free_block(caps));
    if (heap_caps_get_total_size(MALLOC_CAP_SPIRAM) > 0) {
        printf("PSRAM: free %u, lowest %u bytes\n", (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
               (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_SPIRAM));
    }
    uint32_t eval_last, eval_max;
    decision_eval_us(&eval_last, &eval_max);
    printf("window evaluation: last %u us, slowest %u us\n", (unsigned)eval_last, (unsigned)eval_max);
    // Lowest free stack since start (bytes): below about 512 the task's stack size should grow
    static const char *const tasks[] = { "radar_rx", "radar_ctl", "decision", "net", "model_upd", "mqtt_task",
                                         "tiT", "esp_timer", "sys_evt", "console_repl", "main" };
    for (const char *name : tasks) {
        TaskHandle_t h = xTaskGetHandle(name);
        if (h) {
            printf("stack free %-13s %5u bytes\n", name, (unsigned)uxTaskGetStackHighWaterMark(h));
        }
    }
    return 0;
}

static int cmd_reboot(int, char **)
{
    esp_restart();
    return 0;
}

static int cmd_radar_reinit(int, char **)
{
    radar_request_reinit();
    return 0;
}

static int cmd_rollback(int, char **)
{
    printf(model_update_rollback() ? "previous model active\n" : "rollback failed (see log)\n");
    return 0;
}

void console_start()
{
#if CONFIG_PRESENCE_CONSOLE
    esp_console_repl_t *repl = nullptr;
    esp_console_repl_config_t cfg = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    cfg.prompt = "presence>";
    cfg.max_cmdline_length = 256;
    const esp_console_cmd_t cmds[] = {
        { .command = "status", .help = "radar, presence, model, network", .hint = nullptr, .func = cmd_status,
          .argtable = nullptr, .func_w_context = nullptr, .context = nullptr },
        { .command = "config", .help = "config show | set <key> <value> | clear", .hint = nullptr, .func = cmd_config,
          .argtable = nullptr, .func_w_context = nullptr, .context = nullptr },
        { .command = "mem", .help = "heap, slowest window evaluation, free stack of each task", .hint = nullptr,
          .func = cmd_mem, .argtable = nullptr, .func_w_context = nullptr, .context = nullptr },
        { .command = "reboot", .help = "restart", .hint = nullptr, .func = cmd_reboot, .argtable = nullptr,
          .func_w_context = nullptr, .context = nullptr },
        { .command = "radar_reinit", .help = "read radar setup, enable engineering mode again", .hint = nullptr,
          .func = cmd_radar_reinit, .argtable = nullptr, .func_w_context = nullptr, .context = nullptr },
        { .command = "rollback", .help = "switch to the previous model slot", .hint = nullptr, .func = cmd_rollback,
          .argtable = nullptr, .func_w_context = nullptr, .context = nullptr },
    };
    for (const esp_console_cmd_t &c : cmds) {
        esp_console_cmd_register(&c);
    }
    esp_console_register_help_command();
#if CONFIG_ESP_CONSOLE_UART_DEFAULT || CONFIG_ESP_CONSOLE_UART_CUSTOM
    esp_console_dev_uart_config_t dev = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    if (esp_console_new_repl_uart(&dev, &cfg, &repl) != ESP_OK) {
        return;
    }
#elif CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    esp_console_dev_usb_serial_jtag_config_t dev = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    if (esp_console_new_repl_usb_serial_jtag(&dev, &cfg, &repl) != ESP_OK) {
        return;
    }
#else
    return;
#endif
    esp_console_start_repl(repl);
#endif
}

} // namespace app
