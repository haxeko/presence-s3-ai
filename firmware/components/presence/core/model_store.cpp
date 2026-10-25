#include "model_store.h"

#include <string.h>

namespace presence {

static const uint8_t kMetaMagic[4] = { 'P', 'S', 'M', 'M' };
static constexpr uint32_t kMetaFormat = 1;
static constexpr size_t kSlotInfoSize = 76;

uint32_t crc32_le(uint32_t crc, const uint8_t *data, size_t len)
{
    crc = ~crc;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int k = 0; k < 8; k++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}

const char *slot_state_name(SlotState s)
{
    switch (s) {
    case SlotState::Empty:
        return "empty";
    case SlotState::Writing:
        return "writing";
    case SlotState::Ready:
        return "ready";
    case SlotState::Bad:
        return "bad";
    }
    return "?";
}

static void wr_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static uint32_t rd_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void ModelStore::encode(const ModelMeta &m, uint8_t out[kMetaRecordSize])
{
    memset(out, 0, kMetaRecordSize);
    memcpy(out, kMetaMagic, 4);
    wr_u32(out + 4, kMetaFormat);
    wr_u32(out + 8, m.generation);
    out[12] = m.active;
    out[13] = m.previous;
    for (int i = 0; i < 2; i++) {
        uint8_t *s = out + 16 + kSlotInfoSize * i;
        const SlotInfo &si = m.slots[i];
        s[0] = (uint8_t)si.state;
        wr_u32(s + 4, si.model_version);
        wr_u32(s + 8, si.pkg_size);
        memcpy(s + 12, si.pkg_sha256, 32);
        memcpy(s + 44, si.model_id, kPkgModelIdLen);
    }
    wr_u32(out + kMetaRecordSize - 4, crc32_le(0, out, kMetaRecordSize - 4));
}

bool ModelStore::decode(const uint8_t in[kMetaRecordSize], ModelMeta *m)
{
    if (memcmp(in, kMetaMagic, 4) != 0 || rd_u32(in + 4) != kMetaFormat) {
        return false;
    }
    if (crc32_le(0, in, kMetaRecordSize - 4) != rd_u32(in + kMetaRecordSize - 4)) {
        return false;
    }
    ModelMeta r;
    memset(&r, 0, sizeof(r));
    r.generation = rd_u32(in + 8);
    r.active = in[12];
    r.previous = in[13];
    for (int i = 0; i < 2; i++) {
        const uint8_t *s = in + 16 + kSlotInfoSize * i;
        SlotInfo &si = r.slots[i];
        if (s[0] > (uint8_t)SlotState::Bad) {
            return false;
        }
        si.state = (SlotState)s[0];
        si.model_version = rd_u32(s + 4);
        si.pkg_size = rd_u32(s + 8);
        memcpy(si.pkg_sha256, s + 12, 32);
        memcpy(si.model_id, s + 44, kPkgModelIdLen);
        si.model_id[kPkgModelIdLen] = '\0';
    }
    if ((r.active != kNoSlot && r.active > 1) || (r.previous != kNoSlot && r.previous > 1)) {
        return false;
    }
    *m = r;
    return true;
}

ModelStore::ModelStore(FlashRegion *meta, FlashRegion *slot0, FlashRegion *slot1)
    : meta_region_(meta), slot0_(slot0), slot1_(slot1), current_copy_(-1), meta_writes_(0)
{
    memset(&meta_, 0, sizeof(meta_));
    meta_.active = kNoSlot;
    meta_.previous = kNoSlot;
}

bool ModelStore::load()
{
    const size_t copy_size = meta_region_->erase_size();
    ModelMeta best;
    memset(&best, 0, sizeof(best));
    int best_copy = -1;
    for (int c = 0; c < 2; c++) {
        uint8_t rec[kMetaRecordSize];
        if (!meta_region_->read(copy_size * c, rec, sizeof(rec))) {
            return false;
        }
        ModelMeta m;
        if (decode(rec, &m) && (best_copy < 0 || (int32_t)(m.generation - best.generation) > 0)) {
            best = m;
            best_copy = c;
        }
    }
    if (best_copy < 0) {
        memset(&meta_, 0, sizeof(meta_));
        meta_.active = kNoSlot;
        meta_.previous = kNoSlot;
        current_copy_ = -1;
        return true;
    }
    meta_ = best;
    current_copy_ = best_copy;
    // An active or previous slot must be READY; anything else is cleared in memory (not written).
    if (meta_.active != kNoSlot && meta_.slots[meta_.active].state != SlotState::Ready) {
        meta_.active = kNoSlot;
    }
    if (meta_.previous != kNoSlot && meta_.slots[meta_.previous].state != SlotState::Ready) {
        meta_.previous = kNoSlot;
    }
    return true;
}

bool ModelStore::commit(const ModelMeta &next_in)
{
    ModelMeta next = next_in;
    next.generation = meta_.generation + 1;
    const int copy = current_copy_ == 0 ? 1 : 0;
    const size_t copy_size = meta_region_->erase_size();
    uint8_t rec[kMetaRecordSize];
    encode(next, rec);
    meta_writes_++;
    if (!meta_region_->erase(copy_size * copy, copy_size) || !meta_region_->write(copy_size * copy, rec, sizeof(rec))) {
        return false;
    }
    // Read back: a write the flash did not keep must not be believed
    uint8_t check[kMetaRecordSize];
    if (!meta_region_->read(copy_size * copy, check, sizeof(check)) || memcmp(check, rec, sizeof(rec)) != 0) {
        return false;
    }
    meta_ = next;
    current_copy_ = copy;
    return true;
}

int ModelStore::inactive_slot() const
{
    if (meta_.active == kNoSlot) {
        if (meta_.slots[0].state != SlotState::Ready) {
            return 0;
        }
        if (meta_.slots[1].state != SlotState::Ready) {
            return 1;
        }
        return meta_.previous == 0 ? 1 : 0;
    }
    return meta_.active == 0 ? 1 : 0;
}

bool ModelStore::begin_write(int slot, uint32_t model_version, const char *model_id, uint32_t pkg_size,
                             const uint8_t sha256[32])
{
    if (slot < 0 || slot > 1 || slot == meta_.active) {
        return false;
    }
    FlashRegion *r = this->slot(slot);
    if (pkg_size == 0 || pkg_size > r->size()) {
        return false;
    }
    ModelMeta next = meta_;
    SlotInfo &si = next.slots[slot];
    si.state = SlotState::Writing;
    si.model_version = model_version;
    si.pkg_size = pkg_size;
    memcpy(si.pkg_sha256, sha256, 32);
    memset(si.model_id, 0, sizeof(si.model_id));
    strncpy(si.model_id, model_id, kPkgModelIdLen);
    if (next.previous == slot) {
        next.previous = kNoSlot; // its old content is being overwritten
    }
    if (!commit(next)) {
        return false;
    }
    const size_t es = r->erase_size();
    const size_t erase_len = (pkg_size + es - 1) / es * es;
    return r->erase(0, erase_len);
}

bool ModelStore::write_chunk(int slot, size_t offset, const void *data, size_t len)
{
    if (slot < 0 || slot > 1 || meta_.slots[slot].state != SlotState::Writing ||
        offset + len > meta_.slots[slot].pkg_size) {
        return false;
    }
    return this->slot(slot)->write(offset, data, len);
}

bool ModelStore::read(int slot, size_t offset, void *dst, size_t len)
{
    if (slot < 0 || slot > 1 || offset + len > meta_.slots[slot].pkg_size) {
        return false;
    }
    return this->slot(slot)->read(offset, dst, len);
}

bool ModelStore::set_state(int slot, SlotState state)
{
    if (slot < 0 || slot > 1) {
        return false;
    }
    ModelMeta next = meta_;
    next.slots[slot].state = state;
    if (state != SlotState::Ready) {
        if (next.active == slot) {
            next.active = kNoSlot;
        }
        if (next.previous == slot) {
            next.previous = kNoSlot;
        }
    }
    return commit(next);
}

bool ModelStore::activate(int slot)
{
    if (slot < 0 || slot > 1 || meta_.slots[slot].state != SlotState::Ready) {
        return false;
    }
    if (meta_.active == slot) {
        return true;
    }
    ModelMeta next = meta_;
    next.previous = meta_.active;
    next.active = (uint8_t)slot;
    return commit(next);
}

bool ModelStore::promote(int slot)
{
    if (slot < 0 || slot > 1 || meta_.slots[slot].state != SlotState::Writing) {
        return false;
    }
    ModelMeta next = meta_;
    next.slots[slot].state = SlotState::Ready;
    if (next.active != slot) {
        next.previous = meta_.active;
        next.active = (uint8_t)slot;
    }
    return commit(next);
}

bool ModelStore::replace_active(int fallback)
{
    ModelMeta next = meta_;
    if (next.active != kNoSlot) {
        next.slots[next.active].state = SlotState::Bad;
    }
    if (fallback != kNoSlot && (fallback < 0 || fallback > 1 || next.slots[fallback].state != SlotState::Ready)) {
        return false;
    }
    next.active = (uint8_t)fallback;
    next.previous = kNoSlot;
    return commit(next);
}

} // namespace presence
