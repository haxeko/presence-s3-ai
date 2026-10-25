// Occupied / empty / uncertain with confirmation thresholds and timers, same as
// training/presence_ml/presence_logic.py (shared fixture firmware/test/fixtures/presence.txt).
//
// One step per hop (1 s) with the occupied score of the window, or no score when the radar data is missing:
//  - evidence HIGH (score >= enter), LOW (score <= exit), MID (in between), NONE (no data);
//  - the decision becomes occupied after HIGH for confirm_occupied_ms, empty after LOW for confirm_empty_ms;
//    MID and NONE restart both timers, so missing radar data never leads to "empty";
//  - the reported state is uncertain before the first decision, while data is missing, and while opposite
//    evidence has not lasted long enough; the previous decision is kept meanwhile.
// The score is a model score (or the share of samples above the thresholds), not a calibrated probability.
#pragma once

#include <stdint.h>

#include "contract_v1.h"

namespace presence {

struct PresenceParams {
    float enter;
    float exit;
    uint32_t confirm_occupied_ms;
    uint32_t confirm_empty_ms;

    bool valid() const;
};

PresenceParams model_presence_defaults();
PresenceParams threshold_presence_defaults();

enum class Evidence : uint8_t { None = 0, Low = 1, Mid = 2, High = 3 };

class PresenceLogic {
public:
    explicit PresenceLogic(const PresenceParams &params, uint32_t hop_ms = kHopSlots * kSamplePeriodMs);
    void reset();
    // Keeps the decision, restarts the timers (method or parameters changed).
    bool set_params(const PresenceParams &params);
    const PresenceParams &params() const { return params_; }

    Evidence step(const float *score); // nullptr: no data
    uint8_t state() const { return state_; }
    uint8_t decision() const { return decision_; }
    uint32_t high_ms() const { return high_ms_; }
    uint32_t low_ms() const { return low_ms_; }

private:
    PresenceParams params_;
    uint32_t hop_ms_;
    uint8_t decision_;
    uint8_t state_;
    uint32_t high_ms_;
    uint32_t low_ms_;
};

} // namespace presence
