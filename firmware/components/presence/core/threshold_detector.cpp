#include "threshold_detector.h"

namespace presence {

bool Thresholds::valid() const
{
    for (int g = 0; g < kGates; g++) {
        if (moving[g] > 100 || still[g] > 100) {
            return false;
        }
    }
    return true;
}

Thresholds factory_thresholds()
{
    return Thresholds{
        { 50, 50, 40, 30, 20, 15, 15, 15, 15 },
        { kThresholdDisabled, kThresholdDisabled, 40, 40, 30, 30, 20, 20, 20 },
    };
}

bool sample_hit(const uint8_t energy[kChannels], const Thresholds &thr)
{
    for (int g = 0; g < kGates; g++) {
        if (thr.moving[g] < kThresholdDisabled && energy[g] > thr.moving[g]) {
            return true;
        }
        if (thr.still[g] < kThresholdDisabled && energy[kGates + g] > thr.still[g]) {
            return true;
        }
    }
    return false;
}

bool threshold_score(const Window &w, const Thresholds &thr, float *score)
{
    int n = 0;
    int hits = 0;
    for (int i = 0; i < kWindowSlots; i++) {
        if (!w.valid[i]) {
            continue;
        }
        n++;
        hits += sample_hit(w.energy[i], thr) ? 1 : 0;
    }
    if (n == 0) {
        return false;
    }
    *score = (float)hits / (float)n;
    return true;
}

} // namespace presence
