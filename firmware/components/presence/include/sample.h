// One 5 Hz sample (200 ms tick) as kept in the offline buffer and sent to the backend (row schema v1, see
// docs/data-format.md). About 52 bytes.
#pragma once

#include <stdint.h>

#include "contract_v1.h"

namespace presence {

// Decision annotation: the latest result of the decision pipeline when the sample was produced.
struct DecisionInfo {
    uint8_t evaluated;      // 1: the pipeline evaluated a window ending at this tick
    uint8_t method;         // kMethod*
    uint32_t model_version; // active model (0 without a model)
    int8_t pred;            // argmax class of the model, -1 none
    int8_t q[kClassCount];  // int8 model outputs (0 without a model)
    int16_t occ_permille;   // occupied score x 1000, -1 when the window was not usable
    uint8_t state;          // kState*
    uint8_t decision;       // kDecision*
};

struct Sample {
    uint32_t seq;  // dense: +1 per sample produced since boot (a gap at the backend = lost sample)
    uint32_t tick; // 200 ms tick since the sampler started (a gap without a seq gap = radar sent nothing)
    uint8_t n_frames;
    uint8_t target_state;
    uint16_t moving_cm;
    uint16_t still_cm;
    uint16_t detect_cm;
    uint8_t moving_energy;
    uint8_t still_energy;
    uint8_t energy[kChannels]; // m0..m8, s0..s8 (0..100)
    int16_t light;             // -1 when the radar does not report it
    int16_t out_pin;
    DecisionInfo info;
};

} // namespace presence
