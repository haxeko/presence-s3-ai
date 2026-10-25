#include "pipeline.h"

#include <string.h>

namespace presence {

DecisionPipeline::DecisionPipeline(ModelRunner *models, const PipelineConfig &config)
    : models_(models), config_(config), logic_(config.threshold_params), logic_method_(kMethodNone),
      logic_model_version_(0)
{
    memset(&last_, 0, sizeof(last_));
    last_.pred = -1;
}

bool DecisionPipeline::set_config(const PipelineConfig &config)
{
    if (!config.model_params.valid() || !config.threshold_params.valid() || !config.thresholds.valid()) {
        return false;
    }
    config_ = config;
    logic_method_ = kMethodNone; // parameters are applied at the next decision
    return true;
}

uint8_t DecisionPipeline::method() const
{
    if (!config_.prefer_threshold && models_ && models_->model_available()) {
        return kMethodModel;
    }
    return config_.baseline_thresholds ? kMethodThresholdBaseline : kMethodThresholdFactory;
}

bool DecisionPipeline::on_tick(uint32_t tick, const Sample *sample, Decision *out)
{
    window_.push(tick, sample ? sample->energy : nullptr);
    const uint8_t m = method();
    const bool use_model = m == kMethodModel;
    const ModelContract *c = use_model ? &models_->model_contract() : nullptr;
    const uint32_t hop = c ? c->hop_slots : (uint32_t)kHopSlots;
    if ((tick + 1) % hop != 0) {
        return false;
    }
    const uint32_t version = use_model ? models_->model_version() : 0;
    if (m != logic_method_ || version != logic_model_version_) {
        // Method or model changed: keep the decision, use the matching parameters, restart the timers.
        logic_.set_params(use_model ? config_.model_params : config_.threshold_params);
        logic_method_ = m;
        logic_model_version_ = version;
    }

    Decision d;
    memset(&d, 0, sizeof(d));
    d.tick = tick;
    d.method = m;
    d.pred = -1;
    d.model_version = version;
    Window w;
    window_.snapshot(&w);
    const int min_valid = c ? (int)c->min_valid_slots : kMinValidSlots;
    const int max_gap = c ? (int)c->max_gap_slots : kMaxGapSlots;
    d.usable = w.usable(min_valid, max_gap);
    if (d.usable) {
        if (use_model) {
            float f[kFeatures];
            int8_t in[kFeatures];
            compute_features(w, f);
            quantize_features(*c, f, in);
            if (models_->model_infer(in, d.q)) {
                d.score = occupied_score(*c, d.q);
                d.pred = (int8_t)argmax(d.q);
            } else {
                d.usable = false; // treated like missing data, never as "empty"
            }
        } else {
            threshold_score(w, config_.thresholds, &d.score);
        }
    }
    d.evidence = logic_.step(d.usable ? &d.score : nullptr);
    d.state = logic_.state();
    d.decision = logic_.decision();
    last_ = d;
    *out = d;
    return true;
}

} // namespace presence
