// Decision pipeline run on every 200 ms tick, same as training/presence_ml/pipeline.py (used by the replay
// evaluation): sliding window of 20 ticks -> every hop (tick % 5 == 4) either the model (features ->
// normalization -> int8 -> inference -> occupied score) or the threshold method -> presence logic.
#pragma once

#include <stdint.h>

#include "window_features.h"
#include "model_contract.h"
#include "presence_logic.h"
#include "sample.h"
#include "threshold_detector.h"

namespace presence {

// What the pipeline needs from the model manager (host tests use a fake).
class ModelRunner {
public:
    virtual ~ModelRunner() {}
    virtual bool model_available() const = 0;
    virtual const ModelContract &model_contract() const = 0;
    virtual uint32_t model_version() const = 0;
    virtual bool model_infer(const int8_t in[kFeatures], int8_t out[kClassCount]) = 0;
};

struct Decision {
    uint32_t tick;
    uint8_t method;
    bool usable; // the window met the missing-data rules
    float score; // occupied score (when usable)
    int8_t q[kClassCount];
    int8_t pred; // -1 without model output
    uint32_t model_version;
    Evidence evidence;
    uint8_t state;
    uint8_t decision;
};

struct PipelineConfig {
    PresenceParams model_params;     // usually the package recommendation, may be overridden
    PresenceParams threshold_params; // for the threshold method
    Thresholds thresholds;
    bool baseline_thresholds;        // thresholds come from a learned empty room (method kMethodThresholdBaseline)
    bool prefer_threshold;           // run the threshold method even when a model is active
};

class DecisionPipeline {
public:
    DecisionPipeline(ModelRunner *models, const PipelineConfig &config);
    bool set_config(const PipelineConfig &config);
    const PipelineConfig &config() const { return config_; }
    // Every tick, in order (sample == nullptr: no sample for this tick). Returns true and fills *out when a
    // decision was taken at this tick.
    bool on_tick(uint32_t tick, const Sample *sample, Decision *out);
    uint8_t method() const;
    const Decision &last() const { return last_; }
    const PresenceLogic &logic() const { return logic_; }
    const SlotWindow &window() const { return window_; }

private:
    ModelRunner *models_;
    PipelineConfig config_;
    SlotWindow window_;
    PresenceLogic logic_;
    uint8_t logic_method_;
    uint32_t logic_model_version_;
    Decision last_;
};

} // namespace presence
