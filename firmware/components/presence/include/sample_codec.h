// JSON batch of samples published on presence/v1/dev/<id>/samples (row schema v1, docs/data-format.md):
// {"v":1,"dev":"s3-a1b2c3","boot":123,"t0":5012,"now":98000,"epoch":1761400000000,"rows":[[...],...]}
// t0: uptime (ms) when tick 0 started; a sample of tick k covers [t0 + 200 k, t0 + 200 (k + 1)).
// Row: seq, tick, nf, rs, mv_cm, mv_e, st_cm, st_e, det_cm, m0..m8, s0..s8, light, out, ev, meth, mver, pred,
//      q0..q3, occ (permille, -1 none), ps, pd  (40 integers)
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "sample.h"

namespace presence {

constexpr int kRowSchemaVersion = 1;
constexpr int kRowColumns = 40;

struct BatchHeader {
    const char *device_id;
    uint32_t boot_id;
    uint32_t t0_ms;
    uint32_t now_ms;
    uint64_t epoch_ms; // 0 when the clock is not set
};

// Writes as many samples as fit (at most n). Returns the number written; *len receives the JSON length.
size_t encode_batch(const BatchHeader &h, const Sample *samples, size_t n, char *out, size_t out_size, size_t *len);

} // namespace presence
