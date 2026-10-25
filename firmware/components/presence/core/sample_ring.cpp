#include "sample_ring.h"

namespace presence {

SampleRing::SampleRing(Sample *storage, size_t capacity)
    : buf_(storage), capacity_(capacity), head_(0), count_(0), dropped_(0), high_water_(0)
{
}

void SampleRing::push(const Sample &s)
{
    if (capacity_ == 0) {
        dropped_++;
        return;
    }
    if (count_ == capacity_) {
        // Full: overwrite the oldest
        buf_[head_] = s;
        head_ = (head_ + 1) % capacity_;
        dropped_++;
        return;
    }
    buf_[(head_ + count_) % capacity_] = s;
    count_++;
    if (count_ > high_water_) {
        high_water_ = count_;
    }
}

size_t SampleRing::peek(Sample *out, size_t max) const
{
    const size_t n = max < count_ ? max : count_;
    for (size_t i = 0; i < n; i++) {
        out[i] = buf_[(head_ + i) % capacity_];
    }
    return n;
}

size_t SampleRing::peek_from(uint32_t first_seq, Sample *out, size_t max) const
{
    size_t i = 0;
    while (i < count_ && (int32_t)(buf_[(head_ + i) % capacity_].seq - first_seq) < 0) {
        i++;
    }
    size_t n = 0;
    for (; i < count_ && n < max; i++) {
        out[n++] = buf_[(head_ + i) % capacity_];
    }
    return n;
}

void SampleRing::consume_through(uint32_t seq)
{
    // seq increases by one per sample; compare as a signed difference so a wrap of the 32-bit counter is harmless.
    while (count_ > 0 && (int32_t)(buf_[head_].seq - seq) <= 0) {
        head_ = (head_ + 1) % capacity_;
        count_--;
    }
}

} // namespace presence
