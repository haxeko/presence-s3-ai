// Bounded buffer of samples waiting to be sent, kept in RAM only (never written to flash per sample).
// Drop policy: when full, the OLDEST sample is overwritten and counted in dropped(); the backend also sees the
// loss as a gap in `seq`. Not thread safe: the caller holds a lock (radar task pushes, network task sends).
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "sample.h"

namespace presence {

class SampleRing {
public:
    SampleRing(Sample *storage, size_t capacity);
    void push(const Sample &s);
    // Copies up to `max` samples, oldest first, without removing them.
    size_t peek(Sample *out, size_t max) const;
    // Same, starting at the first sample with seq >= first_seq (samples already sent and awaiting their ACK are
    // skipped).
    size_t peek_from(uint32_t first_seq, Sample *out, size_t max) const;
    // Removes the samples with seq <= `seq` (those sent). Samples already dropped are simply gone.
    void consume_through(uint32_t seq);
    size_t size() const { return count_; }
    size_t capacity() const { return capacity_; }
    uint32_t dropped() const { return dropped_; }
    size_t high_water() const { return high_water_; }

private:
    Sample *buf_;
    size_t capacity_;
    size_t head_; // oldest
    size_t count_;
    uint32_t dropped_;
    size_t high_water_;
};

} // namespace presence
