#include "model_contract.h"

#include <math.h>
#include <string.h>

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

const char *contract_error_name(ContractError e)
{
    switch (e) {
    case ContractError::Ok:
        return "ok";
    case ContractError::Size:
        return "contract_size";
    case ContractError::Magic:
        return "contract_magic";
    case ContractError::Version:
        return "contract_version";
    case ContractError::Shape:
        return "contract_shape";
    case ContractError::Resolution:
        return "radar_resolution";
    case ContractError::Values:
        return "contract_values";
    }
    return "?";
}

ContractError parse_contract(const uint8_t *d, size_t len, uint32_t radar_gate_cm, ModelContract *out)
{
    if (len != kContractBlockSize) {
        return ContractError::Size;
    }
    if (memcmp(d, "PSC1", 4) != 0) {
        return ContractError::Magic;
    }
    ModelContract c;
    memset(&c, 0, sizeof(c));
    c.contract_version = rd_u32(d + 4);
    c.feature_set_id = rd_u32(d + 8);
    c.feature_count = rd_u32(d + 12);
    c.sample_period_ms = rd_u32(d + 16);
    c.window_slots = rd_u32(d + 20);
    c.hop_slots = rd_u32(d + 24);
    c.min_valid_slots = rd_u32(d + 28);
    c.max_gap_slots = rd_u32(d + 32);
    c.gate_resolution_cm = rd_u32(d + 36);
    c.class_count = rd_u32(d + 40);
    c.occupied_mask = rd_u32(d + 44);
    c.arena_bytes = rd_u32(d + 48);
    c.input_scale = rd_f32(d + 52);
    c.input_zero_point = (int32_t)rd_u32(d + 56);
    c.output_scale = rd_f32(d + 60);
    c.output_zero_point = (int32_t)rd_u32(d + 64);
    c.presence.enter = rd_f32(d + 68);
    c.presence.exit = rd_f32(d + 72);
    c.presence.confirm_occupied_ms = rd_u32(d + 76);
    c.presence.confirm_empty_ms = rd_u32(d + 80);
    c.calib_count = rd_u32(d + 84);
    for (int i = 0; i < kCalibMaxPoints; i++) {
        c.calib_x[i] = rd_f32(d + 88 + 4 * i);
        c.calib_y[i] = rd_f32(d + 152 + 4 * i);
    }
    for (int k = 0; k < kClassCount; k++) {
        memcpy(c.class_names[k], d + 216 + kClassNameLen * k, kClassNameLen);
        c.class_names[k][kClassNameLen - 1] = '\0';
    }
    for (int i = 0; i < kFeatures; i++) {
        c.feature_mean[i] = rd_f32(d + 312 + 4 * i);
        c.feature_std[i] = rd_f32(d + 600 + 4 * i);
    }

    if (c.contract_version != kContractVersion || c.feature_set_id != kFeatureSetId) {
        return ContractError::Version;
    }
    if (c.feature_count != (uint32_t)kFeatures || c.sample_period_ms != kSamplePeriodMs ||
        c.window_slots != (uint32_t)kWindowSlots || c.class_count != (uint32_t)kClassCount ||
        c.hop_slots < 1 || c.hop_slots > c.window_slots || c.min_valid_slots < 1 ||
        c.min_valid_slots > c.window_slots || c.max_gap_slots >= c.window_slots || c.occupied_mask == 0 ||
        c.occupied_mask >= (1u << kClassCount)) {
        return ContractError::Shape;
    }
    if (radar_gate_cm != 0 && c.gate_resolution_cm != radar_gate_cm) {
        return ContractError::Resolution;
    }
    if (!(c.input_scale > 0.0f) || !(c.output_scale > 0.0f) || !isfinite(c.input_scale) ||
        !isfinite(c.output_scale) || c.input_zero_point < -128 || c.input_zero_point > 127 ||
        c.output_zero_point < -128 || c.output_zero_point > 127 || c.calib_count > (uint32_t)kCalibMaxPoints ||
        c.arena_bytes == 0 || !c.presence.valid()) {
        return ContractError::Values;
    }
    for (int i = 0; i < kFeatures; i++) {
        if (!isfinite(c.feature_mean[i]) || !(c.feature_std[i] > 0.0f) || !isfinite(c.feature_std[i])) {
            return ContractError::Values;
        }
    }
    *out = c;
    return ContractError::Ok;
}

void quantize_features(const ModelContract &c, const float features[kFeatures], int8_t out[kFeatures])
{
    for (int i = 0; i < kFeatures; i++) {
        const float z = (features[i] - c.feature_mean[i]) / c.feature_std[i];
        const float v = z / c.input_scale;
        long q;
        if (v >= 4096.0f) {
            q = 127;
        } else if (v <= -4096.0f) {
            q = -128;
        } else {
            q = lroundf(v) + c.input_zero_point;
        }
        out[i] = (int8_t)(q < -128 ? -128 : (q > 127 ? 127 : q));
    }
}

float occupied_score(const ModelContract &c, const int8_t q_out[kClassCount])
{
    int32_t acc = 0;
    for (int k = 0; k < kClassCount; k++) {
        if (c.occupied_mask & (1u << k)) {
            acc += (int32_t)q_out[k] - c.output_zero_point;
        }
    }
    return (float)acc * c.output_scale;
}

int argmax(const int8_t q_out[kClassCount])
{
    int best = 0;
    for (int k = 1; k < kClassCount; k++) {
        if (q_out[k] > q_out[best]) {
            best = k;
        }
    }
    return best;
}

bool calibrate(const ModelContract &c, float score, float *probability)
{
    const int n = (int)c.calib_count;
    if (n == 0) {
        return false;
    }
    if (score <= c.calib_x[0]) {
        *probability = c.calib_y[0];
        return true;
    }
    for (int i = 1; i < n; i++) {
        if (score <= c.calib_x[i]) {
            const float x0 = c.calib_x[i - 1];
            const float x1 = c.calib_x[i];
            const float y0 = c.calib_y[i - 1];
            const float y1 = c.calib_y[i];
            *probability = x1 <= x0 ? y0 : y0 + (y1 - y0) * (score - x0) / (x1 - x0);
            return true;
        }
    }
    *probability = c.calib_y[n - 1];
    return true;
}

} // namespace presence
