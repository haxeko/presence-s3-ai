// A flash partition seen as NOR flash: erase sets bytes to 0xFF in erase_size() blocks, write only clears bits.
// ESP32: platform/partition_region.cpp (esp_partition_*). Host tests: a RAM model that can cut the power.
#pragma once

#include <stddef.h>

namespace presence {

class FlashRegion {
public:
    virtual ~FlashRegion() {}
    virtual size_t size() const = 0;
    virtual size_t erase_size() const = 0;
    virtual bool read(size_t offset, void *dst, size_t len) = 0;
    virtual bool write(size_t offset, const void *src, size_t len) = 0;
    virtual bool erase(size_t offset, size_t len) = 0; // offset and len multiples of erase_size()
};

} // namespace presence
