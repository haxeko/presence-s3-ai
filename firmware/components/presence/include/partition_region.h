// core/flash_region.h over an ESP-IDF data partition.
#pragma once

#include "esp_partition.h"
#include "flash_region.h"

namespace presence {

class PartitionRegion : public FlashRegion {
public:
    explicit PartitionRegion(const esp_partition_t *p) : p_(p) {}
    // Finds a data partition by label; nullptr when the partition table lacks it.
    static const esp_partition_t *find(const char *label);
    bool valid() const { return p_ != nullptr; }
    size_t size() const override { return p_ ? p_->size : 0; }
    size_t erase_size() const override { return p_ ? p_->erase_size : 4096; }
    bool read(size_t offset, void *dst, size_t len) override;
    bool write(size_t offset, const void *src, size_t len) override;
    bool erase(size_t offset, size_t len) override;

private:
    const esp_partition_t *p_;
};

} // namespace presence
