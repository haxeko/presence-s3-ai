#include "window_features.h"

#include <math.h>
#include <string.h>

namespace presence {

int Window::n_valid() const
{
    int n = 0;
    for (int i = 0; i < kWindowSlots; i++) {
        n += valid[i] ? 1 : 0;
    }
    return n;
}

int Window::longest_gap() const
{
    int run = 0;
    int best = 0;
    for (int i = 0; i < kWindowSlots; i++) {
        run = valid[i] ? 0 : run + 1;
        if (run > best) {
            best = run;
        }
    }
    return best;
}

bool Window::usable(int min_valid, int max_gap) const
{
    return n_valid() >= min_valid && longest_gap() <= max_gap;
}

SlotWindow::SlotWindow()
{
    reset();
}

void SlotWindow::reset()
{
    started_ = false;
    last_tick_ = 0;
    head_ = 0;
    memset(valid_, 0, sizeof(valid_));
    memset(energy_, 0, sizeof(energy_));
}

void SlotWindow::push(uint32_t tick, const uint8_t *energy)
{
    if (started_) {
        uint32_t step = tick - last_tick_;
        if (step == 0 || step > 0x80000000u) {
            return; // same or older tick: ignore
        }
        // Ticks skipped in between are missing data
        if (step > (uint32_t)kWindowSlots) {
            step = kWindowSlots;
        }
        for (uint32_t i = 1; i < step; i++) {
            head_ = (head_ + 1) % kWindowSlots;
            valid_[head_] = false;
        }
        head_ = (head_ + 1) % kWindowSlots;
    } else {
        // Before the first tick every slot is missing
        memset(valid_, 0, sizeof(valid_));
        head_ = 0;
        started_ = true;
    }
    last_tick_ = tick;
    valid_[head_] = energy != nullptr;
    if (energy) {
        memcpy(energy_[head_], energy, kChannels);
    } else {
        memset(energy_[head_], 0, kChannels);
    }
}

void SlotWindow::snapshot(Window *out) const
{
    for (int i = 0; i < kWindowSlots; i++) {
        // i = 0 oldest ... kWindowSlots - 1 = last_tick_
        const int idx = (head_ + 1 + i) % kWindowSlots;
        out->valid[i] = started_ && valid_[idx];
        if (out->valid[i]) {
            memcpy(out->energy[i], energy_[idx], kChannels);
        } else {
            memset(out->energy[i], 0, kChannels);
        }
    }
}

bool compute_features(const Window &w, float out[kFeatures])
{
    const int n = w.n_valid();
    if (n == 0) {
        return false;
    }
    int pairs = 0;
    for (int i = 1; i < kWindowSlots; i++) {
        pairs += (w.valid[i] && w.valid[i - 1]) ? 1 : 0;
    }
    for (int c = 0; c < kChannels; c++) {
        int32_t s1 = 0;
        int32_t s2 = 0;
        int32_t mx = 0;
        int32_t d = 0;
        for (int i = 0; i < kWindowSlots; i++) {
            if (!w.valid[i]) {
                continue;
            }
            const int32_t x = w.energy[i][c];
            s1 += x;
            s2 += x * x;
            if (x > mx) {
                mx = x;
            }
            if (i > 0 && w.valid[i - 1]) {
                const int32_t diff = x - (int32_t)w.energy[i - 1][c];
                d += diff < 0 ? -diff : diff;
            }
        }
        // n * S2 - S1^2 <= 20 * 200000 = 4e6 < 2^24: exact in float32
        const int32_t var_num = n * s2 - s1 * s1;
        out[0 * kChannels + c] = (float)s1 / (float)n;
        out[1 * kChannels + c] = sqrtf((float)var_num / (float)(n * n));
        out[2 * kChannels + c] = (float)mx;
        out[3 * kChannels + c] = pairs > 0 ? (float)d / (float)pairs : 0.0f;
    }
    return true;
}

} // namespace presence
