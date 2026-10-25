// RAM model of a NOR flash partition for the host tests: erase -> 0xFF, write clears bits only, and a power cut
// after a given number of written bytes (the byte being written may be torn: only some of its bits cleared).
#pragma once

#include <string.h>

#include <vector>

#include "flash_region.h"

struct PowerCut {
    long budget = -1; // bytes that can still be written; -1 = unlimited
    bool cut = false;
    long erases = 0;
    long written = 0;
};

class RamFlash : public presence::FlashRegion {
public:
    RamFlash(size_t size, PowerCut *power, size_t erase_size = 4096)
        : mem_(size, 0xFF), power_(power), erase_size_(erase_size)
    {
    }
    size_t size() const override { return mem_.size(); }
    size_t erase_size() const override { return erase_size_; }
    bool read(size_t offset, void *dst, size_t len) override
    {
        if (power_->cut || offset + len > mem_.size()) {
            return false;
        }
        memcpy(dst, mem_.data() + offset, len);
        return true;
    }
    bool write(size_t offset, const void *src, size_t len) override
    {
        if (power_->cut || offset + len > mem_.size()) {
            return false;
        }
        const uint8_t *s = (const uint8_t *)src;
        for (size_t i = 0; i < len; i++) {
            if (power_->budget == 0) {
                // Torn byte: half of its bits made it
                mem_[offset + i] &= (uint8_t)(s[i] | 0x0F);
                power_->cut = true;
                return false;
            }
            if (power_->budget > 0) {
                power_->budget--;
            }
            mem_[offset + i] &= s[i];
            power_->written++;
        }
        return true;
    }
    bool erase(size_t offset, size_t len) override
    {
        if (power_->cut || offset % erase_size_ || len % erase_size_ || offset + len > mem_.size()) {
            return false;
        }
        for (size_t b = offset; b < offset + len; b += erase_size_) {
            if (power_->budget == 0) {
                // Interrupted erase: part of the block erased
                memset(mem_.data() + b, 0xFF, erase_size_ / 3);
                power_->cut = true;
                return false;
            }
            memset(mem_.data() + b, 0xFF, erase_size_);
            power_->erases++;
        }
        return true;
    }
    std::vector<uint8_t> &mem() { return mem_; }

private:
    std::vector<uint8_t> mem_;
    PowerCut *power_;
    size_t erase_size_;
};
