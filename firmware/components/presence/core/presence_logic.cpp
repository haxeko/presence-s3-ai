#include "presence_logic.h"

namespace presence {

bool PresenceParams::valid() const
{
    return exit >= 0.0f && exit < enter && enter <= 1.0f && confirm_occupied_ms <= 600000 &&
           confirm_empty_ms >= 1000 && confirm_empty_ms <= 3600000;
}

PresenceParams model_presence_defaults()
{
    return PresenceParams{ 0.7f, 0.3f, 2000, 60000 };
}

PresenceParams threshold_presence_defaults()
{
    return PresenceParams{ 0.5f, 0.2f, 2000, 60000 };
}

PresenceLogic::PresenceLogic(const PresenceParams &params, uint32_t hop_ms) : params_(params), hop_ms_(hop_ms)
{
    reset();
}

void PresenceLogic::reset()
{
    decision_ = kDecisionUnknown;
    state_ = kStateUncertain;
    high_ms_ = 0;
    low_ms_ = 0;
}

bool PresenceLogic::set_params(const PresenceParams &params)
{
    if (!params.valid()) {
        return false;
    }
    params_ = params;
    high_ms_ = 0;
    low_ms_ = 0;
    return true;
}

Evidence PresenceLogic::step(const float *score)
{
    if (score == nullptr) {
        high_ms_ = 0;
        low_ms_ = 0;
        state_ = kStateUncertain;
        return Evidence::None;
    }
    Evidence level;
    if (*score >= params_.enter) {
        level = Evidence::High;
        high_ms_ += hop_ms_;
        low_ms_ = 0;
    } else if (*score <= params_.exit) {
        level = Evidence::Low;
        low_ms_ += hop_ms_;
        high_ms_ = 0;
    } else {
        level = Evidence::Mid;
        high_ms_ = 0;
        low_ms_ = 0;
    }
    if (decision_ != kDecisionOccupied && level == Evidence::High && high_ms_ >= params_.confirm_occupied_ms) {
        decision_ = kDecisionOccupied;
    } else if (decision_ != kDecisionEmpty && level == Evidence::Low && low_ms_ >= params_.confirm_empty_ms) {
        decision_ = kDecisionEmpty;
    }
    if (decision_ == kDecisionUnknown) {
        state_ = kStateUncertain;
    } else if (decision_ == kDecisionOccupied) {
        state_ = low_ms_ > 0 ? kStateUncertain : kStateOccupied;
    } else {
        state_ = high_ms_ > 0 ? kStateUncertain : kStateEmpty;
    }
    return level;
}

} // namespace presence
