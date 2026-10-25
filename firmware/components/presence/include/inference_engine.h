// INT8 classifier engine: TensorFlow Lite Micro on the ESP32 (platform/tflm_engine.cpp, also built on the host
// with the reference kernels), a fake in the host tests of the update logic.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "contract_v1.h"

namespace presence {

struct EngineQuant {
    float input_scale;
    int32_t input_zero_point;
    float output_scale;
    int32_t output_zero_point;
};

class InferenceEngine {
public:
    virtual ~InferenceEngine() {}
    // The flatbuffer must stay valid until unload(). err receives a short reason on failure.
    virtual bool load(const uint8_t *tflite, size_t len, size_t arena_bytes, char *err, size_t err_len) = 0;
    virtual void unload() = 0;
    virtual bool loaded() const = 0;
    // in: kFeatures int8 values, out: kClassCount int8 values
    virtual bool invoke(const int8_t *in, int8_t *out) = 0;
    virtual size_t arena_used() const = 0;
    virtual EngineQuant quant() const = 0;
};

} // namespace presence
