// Turns radar reports (~10 Hz) into 5 Hz samples, same rules as training/presence_ml/sampler.py:
// tick k closes the bin of the last 200 ms; its sample holds the per-channel MAXIMUM energy (capped at 100) of the
// engineering reports with all 9 + 9 gates received in the bin, and the target state, distances and extra bytes of
// the last of them. A bin without such a report produces no sample: old samples are never repeated.
#pragma once

#include <stdint.h>

#include "ld2410_parser.h"
#include "sample.h"

namespace presence {

class Sampler {
public:
    Sampler();
    // Returns false when the report cannot be used for the 18 channel contract (basic report, fewer gates).
    bool add_report(const ld2410::Report &r);
    // Closes the bin of `tick`. Returns true and fills *out (seq assigned) when the bin had a usable report.
    bool close_tick(uint32_t tick, Sample *out);

    uint32_t reports_used() const { return reports_used_; }
    uint32_t reports_rejected() const { return reports_rejected_; }
    uint32_t empty_ticks() const { return empty_ticks_; }
    uint32_t next_seq() const { return next_seq_; }

private:
    void clear_bin();

    uint8_t count_;
    uint8_t energy_[kChannels];
    ld2410::Report last_;
    uint32_t next_seq_;
    uint32_t reports_used_;
    uint32_t reports_rejected_;
    uint32_t empty_ticks_;
};

} // namespace presence
