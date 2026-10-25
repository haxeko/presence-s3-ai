#include "ld2410_parser.h"

#include <string.h>

namespace ld2410 {

static const uint8_t kReportHeader[4] = { 0xF4, 0xF3, 0xF2, 0xF1 };
static const uint8_t kReportFooter[4] = { 0xF8, 0xF7, 0xF6, 0xF5 };
static const uint8_t kCmdHeader[4] = { 0xFD, 0xFC, 0xFB, 0xFA };
static const uint8_t kCmdFooter[4] = { 0x04, 0x03, 0x02, 0x01 };

bool parse_report_payload(const uint8_t *p, size_t n, Report *out)
{
    if (n < 13 || (p[0] != kTypeEngineering && p[0] != kTypeBasic) || p[1] != 0xAA) {
        return false;
    }
    if (p[n - 2] != 0x55 || p[n - 1] != 0x00) {
        return false;
    }
    Report r;
    memset(&r, 0, sizeof(r));
    r.type = p[0];
    r.target_state = p[2];
    r.moving_cm = (uint16_t)(p[3] | (p[4] << 8));
    r.moving_energy = p[5];
    r.still_cm = (uint16_t)(p[6] | (p[7] << 8));
    r.still_energy = p[8];
    r.detect_cm = (uint16_t)(p[9] | (p[10] << 8));
    if (p[0] == kTypeEngineering) {
        if (n < 13 + 2 + 2) {
            return false;
        }
        const size_t nm = p[11];
        const size_t ns = p[12];
        if (nm > kGates - 1 || ns > kGates - 1) {
            return false;
        }
        const size_t end = 13 + (nm + 1) + (ns + 1);
        if (end > n - 2) {
            return false;
        }
        r.engineering = true;
        r.max_moving_gate = (uint8_t)nm;
        r.max_still_gate = (uint8_t)ns;
        memcpy(r.moving, &p[13], nm + 1);
        memcpy(r.still, &p[13 + nm + 1], ns + 1);
        const size_t aux = n - 2 - end;
        r.aux_len = (uint8_t)(aux < kMaxAux ? aux : kMaxAux);
        memcpy(r.aux, &p[end], r.aux_len);
    } else if (n != 13) {
        return false;
    }
    *out = r;
    return true;
}

Parser::Parser(ReportFn on_report, AckFn on_ack, void *ctx) : on_report_(on_report), on_ack_(on_ack), ctx_(ctx)
{
    reset();
}

void Parser::reset()
{
    pos_ = 0;
    total_ = 0;
    pending_len_ = 0;
    memset(&stats_, 0, sizeof(stats_));
}

void Parser::feed(const uint8_t *data, size_t len)
{
    stats_.bytes += (uint32_t)len;
    for (size_t i = 0; i < len; i++) {
        push(data[i]);
        // Rescan bytes left over by rejected frames (oldest first) before the next new byte.
        while (pending_len_ > 0) {
            const uint8_t b = pending_[0];
            memmove(pending_, pending_ + 1, --pending_len_);
            push(b);
        }
    }
}

// Puts bytes in front of the pending queue (they were received before the queued ones).
void Parser::requeue(const uint8_t *bytes, size_t n)
{
    if (n == 0) {
        return;
    }
    if (n + pending_len_ > sizeof(pending_)) {
        // Cannot happen (buffered + pending bytes never exceed one frame plus one byte); drop rather than overflow.
        stats_.discarded += (uint32_t)n;
        return;
    }
    memmove(pending_ + n, pending_, pending_len_);
    memcpy(pending_, bytes, n);
    pending_len_ += n;
}

void Parser::push(uint8_t b)
{
    if (pos_ == 0) {
        if (b == kReportHeader[0] || b == kCmdHeader[0]) {
            buf_[pos_++] = b;
        } else {
            stats_.discarded++;
        }
        return;
    }
    const uint8_t *header = buf_[0] == kReportHeader[0] ? kReportHeader : kCmdHeader;
    if (pos_ < 4) {
        if (b == header[pos_]) {
            buf_[pos_++] = b;
        } else {
            // Not a header after all: drop its first byte, rescan the others and this byte.
            uint8_t rest[4];
            const size_t n = pos_ - 1;
            memcpy(rest, buf_ + 1, n);
            rest[n] = b;
            stats_.discarded++;
            pos_ = 0;
            requeue(rest, n + 1);
        }
        return;
    }
    buf_[pos_++] = b;
    if (pos_ == 6) {
        const size_t length = (size_t)(buf_[4] | (buf_[5] << 8));
        if (length > kMaxPayload) {
            stats_.bad_length++;
            uint8_t rest[6];
            memcpy(rest, buf_ + 1, 5);
            pos_ = 0;
            requeue(rest, 5);
            return;
        }
        total_ = 4 + 2 + length + 4;
    }
    if (pos_ < 6 || pos_ < total_) {
        return;
    }
    uint8_t frame[kMaxFrame];
    const size_t n = pos_;
    memcpy(frame, buf_, n);
    const bool ok = finish_frame();
    pos_ = 0;
    if (!ok) {
        // A real frame may start inside the rejected one.
        stats_.discarded++;
        requeue(frame + 1, n - 1);
    }
}

bool Parser::finish_frame()
{
    const bool is_report = buf_[0] == kReportHeader[0];
    const uint8_t *footer = is_report ? kReportFooter : kCmdFooter;
    const size_t length = total_ - 10;
    if (memcmp(&buf_[total_ - 4], footer, 4) != 0) {
        stats_.bad_footer++;
        return false;
    }
    const uint8_t *payload = &buf_[6];
    if (is_report) {
        Report r;
        if (!parse_report_payload(payload, length, &r)) {
            stats_.bad_payload++;
            return false;
        }
        stats_.reports++;
        if (on_report_) {
            on_report_(r, ctx_);
        }
        return true;
    }
    if (length < 4) {
        stats_.bad_payload++;
        return false;
    }
    Ack ack;
    memset(&ack, 0, sizeof(ack));
    ack.command = (uint16_t)(payload[0] | (payload[1] << 8));
    ack.status = (uint16_t)(payload[2] | (payload[3] << 8));
    ack.len = (uint8_t)(length - 4 < kMaxAckData ? length - 4 : kMaxAckData);
    memcpy(ack.data, &payload[4], ack.len);
    stats_.acks++;
    if (on_ack_) {
        on_ack_(ack, ctx_);
    }
    return true;
}

} // namespace ld2410
