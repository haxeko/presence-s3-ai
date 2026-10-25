// Threshold method (fallback while no valid model is active, and the baseline of the evaluation), same as
// training/presence_ml/threshold.py: a sample is a "hit" when some gate has moving energy > its moving threshold or
// still energy > its still threshold (threshold 100 disables the gate); the window score is hits / valid samples.
#pragma once

#include <stdint.h>

#include "contract_v1.h"
#include "window_features.h"

namespace presence {

constexpr uint8_t kThresholdDisabled = 100;

struct Thresholds {
    uint8_t moving[kGates];
    uint8_t still[kGates];
    bool valid() const;
};

// LD2410 factory sensitivities (0.75 m gates); still gates 0-1 are not settable on the radar, so disabled here.
Thresholds factory_thresholds();

bool sample_hit(const uint8_t energy[kChannels], const Thresholds &thr);
// Returns false when the window has no valid sample.
bool threshold_score(const Window &w, const Thresholds &thr, float *score);

} // namespace presence
