#include "model_manager.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "crypto.h"

namespace presence {

const char *model_error_name(ModelError e)
{
    switch (e) {
    case ModelError::Ok:
        return "ok";
    case ModelError::NoModel:
        return "no_model";
    case ModelError::Flash:
        return "flash";
    case ModelError::Size:
        return "size";
    case ModelError::Memory:
        return "memory";
    case ModelError::Sha256:
        return "sha256";
    case ModelError::Package:
        return "package";
    case ModelError::Contract:
        return "contract";
    case ModelError::EngineLoad:
        return "engine_load";
    case ModelError::SelfTest:
        return "selftest";
    case ModelError::NotReady:
        return "slot_not_ready";
    case ModelError::Download:
        return "download";
    }
    return "?";
}

static void *default_alloc(size_t size)
{
    // 16-byte aligned: the flatbuffer of the TFLITE section starts at a 16-byte offset of the package
    void *p = nullptr;
    if (posix_memalign(&p, 16, size) != 0) {
        return nullptr;
    }
    return p;
}

static void default_release(void *p)
{
    free(p);
}

ModelManager::ModelManager(ModelStore &store, InferenceEngine &engine_a, InferenceEngine &engine_b,
                           const ModelManagerConfig &cfg)
    : store_(store), cfg_(cfg), running_(-1)
{
    engines_[0] = &engine_a;
    engines_[1] = &engine_b;
    buffers_[0] = buffers_[1] = nullptr;
    if (!cfg_.alloc || !cfg_.release) {
        cfg_.alloc = default_alloc;
        cfg_.release = default_release;
    }
    memset(&active_, 0, sizeof(active_));
    memset(&prepared_, 0, sizeof(prepared_));
    active_.slot = -1;
}

ModelManager::~ModelManager()
{
    for (int i = 0; i < 2; i++) {
        engines_[i]->unload();
        if (buffers_[i]) {
            cfg_.release(buffers_[i]);
        }
    }
}

static ModelError set_detail(ModelCheck *check, ModelError e, const char *detail)
{
    check->error = e;
    snprintf(check->detail, sizeof(check->detail), "%s", detail);
    return e;
}

ModelError ModelManager::prepare(int slot, const uint8_t *expected_sha256, ModelCheck *check)
{
    memset(check, 0, sizeof(*check));
    const SlotInfo si = store_.meta().slots[slot];
    if (si.state != SlotState::Ready && si.state != SlotState::Writing) {
        return set_detail(check, ModelError::NotReady, slot_state_name(si.state));
    }
    const size_t size = si.pkg_size;
    if (size == 0 || size > cfg_.max_package_bytes || size > store_.slot(slot)->size()) {
        return set_detail(check, ModelError::Size, "package size");
    }
    if (cfg_.free_heap && cfg_.free_heap() < size + cfg_.free_heap_reserve) {
        return set_detail(check, ModelError::Memory, "package buffer");
    }
    const int spare = running_ == 0 ? 1 : 0;
    engines_[spare]->unload();
    if (buffers_[spare]) {
        cfg_.release(buffers_[spare]);
        buffers_[spare] = nullptr;
    }
    uint8_t *buf = (uint8_t *)cfg_.alloc(size);
    if (!buf) {
        return set_detail(check, ModelError::Memory, "alloc failed");
    }
    buffers_[spare] = buf;
    if (!store_.read(slot, 0, buf, size)) {
        drop_prepared();
        return set_detail(check, ModelError::Flash, "slot read");
    }

    uint8_t digest[32];
    sha256(buf, size, digest);
    if (memcmp(digest, si.pkg_sha256, 32) != 0 || (expected_sha256 && memcmp(digest, expected_sha256, 32) != 0)) {
        drop_prepared();
        return set_detail(check, ModelError::Sha256, "package sha256");
    }
    PkgInfo info;
    PkgError pe = parse_package(buf, size, &info);
    if (pe == PkgError::Ok) {
        pe = check_compatibility(info, kFirmwareModelApi);
    }
    if (pe == PkgError::Ok) {
        pe = check_section_hashes(buf, info);
    }
    if (pe == PkgError::Ok) {
        uint8_t signed_digest[32];
        sha256(buf, info.total_size, signed_digest);
        pe = check_signature(info, signed_digest, cfg_.public_key_pem);
    }
    if (pe != PkgError::Ok) {
        check->pkg_error = pe;
        drop_prepared();
        return set_detail(check, ModelError::Package, pkg_error_name(pe));
    }
    if (info.model_version != si.model_version || strncmp(info.model_id, si.model_id, kPkgModelIdLen) != 0) {
        check->pkg_error = PkgError::Header;
        drop_prepared();
        return set_detail(check, ModelError::Package, "version or id differs from announcement");
    }

    const PkgSection *cs = info.find(kSectionContract);
    ModelContract contract;
    const ContractError ce = parse_contract(buf + cs->offset, cs->length, cfg_.radar_gate_cm, &contract);
    if (ce != ContractError::Ok) {
        check->contract_error = ce;
        drop_prepared();
        return set_detail(check, ModelError::Contract, contract_error_name(ce));
    }
    if (cfg_.free_heap && cfg_.free_heap() < contract.arena_bytes + cfg_.free_heap_reserve) {
        drop_prepared();
        return set_detail(check, ModelError::Memory, "tensor arena");
    }

    const PkgSection *ts = info.find(kSectionTflite);
    char err[48] = "";
    if (!engines_[spare]->load(buf + ts->offset, ts->length, contract.arena_bytes, err, sizeof(err))) {
        drop_prepared();
        return set_detail(check, ModelError::EngineLoad, err);
    }
    const PkgSection *vs = info.find(kSectionTestVectors);
    if (!run_selftest(contract, buf + vs->offset, vs->length, *engines_[spare], &check->selftest, cfg_.now_us)) {
        drop_prepared();
        return set_detail(check, ModelError::SelfTest, check->selftest.error);
    }
    // Keep only what inference needs: a copy of the flatbuffer and an arena sized to what TFLM used (+1 KB),
    // then test again with that final setup before the package buffer is released.
    const size_t used = engines_[spare]->arena_used();
    size_t arena = (used + 1023) / 1024 * 1024 + 1024;
    if (arena > contract.arena_bytes) {
        arena = contract.arena_bytes;
    }
    uint8_t *compact = (uint8_t *)cfg_.alloc(ts->length);
    if (compact) {
        memcpy(compact, buf + ts->offset, ts->length);
        engines_[spare]->unload();
        SelfTestReport final_report;
        if (!engines_[spare]->load(compact, ts->length, arena, err, sizeof(err)) ||
            !run_selftest(contract, buf + vs->offset, vs->length, *engines_[spare], &final_report, cfg_.now_us)) {
            // Fall back to the configuration that passed
            engines_[spare]->unload();
            cfg_.release(compact);
            if (!engines_[spare]->load(buf + ts->offset, ts->length, contract.arena_bytes, err, sizeof(err))) {
                drop_prepared();
                return set_detail(check, ModelError::EngineLoad, err);
            }
        } else {
            cfg_.release(buf);
            buffers_[spare] = compact;
            check->selftest.arena_used = final_report.arena_used;
        }
    }

    memset(&prepared_, 0, sizeof(prepared_));
    prepared_.present = true;
    prepared_.slot = slot;
    prepared_.model_version = info.model_version;
    memcpy(prepared_.model_id, info.model_id, sizeof(prepared_.model_id));
    prepared_.flags = info.flags;
    prepared_.contract = contract;
    prepared_.tflite_len = ts->length;
    prepared_.arena_used = engines_[spare]->arena_used();
    check->error = ModelError::Ok;
    return ModelError::Ok;
}

void ModelManager::drop_prepared()
{
    const int spare = running_ == 0 ? 1 : 0;
    engines_[spare]->unload();
    if (buffers_[spare]) {
        cfg_.release(buffers_[spare]);
        buffers_[spare] = nullptr;
    }
    memset(&prepared_, 0, sizeof(prepared_));
}

void ModelManager::commit_prepared(int slot)
{
    const int spare = running_ == 0 ? 1 : 0;
    const int old = running_;
    if (cfg_.lock) {
        cfg_.lock();
    }
    running_ = spare;
    active_ = prepared_;
    active_.slot = slot;
    if (cfg_.unlock) {
        cfg_.unlock();
    }
    if (old >= 0) {
        engines_[old]->unload();
        if (buffers_[old]) {
            cfg_.release(buffers_[old]);
            buffers_[old] = nullptr;
        }
    }
    memset(&prepared_, 0, sizeof(prepared_));
}

ModelError ModelManager::boot(ModelCheck *check, ModelCheck *failed, bool *rolled_back)
{
    *rolled_back = false;
    memset(check, 0, sizeof(*check));
    if (failed) {
        memset(failed, 0, sizeof(*failed));
    }
    if (!store_.load()) {
        return set_detail(check, ModelError::Flash, "metadata read");
    }
    for (int attempt = 0; attempt < 2; attempt++) {
        const uint8_t slot = store_.meta().active;
        if (slot == kNoSlot) {
            if (check->error == ModelError::Ok) {
                set_detail(check, ModelError::NoModel, "no active model");
            }
            return ModelError::NoModel;
        }
        const ModelError e = prepare(slot, nullptr, check);
        if (e == ModelError::Ok) {
            commit_prepared(slot);
            return ModelError::Ok;
        }
        // The active slot is unusable: fall back to the previous one if it is still there.
        if (failed && attempt == 0) {
            *failed = *check;
        }
        const uint8_t prev = store_.meta().previous;
        const int fallback = (prev != kNoSlot && store_.meta().slots[prev].state == SlotState::Ready) ? prev : kNoSlot;
        if (!store_.replace_active(fallback)) {
            return set_detail(check, ModelError::Flash, "metadata write");
        }
        *rolled_back = true;
        if (fallback == kNoSlot) {
            return e;
        }
    }
    return check->error;
}

ModelError ModelManager::install(int slot, const uint8_t expected_sha256[32], ModelCheck *check)
{
    memset(check, 0, sizeof(*check));
    if (slot < 0 || slot > 1 || store_.meta().slots[slot].state != SlotState::Writing || slot == active_.slot) {
        return set_detail(check, ModelError::NotReady, "slot not downloaded");
    }
    const ModelError e = prepare(slot, expected_sha256, check);
    if (e != ModelError::Ok) {
        store_.set_state(slot, SlotState::Bad);
        return e;
    }
    if (!store_.promote(slot)) {
        drop_prepared();
        return set_detail(check, ModelError::Flash, "metadata write");
    }
    commit_prepared(slot);
    return ModelError::Ok;
}

ModelError ModelManager::rollback(ModelCheck *check)
{
    memset(check, 0, sizeof(*check));
    const uint8_t prev = store_.meta().previous;
    if (prev == kNoSlot || store_.meta().slots[prev].state != SlotState::Ready) {
        return set_detail(check, ModelError::NoModel, "no previous model");
    }
    const ModelError e = prepare(prev, nullptr, check);
    if (e != ModelError::Ok) {
        store_.set_state(prev, SlotState::Bad);
        return e;
    }
    if (!store_.activate(prev)) {
        drop_prepared();
        return set_detail(check, ModelError::Flash, "metadata write");
    }
    commit_prepared(prev);
    return ModelError::Ok;
}

bool ModelManager::infer(const int8_t in[kFeatures], int8_t out[kClassCount])
{
    if (cfg_.lock) {
        cfg_.lock();
    }
    const bool ok = running_ >= 0 && engines_[running_]->invoke(in, out);
    if (cfg_.unlock) {
        cfg_.unlock();
    }
    return ok;
}

} // namespace presence
