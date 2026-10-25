// Sliding window of the last 20 ticks and the 72 features of contract v1, bit-identical to
// training/presence_ml/contract.py (integer sums, then one float32 division / square root per value).
#pragma once

#include <stdint.h>

#include "contract_v1.h"

namespace presence {

struct Window {
    bool valid[kWindowSlots];               // oldest first
    uint8_t energy[kWindowSlots][kChannels];
    int n_valid() const;
    int longest_gap() const;
    bool usable(int min_valid, int max_gap) const;
};

// Last kWindowSlots ticks. Ticks that never got a sample (radar silent, report rejected) stay invalid.
class SlotWindow {
public:
    SlotWindow();
    void reset();
    // Records tick `tick` (energy == nullptr: no sample). Skipped ticks are recorded as missing.
    void push(uint32_t tick, const uint8_t *energy);
    // Window of the ticks [last_tick - 19, last_tick], oldest first.
    void snapshot(Window *out) const;
    bool started() const { return started_; }
    uint32_t last_tick() const { return last_tick_; }

private:
    bool started_;
    uint32_t last_tick_;
    int head_; // index of the slot holding last_tick_
    bool valid_[kWindowSlots];
    uint8_t energy_[kWindowSlots][kChannels];
};

// Returns false when the window has no valid slot.
bool compute_features(const Window &w, float out[kFeatures]);

} // namespace presence
