// LD2410 configuration commands: frame building and ACK decoding (no ESP-IDF dependency, host-tested).
// Only read-only queries and the engineering mode switch are used: the firmware never changes the radar's stored
// configuration (thresholds, gates, resolution, baud rate), so every recording keeps the radar setup it reports.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "ld2410_parser.h"

namespace ld2410 {

constexpr uint16_t kCmdEnableConfig = 0x00FF;
constexpr uint16_t kCmdEndConfig = 0x00FE;
constexpr uint16_t kCmdEngineeringOn = 0x0062;
constexpr uint16_t kCmdEngineeringOff = 0x0063;
constexpr uint16_t kCmdReadParams = 0x0061;
constexpr uint16_t kCmdReadVersion = 0x00A0;
constexpr uint16_t kCmdReadResolution = 0x00AB;
constexpr uint16_t kAckFlag = 0x0100;

// Frame for `command` with an optional value; returns its length (0 when out does not fit).
size_t build_command(uint16_t command, const uint8_t *value, size_t value_len, uint8_t *out, size_t out_size);

struct RadarParams {
    uint8_t max_gate;        // highest gate N of the radar (8)
    uint8_t max_moving_gate; // configured farthest moving gate
    uint8_t max_still_gate;  // configured farthest still gate
    uint8_t moving_threshold[kGates];
    uint8_t still_threshold[kGates];
    uint16_t no_one_duration_s;
};

struct RadarVersion {
    uint16_t type;
    uint16_t major;
    uint32_t minor;
};

// 0x00FF ACK value: protocol version (2), buffer size (2)
bool decode_enable_config(const Ack &ack, uint16_t *protocol_version, uint16_t *buffer_size);
// 0x0061 ACK value: 0xAA, N, max moving gate, max still gate, moving thresholds 0..N, still thresholds 0..N,
// no-one duration (2)
bool decode_params(const Ack &ack, RadarParams *out);
// 0x00A0 ACK value: type (2), major (2), minor (4) -> "V2.44.25070917"
bool decode_version(const Ack &ack, RadarVersion *out);
void format_version(const RadarVersion &v, char *buf, size_t len);
// 0x00AB ACK value: resolution (2): 0 = 0.75 m per gate, 1 = 0.2 m (LD2410B/C newer firmware only)
bool decode_resolution(const Ack &ack, uint16_t *gate_cm);

} // namespace ld2410
