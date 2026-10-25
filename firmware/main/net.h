// Network: Wi-Fi station (or OpenCores Ethernet under QEMU), SNTP, MQTT.
// Samples leave the offline buffer only when the broker acknowledged them (QoS 1 PUBACK): at-least-once
// delivery, duplicates removed by the backend (boot id + seq). The network task never touches the radar.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace app {

void net_start();
bool net_mqtt_connected();
bool net_time_synced();
int net_rssi();
void net_ip(char *buf, size_t len);
// Publishes on presence/v1/dev/<id>/<suffix> (QoS 1). Returns false when not connected.
bool net_publish(const char *suffix, const char *json, bool retain);
void net_request_status();

} // namespace app
