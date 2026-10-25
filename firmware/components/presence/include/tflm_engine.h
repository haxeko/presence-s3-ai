// InferenceEngine backed by TensorFlow Lite Micro (esp-tflite-micro, esp-nn optimized kernels on the ESP32-S3).
// Also compiled on the host with the TFLM reference kernels (firmware/test/host/tflm_host.cmake).
#pragma once

#include "inference_engine.h"

namespace presence {

class TflmEngine : public InferenceEngine {
public:
    TflmEngine();
    ~TflmEngine() override;
    bool load(const uint8_t *tflite, size_t len, size_t arena_bytes, char *err, size_t err_len) override;
    void unload() override;
    bool loaded() const override { return interpreter_ != nullptr; }
    bool invoke(const int8_t *in, int8_t *out) override;
    size_t arena_used() const override { return arena_used_; }
    EngineQuant quant() const override { return quant_; }

private:
    void *interpreter_; // tflite::MicroInterpreter
    void *resolver_;    // tflite::MicroMutableOpResolver<N>
    uint8_t *arena_;
    size_t arena_used_;
    EngineQuant quant_;
};

} // namespace presence
