// Two model slots (A/B) and their metadata, safe against power loss.
//
// The metadata partition holds two 4 KB copies of a 172-byte record (generation counter + CRC-32). A change is
// written to the copy NOT holding the current record, with generation + 1; the current copy is untouched until
// the new one is complete. After a power cut during a write the new copy fails its CRC and the previous record
// (previous state) is used. The highest valid generation wins.
//
// Slot states: EMPTY, WRITING (download in progress or interrupted: never used), READY (complete and checked),
// BAD (failed a check). Only a READY slot can become active; the previous active slot is kept for rollback.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "flash_region.h"
#include "model_package.h"

namespace presence {

enum class SlotState : uint8_t { Empty = 0, Writing = 1, Ready = 2, Bad = 3 };

constexpr uint8_t kNoSlot = 0xFF;
constexpr size_t kMetaRecordSize = 172;

struct SlotInfo {
    SlotState state;
    uint32_t model_version;
    uint32_t pkg_size;
    uint8_t pkg_sha256[32];
    char model_id[kPkgModelIdLen + 1];
};

struct ModelMeta {
    uint32_t generation;
    uint8_t active;   // 0, 1 or kNoSlot
    uint8_t previous; // 0, 1 or kNoSlot
    SlotInfo slots[2];
};

const char *slot_state_name(SlotState s);

class ModelStore {
public:
    ModelStore(FlashRegion *meta, FlashRegion *slot0, FlashRegion *slot1);
    // Reads the newest valid record (fresh empty metadata when none is valid). False on a flash error.
    bool load();
    const ModelMeta &meta() const { return meta_; }
    FlashRegion *slot(int i) { return i == 0 ? slot0_ : slot1_; }
    // Slot to download into: never the active one; prefers an empty or bad one, else the oldest.
    int inactive_slot() const;

    // Download: mark WRITING (persisted before the slot is erased), erase, then write chunks in order.
    bool begin_write(int slot, uint32_t model_version, const char *model_id, uint32_t pkg_size,
                     const uint8_t sha256[32]);
    bool write_chunk(int slot, size_t offset, const void *data, size_t len);
    // Reads the package of a slot back (len <= pkg_size of the slot).
    bool read(int slot, size_t offset, void *dst, size_t len);
    bool set_state(int slot, SlotState state);
    // Makes a READY slot active; the active one becomes previous. One metadata write.
    bool activate(int slot);
    // A downloaded slot (WRITING) passed every check: READY and active in one metadata write.
    bool promote(int slot);
    // A failed active slot: marks it BAD and makes `fallback` (READY, or kNoSlot) active. One metadata write.
    bool replace_active(int fallback);

    uint32_t meta_writes() const { return meta_writes_; }

private:
    bool commit(const ModelMeta &next);
    static void encode(const ModelMeta &m, uint8_t out[kMetaRecordSize]);
    static bool decode(const uint8_t in[kMetaRecordSize], ModelMeta *m);

    FlashRegion *meta_region_;
    FlashRegion *slot0_;
    FlashRegion *slot1_;
    ModelMeta meta_;
    int current_copy_; // copy holding meta_, -1 when none was valid
    uint32_t meta_writes_;
};

uint32_t crc32_le(uint32_t crc, const uint8_t *data, size_t len);

} // namespace presence
