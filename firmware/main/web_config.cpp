#include "web_config.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_state.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net.h"
#include "sdkconfig.h"
#include "settings.h"
#include "status.h"

namespace app {

#if CONFIG_PRESENCE_WEB_CONFIG

static const char *TAG = "web";

// Fields of the form: settings key, label, secret (kept when left empty, never shown)
struct Field {
    const char *key;
    const char *label;
    bool secret;
};

static const Field kFields[] = {
    { "wifi_ssid", "Tên Wi-Fi (2,4 GHz)", false },
    { "wifi_pass", "Mật khẩu Wi-Fi", true },
    { "mqtt_uri", "Địa chỉ MQTT (mqtt://IP:1883)", false },
    { "mqtt_user", "Tên đăng nhập MQTT", false },
    { "mqtt_pass", "Mật khẩu MQTT", true },
    { "server_url", "Backend (http://IP:8000)", false },
    { "device_token", "Token thiết bị", true },
};

static const char *current_value(const char *key)
{
    const NetSettings &ns = net_settings();
    if (strcmp(key, "wifi_ssid") == 0) {
        return ns.wifi_ssid;
    }
    if (strcmp(key, "mqtt_uri") == 0) {
        return ns.mqtt_uri;
    }
    if (strcmp(key, "mqtt_user") == 0) {
        return ns.mqtt_user;
    }
    if (strcmp(key, "server_url") == 0) {
        return ns.server_url;
    }
    return "";
}

// Appends text with &, <, >, " and ' escaped
static void append_escaped(char *out, size_t size, size_t *n, const char *s)
{
    for (; *s && *n + 7 < size; s++) {
        const char *rep = nullptr;
        switch (*s) {
        case '&':
            rep = "&amp;";
            break;
        case '<':
            rep = "&lt;";
            break;
        case '>':
            rep = "&gt;";
            break;
        case '"':
            rep = "&quot;";
            break;
        case '\'':
            rep = "&#39;";
            break;
        }
        if (rep) {
            *n += snprintf(out + *n, size - *n, "%s", rep);
        } else {
            out[(*n)++] = *s;
        }
    }
    out[*n] = '\0';
}

static void append(char *out, size_t size, size_t *n, const char *s)
{
    *n += snprintf(out + *n, *n < size ? size - *n : 0, "%s", s);
    if (*n >= size) {
        *n = size - 1;
    }
}

static esp_err_t page(httpd_req_t *req)
{
    const size_t size = 6144;
    char *html = (char *)malloc(size);
    char *state = (char *)malloc(512);
    if (!html || !state) {
        free(html);
        free(state);
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no memory");
    }
    status_build_state(state, 512);
    char ip[16];
    net_ip(ip, sizeof(ip));
    lock();
    const char *radar = radar_status_name(g.radar.status);
    const unsigned gate_cm = g.radar.gate_cm;
    unlock();
    size_t n = 0;
    append(html, size, &n,
           "<!doctype html><html lang=vi><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'><title>Cảm biến hiện diện</title>"
           "<style>body{font-family:sans-serif;max-width:560px;margin:16px auto;padding:0 12px}"
           "label{display:block;margin-top:10px;font-size:14px}input{width:100%;padding:8px;box-sizing:border-box}"
           "button{margin-top:16px;padding:10px 16px}pre{background:#f3f3f3;padding:8px;white-space:pre-wrap}"
           "small{color:#666}</style></head><body><h2>Cảm biến hiện diện</h2><p>Thiết bị <b>");
    append_escaped(html, size, &n, g.device_id);
    char line[256];
    snprintf(line, sizeof(line), "</b> &middot; firmware %s &middot; IP %s &middot; Wi-Fi %d dBm</p>"
                                 "<p>MQTT: <b>%s</b> &middot; radar: <b>%s</b>, gate %u cm</p><pre>",
             g.fw_version, ip[0] ? ip : "-", net_rssi(), net_mqtt_connected() ? "đã kết nối" : "chưa kết nối", radar,
             gate_cm);
    append(html, size, &n, line);
    append_escaped(html, size, &n, state);
    append(html, size, &n, "</pre><form method=post action=/save>");
    for (const Field &f : kFields) {
        snprintf(line, sizeof(line), "<label>%s<input name=%s type=%s", f.label, f.key, f.secret ? "password" : "text");
        append(html, size, &n, line);
        if (f.secret) {
            append(html, size, &n, " placeholder='để trống = giữ nguyên'");
        } else {
            append(html, size, &n, " value=\"");
            append_escaped(html, size, &n, current_value(f.key));
            append(html, size, &n, "\"");
        }
        append(html, size, &n, "></label>");
    }
    append(html, size, &n,
           "<button type=submit>Lưu và khởi động lại</button></form>"
           "<p><small>Home Assistant (add-on Mosquitto): địa chỉ mqtt://homeassistant.local:1883 hoặc "
           "mqtt://IP-của-HA:1883, tên/mật khẩu là một người dùng của Home Assistant. Thiết bị tự xuất hiện trong "
           "Home Assistant (MQTT Discovery).</small></p></body></html>");
    free(state);
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    const esp_err_t err = httpd_resp_send(req, html, (ssize_t)n);
    free(html);
    return err;
}

// application/x-www-form-urlencoded value, in place
static void url_decode(char *s)
{
    char *o = s;
    for (; *s; s++) {
        if (*s == '+') {
            *o++ = ' ';
        } else if (*s == '%' && isxdigit((unsigned char)s[1]) && isxdigit((unsigned char)s[2])) {
            const char hex[3] = { s[1], s[2], '\0' };
            *o++ = (char)strtol(hex, nullptr, 16);
            s += 2;
        } else {
            *o++ = *s;
        }
    }
    *o = '\0';
}

static void restart_task(void *)
{
    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
}

static esp_err_t save(httpd_req_t *req)
{
    if (req->content_len == 0 || req->content_len > 2048) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "bad form");
    }
    char *body = (char *)calloc(1, req->content_len + 1);
    if (!body) {
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no memory");
    }
    size_t got = 0;
    while (got < req->content_len) {
        const int r = httpd_req_recv(req, body + got, req->content_len - got);
        if (r <= 0) {
            free(body);
            return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "read error");
        }
        got += (size_t)r;
    }
    char value[200];
    char errors[256] = "";
    int saved = 0;
    for (const Field &f : kFields) {
        if (httpd_query_key_value(body, f.key, value, sizeof(value)) != ESP_OK) {
            continue;
        }
        url_decode(value);
        if (f.secret && !value[0]) {
            continue; // secret left empty: keep the stored one
        }
        if (strcmp(f.key, "wifi_ssid") == 0 && !value[0]) {
            continue; // never remove the Wi-Fi network from here (the page would become unreachable)
        }
        if (!f.secret && strcmp(value, current_value(f.key)) == 0) {
            continue; // unchanged
        }
        if (settings_set(f.key, value)) {
            saved++;
        } else {
            strlcat(errors, f.key, sizeof(errors));
            strlcat(errors, " ", sizeof(errors));
        }
    }
    free(body);
    char html[640];
    snprintf(html, sizeof(html),
             "<!doctype html><html lang=vi><head><meta charset=utf-8>"
             "<meta name=viewport content='width=device-width,initial-scale=1'></head><body style='font-family:sans-serif'>"
             "<p>Đã lưu %d mục.%s%s</p><p>%s</p><p><a href=/>Quay lại</a></p></body></html>",
             saved, errors[0] ? " Lỗi (quá dài?): " : "", errors,
             saved ? "Thiết bị đang khởi động lại để áp dụng (khoảng 10 giây). Nếu đổi Wi-Fi, IP có thể khác."
                   : "Không có gì thay đổi.");
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_sendstr(req, html);
    ESP_LOGI(TAG, "settings saved from the web page: %d changed", saved);
    if (saved) {
        xTaskCreate(restart_task, "restart", 2048, nullptr, 5, nullptr);
    }
    return ESP_OK;
}

void web_config_start()
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 6144;
    cfg.lru_purge_enable = true;
    cfg.max_open_sockets = 3;
    httpd_handle_t server = nullptr;
    if (httpd_start(&server, &cfg) != ESP_OK) {
        ESP_LOGW(TAG, "web page not started");
        return;
    }
    const httpd_uri_t get = { .uri = "/", .method = HTTP_GET, .handler = page, .user_ctx = nullptr };
    const httpd_uri_t post = { .uri = "/save", .method = HTTP_POST, .handler = save, .user_ctx = nullptr };
    httpd_register_uri_handler(server, &get);
    httpd_register_uri_handler(server, &post);
    ESP_LOGI(TAG, "configuration page on port 80");
}

#else

void web_config_start() {}

#endif

} // namespace app
