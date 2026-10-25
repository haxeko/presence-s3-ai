// Test vectors of a model package (section TESTVEC, "PST1") and the self-test run before a model is activated:
// raw 20 x 18 window -> features (C++ vs Python) -> normalization + int8 quantization -> inference -> int8
// outputs, each compared with the values the training machine computed, within the package tolerances.
//
//   header (32 bytes): "PST1", count, record_size (732), tol_input_q (i32), tol_output_q (i32),
//                      tol_feature_abs (f32), tol_feature_rel (f32), reserved
//   record: valid_mask (u32, bit i = slot i), energy[20][18] (u8), features[72] (f32), input_q[72] (i8),
//           output_q[4] (i8), expected_class (u8), pad[3]
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "inference_engine.h"
#include "model_contract.h"

namespace presence {

constexpr size_t kTestVecHeaderSize = 32;
constexpr size_t kTestVecRecordSize = 4 + kWindowSlots * kChannels + 4 * kFeatures + kFeatures + kClassCount + 4;

struct SelfTestReport {
    uint32_t vectors;
    uint32_t feature_mismatch;
    uint32_t input_mismatch;
    uint32_t output_mismatch;
    float max_feature_diff;
    int max_input_diff;
    int max_output_diff;
    uint32_t inference_us_max; // slowest single inference (0 when no clock was given)
    size_t arena_used;
    bool passed;
    char error[48];
};

typedef int64_t (*MicrosFn)();

// Also checks that the engine's input/output quantization equals the contract's.
bool run_selftest(const ModelContract &c, const uint8_t *testvec, size_t len, InferenceEngine &engine,
                  SelfTestReport *report, MicrosFn now_us = nullptr);

} // namespace presence
