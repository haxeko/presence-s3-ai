// Binary contract block of a model package (section CONTRACT, 888 bytes, little-endian), written by
// training/presence_ml/contract.py ContractBlock.pack(). Layout in docs/model-package.md.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "contract_v1.h"
#include "presence_logic.h"

namespace presence {

constexpr size_t kContractBlockSize = 888;
constexpr int kCalibMaxPoints = 16;
constexpr int kClassNameLen = 24;

struct ModelContract {
    uint32_t contract_version;
    uint32_t feature_set_id;
    uint32_t feature_count;
    uint32_t sample_period_ms;
    uint32_t window_slots;
    uint32_t hop_slots;
    uint32_t min_valid_slots;
    uint32_t max_gap_slots;
    uint32_t gate_resolution_cm;
    uint32_t class_count;
    uint32_t occupied_mask;
    uint32_t arena_bytes;
    float input_scale;
    int32_t input_zero_point;
    float output_scale;
    int32_t output_zero_point;
    PresenceParams presence; // recommended by the training run
    uint32_t calib_count;
    float calib_x[kCalibMaxPoints];
    float calib_y[kCalibMaxPoints];
    char class_names[kClassCount][kClassNameLen];
    float feature_mean[kFeatures];
    float feature_std[kFeatures];
};

enum class ContractError {
    Ok = 0,
    Size,
    Magic,
    Version,     // contract or feature set this firmware does not implement
    Shape,       // features, window, rate or classes differ from contract v1
    Resolution,  // trained on another radar gate resolution
    Values,      // scales, normalization or presence parameters out of range
};

const char *contract_error_name(ContractError e);

// Parses and checks the block. radar_gate_cm: gate length the radar reports (0: unknown, not checked).
ContractError parse_contract(const uint8_t *data, size_t len, uint32_t radar_gate_cm, ModelContract *out);

// q = clamp(lroundf(((f - mean) / std) / input_scale) + input_zero_point, -128, 127), as contract.py
void quantize_features(const ModelContract &c, const float features[kFeatures], int8_t out[kFeatures]);
// Sum of the dequantized outputs of the occupied classes
float occupied_score(const ModelContract &c, const int8_t q_out[kClassCount]);
int argmax(const int8_t q_out[kClassCount]);
// Linear interpolation in the calibration table; returns false without a table.
bool calibrate(const ModelContract &c, float score, float *probability);

} // namespace presence
