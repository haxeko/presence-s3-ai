#include "ld2410_protocol.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

namespace ld2410 {

size_t build_command(uint16_t command, const uint8_t *value, size_t value_len, uint8_t *out, size_t out_size)
{
    const size_t n = 4 + 2 + 2 + value_len + 4;
    if (n > out_size || 2 + value_len > kMaxPayload) {
        return 0;
    }
    static const uint8_t header[4] = { 0xFD, 0xFC, 0xFB, 0xFA };
    static const uint8_t footer[4] = { 0x04, 0x03, 0x02, 0x01 };
    size_t i = 0;
    memcpy(out, header, 4);
    i += 4;
    out[i++] = (uint8_t)((2 + value_len) & 0xFF);
    out[i++] = (uint8_t)((2 + value_len) >> 8);
    out[i++] = (uint8_t)(command & 0xFF);
    out[i++] = (uint8_t)(command >> 8);
    if (value_len) {
        memcpy(&out[i], value, value_len);
        i += value_len;
    }
    memcpy(&out[i], footer, 4);
    return i + 4;
}

bool decode_enable_config(const Ack &ack, uint16_t *protocol_version, uint16_t *buffer_size)
{
    if (ack.command != (kCmdEnableConfig | kAckFlag) || ack.status != 0 || ack.len < 4) {
        return false;
    }
    *protocol_version = (uint16_t)(ack.data[0] | (ack.data[1] << 8));
    *buffer_size = (uint16_t)(ack.data[2] | (ack.data[3] << 8));
    return true;
}

bool decode_params(const Ack &ack, RadarParams *out)
{
    if (ack.command != (kCmdReadParams | kAckFlag) || ack.status != 0 || ack.len < 4) {
        return false;
    }
    const size_t gates = (size_t)ack.data[1] + 1;
    if (ack.data[0] != 0xAA || gates != (size_t)kGates || ack.len < 4 + 2 * kGates + 2) {
        return false;
    }
    out->max_gate = ack.data[1];
    out->max_moving_gate = ack.data[2];
    out->max_still_gate = ack.data[3];
    memcpy(out->moving_threshold, &ack.data[4], kGates);
    memcpy(out->still_threshold, &ack.data[4 + kGates], kGates);
    const size_t d = 4 + 2 * kGates;
    out->no_one_duration_s = (uint16_t)(ack.data[d] | (ack.data[d + 1] << 8));
    return true;
}

bool decode_version(const Ack &ack, RadarVersion *out)
{
    if (ack.command != (kCmdReadVersion | kAckFlag) || ack.status != 0 || ack.len < 8) {
        return false;
    }
    out->type = (uint16_t)(ack.data[0] | (ack.data[1] << 8));
    out->major = (uint16_t)(ack.data[2] | (ack.data[3] << 8));
    out->minor = (uint32_t)ack.data[4] | ((uint32_t)ack.data[5] << 8) | ((uint32_t)ack.data[6] << 16) |
                 ((uint32_t)ack.data[7] << 24);
    return true;
}

void format_version(const RadarVersion &v, char *buf, size_t len)
{
    snprintf(buf, len, "V%x.%02x.%08" PRIx32, v.major >> 8, v.major & 0xFF, v.minor);
}

bool decode_resolution(const Ack &ack, uint16_t *gate_cm)
{
    if (ack.command != (kCmdReadResolution | kAckFlag) || ack.status != 0 || ack.len < 2) {
        return false;
    }
    const uint16_t r = (uint16_t)(ack.data[0] | (ack.data[1] << 8));
    if (r == 0) {
        *gate_cm = 75;
    } else if (r == 1) {
        *gate_cm = 20;
    } else {
        return false;
    }
    return true;
}

} // namespace ld2410
