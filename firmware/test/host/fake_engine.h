// Fake INT8 engine for the model update tests, same function as training/presence_ml/fixtures.py fake_infer():
// the "tflite" blob is "FAKEMODEL\0" + input scale, input zero point, output scale, output zero point, and
// output k = clamp(sum(in[18k .. 18k + 17]) >> 4, -128, 127).
#pragma once

#include <stdio.h>
#include <string.h>

#include "inference_engine.h"

class FakeEngine : public presence::InferenceEngine {
public:
    int loads = 0;
    int invokes = 0;
    bool fail_load = false;

    bool load(const uint8_t *tflite, size_t len, size_t arena_bytes, char *err, size_t err_len) override
    {
        loads++;
        if (fail_load || len < 26 || memcmp(tflite, "FAKEMODEL\0", 10) != 0) {
            snprintf(err, err_len, "not a fake model");
            return false;
        }
        memcpy(&q_.input_scale, tflite + 10, 4);
        memcpy(&q_.input_zero_point, tflite + 14, 4);
        memcpy(&q_.output_scale, tflite + 18, 4);
        memcpy(&q_.output_zero_point, tflite + 22, 4);
        arena_ = arena_bytes / 2;
        loaded_ = true;
        model_ = tflite;
        return true;
    }
    void unload() override
    {
        loaded_ = false;
        model_ = nullptr;
    }
    bool loaded() const override { return loaded_; }
    bool invoke(const int8_t *in, int8_t *out) override
    {
        if (!loaded_ || model_[0] != 'F') { // the flatbuffer must still be readable
            return false;
        }
        invokes++;
        for (int k = 0; k < presence::kClassCount; k++) {
            int32_t s = 0;
            for (int i = 0; i < 18; i++) {
                s += in[18 * k + i];
            }
            s >>= 4;
            out[k] = (int8_t)(s < -128 ? -128 : (s > 127 ? 127 : s));
        }
        return true;
    }
    size_t arena_used() const override { return arena_; }
    presence::EngineQuant quant() const override { return q_; }

private:
    bool loaded_ = false;
    const uint8_t *model_ = nullptr;
    size_t arena_ = 0;
    presence::EngineQuant q_{};
};
