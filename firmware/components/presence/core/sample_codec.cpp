#include "sample_codec.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

namespace presence {

static int encode_row(const Sample &s, char *out, size_t size)
{
    const DecisionInfo &d = s.info;
    int n = snprintf(out, size, "[%" PRIu32 ",%" PRIu32 ",%u,%u,%u,%u,%u,%u,%u", s.seq, s.tick, s.n_frames,
                     s.target_state, s.moving_cm, s.moving_energy, s.still_cm, s.still_energy, s.detect_cm);
    for (int c = 0; c < kChannels && n > 0 && (size_t)n < size; c++) {
        n += snprintf(out + n, size - n, ",%u", s.energy[c]);
    }
    if (n <= 0 || (size_t)n >= size) {
        return -1;
    }
    n += snprintf(out + n, size - n, ",%d,%d,%u,%u,%" PRIu32 ",%d,%d,%d,%d,%d,%d,%u,%u]", s.light, s.out_pin,
                  d.evaluated, d.method, d.model_version, d.pred, d.q[0], d.q[1], d.q[2], d.q[3], d.occ_permille,
                  d.state, d.decision);
    return (size_t)n < size ? n : -1;
}

size_t encode_batch(const BatchHeader &h, const Sample *samples, size_t n, char *out, size_t out_size, size_t *len)
{
    *len = 0;
    int pos = snprintf(out, out_size,
                       "{\"v\":%d,\"dev\":\"%s\",\"boot\":%" PRIu32 ",\"t0\":%" PRIu32 ",\"now\":%" PRIu32
                       ",\"epoch\":%" PRIu64 ",\"rows\":[",
                       kRowSchemaVersion, h.device_id, h.boot_id, h.t0_ms, h.now_ms, h.epoch_ms);
    if (pos <= 0 || (size_t)pos + 3 > out_size) {
        return 0;
    }
    size_t written = 0;
    char row[320];
    for (size_t i = 0; i < n; i++) {
        const int r = encode_row(samples[i], row, sizeof(row));
        // Room for the row, a comma, and the closing "]}"
        if (r < 0 || (size_t)pos + (size_t)r + 4 > out_size) {
            break;
        }
        if (written > 0) {
            out[pos++] = ',';
        }
        memcpy(out + pos, row, (size_t)r);
        pos += r;
        written++;
    }
    out[pos++] = ']';
    out[pos++] = '}';
    out[pos] = '\0';
    *len = (size_t)pos;
    return written;
}

} // namespace presence
