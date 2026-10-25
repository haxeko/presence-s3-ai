#include "model_selftest.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "window_features.h"

namespace presence {

static uint32_t rd_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static float rd_f32(const uint8_t *p)
{
    const uint32_t u = rd_u32(p);
    float f;
    memcpy(&f, &u, sizeof(f));
    return f;
}

static bool fail(SelfTestReport *r, const char *msg)
{
    snprintf(r->error, sizeof(r->error), "%s", msg);
    r->passed = false;
    return false;
}

bool run_selftest(const ModelContract &c, const uint8_t *tv, size_t len, InferenceEngine &engine,
                  SelfTestReport *r, MicrosFn now_us)
{
    memset(r, 0, sizeof(*r));
    if (!engine.loaded()) {
        return fail(r, "engine_not_loaded");
    }
    r->arena_used = engine.arena_used();
    const EngineQuant q = engine.quant();
    if (q.input_scale != c.input_scale || q.input_zero_point != c.input_zero_point ||
        q.output_scale != c.output_scale || q.output_zero_point != c.output_zero_point) {
        return fail(r, "quantization_mismatch");
    }
    if (len < kTestVecHeaderSize || memcmp(tv, "PST1", 4) != 0) {
        return fail(r, "testvec_header");
    }
    const uint32_t count = rd_u32(tv + 4);
    const uint32_t record_size = rd_u32(tv + 8);
    const int tol_input = (int)rd_u32(tv + 12);
    const int tol_output = (int)rd_u32(tv + 16);
    const float tol_abs = rd_f32(tv + 20);
    const float tol_rel = rd_f32(tv + 24);
    if (record_size != kTestVecRecordSize || count == 0 || count > 1024 ||
        len < kTestVecHeaderSize + (size_t)count * record_size || tol_input < 0 || tol_output < 0) {
        return fail(r, "testvec_size");
    }
    for (uint32_t k = 0; k < count; k++) {
        const uint8_t *rec = tv + kTestVecHeaderSize + (size_t)k * record_size;
        Window w;
        const uint32_t mask = rd_u32(rec);
        const uint8_t *energy = rec + 4;
        for (int i = 0; i < kWindowSlots; i++) {
            w.valid[i] = (mask >> i) & 1u;
            memcpy(w.energy[i], energy + i * kChannels, kChannels);
        }
        const uint8_t *exp_feat = energy + kWindowSlots * kChannels;
        const int8_t *exp_in = (const int8_t *)(exp_feat + 4 * kFeatures);
        const int8_t *exp_out = exp_in + kFeatures;

        float feat[kFeatures];
        if (!compute_features(w, feat)) {
            return fail(r, "testvec_empty_window");
        }
        bool feat_bad = false;
        for (int i = 0; i < kFeatures; i++) {
            const float e = rd_f32(exp_feat + 4 * i);
            const float d = fabsf(feat[i] - e);
            if (d > r->max_feature_diff) {
                r->max_feature_diff = d;
            }
            if (d > tol_abs + tol_rel * fabsf(e)) {
                feat_bad = true;
            }
        }
        r->feature_mismatch += feat_bad ? 1 : 0;

        int8_t in[kFeatures];
        quantize_features(c, feat, in);
        bool in_bad = false;
        for (int i = 0; i < kFeatures; i++) {
            const int d = in[i] > exp_in[i] ? in[i] - exp_in[i] : exp_in[i] - in[i];
            if (d > r->max_input_diff) {
                r->max_input_diff = d;
            }
            in_bad = in_bad || d > tol_input;
        }
        r->input_mismatch += in_bad ? 1 : 0;

        // Inference on the expected input: isolates kernel differences from feature rounding.
        int8_t out[kClassCount];
        const int64_t t0 = now_us ? now_us() : 0;
        if (!engine.invoke(exp_in, out)) {
            return fail(r, "invoke_failed");
        }
        if (now_us) {
            const int64_t dt = now_us() - t0;
            if (dt > (int64_t)r->inference_us_max) {
                r->inference_us_max = (uint32_t)dt;
            }
        }
        bool out_bad = false;
        for (int j = 0; j < kClassCount; j++) {
            const int d = out[j] > exp_out[j] ? out[j] - exp_out[j] : exp_out[j] - out[j];
            if (d > r->max_output_diff) {
                r->max_output_diff = d;
            }
            out_bad = out_bad || d > tol_output;
        }
        r->output_mismatch += out_bad ? 1 : 0;
        r->vectors++;
    }
    if (r->feature_mismatch || r->input_mismatch || r->output_mismatch) {
        return fail(r, "testvec_mismatch");
    }
    r->passed = true;
    return true;
}

} // namespace presence
