#include "partition_region.h"

namespace presence {

const esp_partition_t *PartitionRegion::find(const char *label)
{
    return esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, label);
}

bool PartitionRegion::read(size_t offset, void *dst, size_t len)
{
    return p_ && esp_partition_read(p_, offset, dst, len) == ESP_OK;
}

bool PartitionRegion::write(size_t offset, const void *src, size_t len)
{
    return p_ && esp_partition_write(p_, offset, src, len) == ESP_OK;
}

bool PartitionRegion::erase(size_t offset, size_t len)
{
    return p_ && esp_partition_erase_range(p_, offset, len) == ESP_OK;
}

} // namespace presence
