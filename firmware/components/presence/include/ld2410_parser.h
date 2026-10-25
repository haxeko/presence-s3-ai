// Streaming parser of the HLK-LD2410 / LD2410B / LD2410C UART protocol (no ESP-IDF dependency, host-tested).
//
// Report frames (radar -> ESP32):
//   F4 F3 F2 F1 | LEN (2, LE) | TYPE AA STATE MV_CM(2) MV_E ST_CM(2) ST_E DET_CM(2) [engineering] 55 00 | F8 F7 F6 F5
//   TYPE 0x02 basic (LEN 13), 0x01 engineering mode: N, M, moving energy of gates 0..N, still energy of gates
//   0..M, then extra bytes (LD2410B/C: light sensor value, OUT pin state).
// Command / ACK frames:
//   FD FC FB FA | LEN (2, LE) | COMMAND (2) [STATUS (2) in an ACK] [value] | 04 03 02 01
//
// The protocol has no CRC: a frame is accepted only when header, LEN (<= 64), footer, the 0xAA head, the 0x55
// tail and the 0x00 check byte are right. A rejected frame is rescanned from its second byte, so a real frame
// that starts inside a corrupted one is still found. Same rules as training/presence_ml/ld2410.py (shared
// fixture firmware/test/fixtures/ld2410_frames.txt).
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace ld2410 {

constexpr int kGates = 9;
constexpr size_t kMaxPayload = 64;
constexpr size_t kMaxFrame = 4 + 2 + kMaxPayload + 4;
constexpr size_t kMaxAckData = 40;
constexpr size_t kMaxAux = 8;

constexpr uint8_t kTypeEngineering = 0x01;
constexpr uint8_t kTypeBasic = 0x02;

struct Report {
    uint8_t type;
    uint8_t target_state; // 0 none, 1 moving, 2 still, 3 both; 4-6 background noise detection (LD2410B/C)
    uint16_t moving_cm;
    uint8_t moving_energy;
    uint16_t still_cm;
    uint8_t still_energy;
    uint16_t detect_cm;
    bool engineering;
    uint8_t max_moving_gate; // N
    uint8_t max_still_gate;  // M
    uint8_t moving[kGates];  // gates 0..N valid
    uint8_t still[kGates];   // gates 0..M valid
    uint8_t aux_len;
    uint8_t aux[kMaxAux];

    // 9 moving and 9 still gates: required by the 18 channel model contract
    bool full_layout() const
    {
        return engineering && max_moving_gate == kGates - 1 && max_still_gate == kGates - 1;
    }
    int light() const { return aux_len >= 1 ? aux[0] : -1; }
    int out_pin() const { return aux_len >= 2 ? aux[1] : -1; }
};

struct Ack {
    uint16_t command; // command word | 0x0100
    uint16_t status;  // 0 = success
    uint8_t len;
    uint8_t data[kMaxAckData];
};

struct ParserStats {
    uint32_t bytes;
    uint32_t reports;
    uint32_t acks;
    uint32_t bad_length;
    uint32_t bad_footer;
    uint32_t bad_payload;
    uint32_t discarded;
};

// Decodes and checks the payload of a report frame; false when it is malformed.
bool parse_report_payload(const uint8_t *p, size_t len, Report *out);

class Parser {
public:
    typedef void (*ReportFn)(const Report &report, void *ctx);
    typedef void (*AckFn)(const Ack &ack, void *ctx);

    Parser(ReportFn on_report, AckFn on_ack, void *ctx);
    void feed(const uint8_t *data, size_t len);
    void reset();
    const ParserStats &stats() const { return stats_; }

private:
    void push(uint8_t b);
    void requeue(const uint8_t *bytes, size_t n);
    bool finish_frame(); // validates the complete frame in buf_, emits it

    ReportFn on_report_;
    AckFn on_ack_;
    void *ctx_;
    uint8_t buf_[kMaxFrame];
    size_t pos_;
    size_t total_;
    // Bytes waiting to be (re)scanned after a rejected frame. Never more than one frame plus one byte.
    uint8_t pending_[kMaxFrame + 1];
    size_t pending_len_;
    ParserStats stats_;
};

} // namespace ld2410
