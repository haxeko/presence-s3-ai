#include "sampler.h"

#include <string.h>

namespace presence {

Sampler::Sampler() : next_seq_(0), reports_used_(0), reports_rejected_(0), empty_ticks_(0)
{
    clear_bin();
}

void Sampler::clear_bin()
{
    count_ = 0;
    memset(energy_, 0, sizeof(energy_));
    memset(&last_, 0, sizeof(last_));
}

bool Sampler::add_report(const ld2410::Report &r)
{
    if (!r.full_layout()) {
        reports_rejected_++;
        return false;
    }
    for (int g = 0; g < kGates; g++) {
        const uint8_t m = r.moving[g] > kEnergyMax ? kEnergyMax : r.moving[g];
        const uint8_t s = r.still[g] > kEnergyMax ? kEnergyMax : r.still[g];
        if (m > energy_[g]) {
            energy_[g] = m;
        }
        if (s > energy_[kGates + g]) {
            energy_[kGates + g] = s;
        }
    }
    if (count_ < UINT8_MAX) {
        count_++;
    }
    last_ = r;
    reports_used_++;
    return true;
}

bool Sampler::close_tick(uint32_t tick, Sample *out)
{
    if (count_ == 0) {
        empty_ticks_++;
        clear_bin();
        return false;
    }
    Sample s;
    memset(&s, 0, sizeof(s));
    s.seq = next_seq_++;
    s.tick = tick;
    s.n_frames = count_;
    s.target_state = last_.target_state;
    s.moving_cm = last_.moving_cm;
    s.still_cm = last_.still_cm;
    s.detect_cm = last_.detect_cm;
    s.moving_energy = last_.moving_energy;
    s.still_energy = last_.still_energy;
    memcpy(s.energy, energy_, sizeof(energy_));
    s.light = (int16_t)last_.light();
    s.out_pin = (int16_t)last_.out_pin();
    s.info.pred = -1;
    s.info.occ_permille = -1;
    *out = s;
    clear_bin();
    return true;
}

} // namespace presence
