// Model packages (Python-built fixtures), A/B store with power cuts, install / rollback / boot, pipeline.
#include <functional>
#include <memory>

#include "crypto.h"
#include "fake_engine.h"
#include "model_manager.h"
#include "model_package.h"
#include "model_store.h"
#include "pipeline.h"
#include "ram_flash.h"
#include "test_util.h"

using namespace presence;

namespace {

std::vector<uint8_t> pkg(const char *name)
{
    return read_file(fixture_path(std::string("packages/") + name));
}

std::string pub_key()
{
    return read_text(fixture_path("packages/test_signing_pub.pem"));
}

void test_packages()
{
    test_begin("package: Python package parsed, hashes and signature checked");
    const std::string key = pub_key();
    std::vector<uint8_t> p = pkg("pkg_v1.bin");
    PkgInfo info;
    CHECK(parse_package(p.data(), p.size(), &info) == PkgError::Ok);
    CHECK_EQ(info.model_version, 1u);
    CHECK(strcmp(info.model_id, "fake-v1") == 0);
    CHECK(info.find(kSectionContract) && info.find(kSectionTflite) && info.find(kSectionTestVectors) &&
          info.find(kSectionManifest));
    CHECK(check_section_hashes(p.data(), info) == PkgError::Ok);
    uint8_t digest[32];
    sha256(p.data(), info.total_size, digest);
    CHECK(check_signature(info, digest, key.c_str()) == PkgError::Ok);
    CHECK(check_compatibility(info, kFirmwareModelApi) == PkgError::Ok);
    ModelContract c;
    const PkgSection *cs = info.find(kSectionContract);
    CHECK(parse_contract(p.data() + cs->offset, cs->length, 75, &c) == ContractError::Ok);
    CHECK(c.input_scale > 0.049f && c.input_scale < 0.051f && c.input_zero_point == -3);
    CHECK(c.calib_count == 3 && c.calib_y[2] > 0.96f);
    float prob = 0;
    CHECK(calibrate(c, 0.75f, &prob) && prob > 0.68f && prob < 0.69f);
    CHECK(strcmp(c.class_names[2], "occupied_still") == 0);

    test_begin("package: tampering, other key, unsigned, firmware range, resolution");
    std::vector<uint8_t> t = p;
    t[info.find(kSectionTflite)->offset + 30] ^= 0x01;
    PkgInfo ti;
    CHECK(parse_package(t.data(), t.size(), &ti) == PkgError::Ok);
    CHECK(check_section_hashes(t.data(), ti) == PkgError::SectionHash);
    t = p;
    t[20] ^= 0x01; // model_version: header is signed
    CHECK(parse_package(t.data(), t.size(), &ti) == PkgError::Ok);
    sha256(t.data(), ti.total_size, digest);
    CHECK(check_signature(ti, digest, key.c_str()) == PkgError::Signature);

    std::vector<uint8_t> other = pkg("pkg_v3_other_key.bin");
    CHECK(parse_package(other.data(), other.size(), &ti) == PkgError::Ok);
    sha256(other.data(), ti.total_size, digest);
    CHECK(check_signature(ti, digest, key.c_str()) == PkgError::Signature);
    std::vector<uint8_t> unsigned_pkg = pkg("pkg_v4_unsigned.bin");
    CHECK(parse_package(unsigned_pkg.data(), unsigned_pkg.size(), &ti) == PkgError::Ok);
    CHECK(ti.sig_alg == kSigAlgNone);
    sha256(unsigned_pkg.data(), ti.total_size, digest);
    CHECK(check_signature(ti, digest, key.c_str()) == PkgError::Signature);
    CHECK(check_signature(ti, digest, nullptr) == PkgError::Ok);
    std::vector<uint8_t> api = pkg("pkg_v6_fw_api_2.bin");
    CHECK(parse_package(api.data(), api.size(), &ti) == PkgError::Ok);
    CHECK(check_compatibility(ti, kFirmwareModelApi) == PkgError::Incompatible);
    std::vector<uint8_t> res = pkg("pkg_v7_res20.bin");
    CHECK(parse_package(res.data(), res.size(), &ti) == PkgError::Ok);
    cs = ti.find(kSectionContract);
    CHECK(parse_contract(res.data() + cs->offset, cs->length, 75, &c) == ContractError::Resolution);
    CHECK(parse_contract(res.data() + cs->offset, cs->length, 20, &c) == ContractError::Ok);

    test_begin("package: malformed headers rejected");
    t = p;
    t[0] = 'X';
    CHECK(parse_package(t.data(), t.size(), &ti) == PkgError::Magic);
    CHECK(parse_package(p.data(), 40, &ti) == PkgError::TooSmall);
    CHECK(parse_package(p.data(), info.total_size + 4, &ti) == PkgError::Signature); // trailer cut
    t = p;
    t[12] = 9; // 9 sections
    CHECK(parse_package(t.data(), t.size(), &ti) == PkgError::Header);
    t = p;
    t[64 + 4] = 0x08; // first section offset inside the table
    t[64 + 5] = 0x00;
    CHECK(parse_package(t.data(), t.size(), &ti) == PkgError::Sections);
}

// One device: metadata partition (2 x 4 KB), two 64 KB slots, the manager with two fake engines.
struct Device {
    PowerCut power;
    RamFlash meta{ 8192, &power };
    RamFlash slot0{ 65536, &power };
    RamFlash slot1{ 65536, &power };
    ModelStore store{ &meta, &slot0, &slot1 };
    FakeEngine ea, eb;
    std::string key = pub_key();
    ModelManagerConfig cfg{};
    std::unique_ptr<ModelManager> mgr;

    explicit Device(bool signed_only = true)
    {
        cfg.public_key_pem = signed_only ? key.c_str() : nullptr;
        cfg.radar_gate_cm = 75;
        cfg.max_package_bytes = 65536;
        mgr.reset(new ModelManager(store, ea, eb, cfg));
    }
    // Power comes back: new RAM state, same flash
    void reboot()
    {
        power.cut = false;
        power.budget = -1;
        mgr.reset();
        store = ModelStore(&meta, &slot0, &slot1);
        ea = FakeEngine();
        eb = FakeEngine();
        mgr.reset(new ModelManager(store, ea, eb, cfg));
    }
    // HTTP download emulation: begin_write + chunks of 512 bytes. False when the power was cut.
    bool download(const std::vector<uint8_t> &p, int *slot_out, uint8_t sha[32])
    {
        PkgInfo info;
        if (parse_package(p.data(), p.size(), &info) != PkgError::Ok) {
            return false;
        }
        sha256(p.data(), p.size(), sha);
        const int slot = store.inactive_slot();
        *slot_out = slot;
        if (!store.begin_write(slot, info.model_version, info.model_id, (uint32_t)p.size(), sha)) {
            return false;
        }
        for (size_t off = 0; off < p.size(); off += 512) {
            const size_t n = std::min((size_t)512, p.size() - off);
            if (!store.write_chunk(slot, off, p.data() + off, n)) {
                return false;
            }
        }
        return true;
    }
    ModelError update(const std::vector<uint8_t> &p, ModelCheck *check)
    {
        int slot = -1;
        uint8_t sha[32];
        if (!download(p, &slot, sha)) {
            return ModelError::Flash;
        }
        return mgr->install(slot, sha, check);
    }
    uint32_t active_version() const { return mgr->active().present ? mgr->active().model_version : 0; }
};

void test_install_and_rollback()
{
    test_begin("update: install v1 then v2, inference switches, v1 kept for rollback");
    Device d;
    ModelCheck check;
    bool rolled = false;
    CHECK(d.mgr->boot(&check, nullptr, &rolled) == ModelError::NoModel);
    CHECK(d.update(pkg("pkg_v1.bin"), &check) == ModelError::Ok);
    CHECK_EQ(d.active_version(), 1u);
    CHECK(check.selftest.passed && check.selftest.vectors == 6 && check.selftest.max_output_diff == 0);
    CHECK_EQ(check.selftest.max_feature_diff, 0.0f);
    int8_t in[kFeatures] = {}, out[kClassCount];
    CHECK(d.mgr->infer(in, out));
    CHECK(d.update(pkg("pkg_v2.bin"), &check) == ModelError::Ok);
    CHECK_EQ(d.active_version(), 2u);
    CHECK(d.store.meta().active != kNoSlot && d.store.meta().previous != kNoSlot);
    CHECK(d.store.meta().slots[d.store.meta().previous].model_version == 1);
    CHECK(d.mgr->infer(in, out));

    test_begin("update: rollback to v1 and back");
    CHECK(d.mgr->rollback(&check) == ModelError::Ok);
    CHECK_EQ(d.active_version(), 1u);
    CHECK(d.mgr->rollback(&check) == ModelError::Ok);
    CHECK_EQ(d.active_version(), 2u);

    test_begin("update: rejected packages leave the running model untouched");
    struct Case {
        const char *name;
        ModelError error;
        const char *detail;
    } cases[] = {
        { "pkg_v3_other_key.bin", ModelError::Package, "signature" },
        { "pkg_v4_unsigned.bin", ModelError::Package, "signature" },
        { "pkg_v5_bad_vectors.bin", ModelError::SelfTest, "testvec_mismatch" },
        { "pkg_v6_fw_api_2.bin", ModelError::Package, "firmware_incompatible" },
        { "pkg_v7_res20.bin", ModelError::Contract, "radar_resolution" },
        { "pkg_v8_quant_mismatch.bin", ModelError::SelfTest, "quantization_mismatch" },
    };
    for (const Case &c : cases) {
        const uint32_t before = d.active_version();
        const ModelError e = d.update(pkg(c.name), &check);
        CHECK(e == c.error);
        CHECK(strcmp(check.detail, c.detail) == 0);
        if (e != c.error || strcmp(check.detail, c.detail) != 0) {
            fprintf(stderr, "    %s: %s / %s\n", c.name, model_error_name(e), check.detail);
        }
        CHECK_EQ(d.active_version(), before);
        CHECK(d.mgr->infer(in, out));
        const uint8_t bad = d.store.inactive_slot();
        CHECK(d.store.meta().slots[bad].state == SlotState::Bad);
    }
    // After rejected downloads the previous model slot was reused: no rollback target any more
    CHECK(d.mgr->rollback(&check) == ModelError::NoModel);

    test_begin("update: announced SHA-256 must match the downloaded package");
    std::vector<uint8_t> v9 = pkg("pkg_v9_simulated.bin");
    int slot = -1;
    uint8_t sha[32];
    CHECK(d.download(v9, &slot, sha));
    sha[0] ^= 1;
    CHECK(d.mgr->install(slot, sha, &check) == ModelError::Sha256);
    CHECK_EQ(d.active_version(), 2u);

    test_begin("update: simulated-data flag reported, unsigned accepted without a key");
    Device u(false);
    CHECK(u.update(pkg("pkg_v4_unsigned.bin"), &check) == ModelError::Ok);
    CHECK(u.update(v9, &check) == ModelError::Ok);
    CHECK(u.mgr->active().flags & kPkgFlagSimulated);

    test_begin("update: boot restores the active model, corrupted slot rolls back");
    d.reboot();
    CHECK(d.mgr->boot(&check, nullptr, &rolled) == ModelError::Ok);
    CHECK(!rolled && d.active_version() == 2u);
    CHECK(d.update(pkg("pkg_v1.bin"), &check) == ModelError::Ok); // v1 active, v2 previous
    const int active = d.store.meta().active;
    RamFlash &aflash = active == 0 ? d.slot0 : d.slot1;
    aflash.mem()[200] ^= 0x40; // flash corruption in the active slot
    d.reboot();
    ModelCheck failed;
    CHECK(d.mgr->boot(&check, &failed, &rolled) == ModelError::Ok);
    CHECK(rolled && d.active_version() == 2u);
    CHECK(failed.error == ModelError::Sha256);
    CHECK(d.store.meta().slots[active].state == SlotState::Bad);
    // Both slots unusable: threshold method (no model), never a crash
    RamFlash &bflash = d.store.meta().active == 0 ? d.slot0 : d.slot1;
    bflash.mem()[300] ^= 0x01;
    d.reboot();
    CHECK(d.mgr->boot(&check, nullptr, &rolled) == ModelError::Sha256);
    CHECK(rolled && !d.mgr->active().present);
    CHECK(!d.mgr->infer(in, out));
}

void test_power_cuts()
{
    test_begin("update: power cut after every possible number of written bytes");
    const std::vector<uint8_t> v1 = pkg("pkg_v1.bin");
    const std::vector<uint8_t> v2 = pkg("pkg_v2.bin");
    // Count the bytes one complete update writes
    long total = 0;
    {
        Device d;
        ModelCheck check;
        CHECK(d.update(v1, &check) == ModelError::Ok);
        const long before = d.power.written;
        CHECK(d.update(v2, &check) == ModelError::Ok);
        total = d.power.written - before;
    }
    CHECK(total > (long)v2.size());
    int old_model = 0, new_model = 0, step = 0;
    for (long cut = 0; cut <= total; cut += (cut < 400 || cut > total - 400) ? 1 : 97) {
        Device d;
        ModelCheck check;
        bool rolled = false;
        CHECK(d.update(v1, &check) == ModelError::Ok);
        d.power.budget = cut;
        d.update(v2, &check);
        d.reboot();
        const ModelError e = d.mgr->boot(&check, nullptr, &rolled);
        // Always a verified model: the old one, or the new one when the switch was committed
        CHECK(e == ModelError::Ok);
        CHECK(!rolled);
        const uint32_t v = d.active_version();
        CHECK(v == 1 || v == 2);
        old_model += v == 1;
        new_model += v == 2;
        // And the device can still update afterwards
        if (v == 1) {
            CHECK(d.update(v2, &check) == ModelError::Ok && d.active_version() == 2);
        }
        step++;
    }
    CHECK(old_model > 0 && new_model > 0);
    printf("    %d power cut points: %d kept v1, %d had switched to v2\n", step, old_model, new_model);

    test_begin("store: torn metadata write keeps the previous record");
    PowerCut pc;
    RamFlash meta(8192, &pc), s0(65536, &pc), s1(65536, &pc);
    ModelStore st(&meta, &s0, &s1);
    CHECK(st.load());
    uint8_t sha[32] = { 1, 2, 3 };
    CHECK(st.begin_write(0, 5, "m5", 100, sha));
    CHECK(st.promote(0));
    const uint32_t gen = st.meta().generation;
    pc.budget = 50; // dies in the middle of the next record
    CHECK(!st.begin_write(1, 6, "m6", 100, sha));
    pc.cut = false;
    pc.budget = -1;
    ModelStore st2(&meta, &s0, &s1);
    CHECK(st2.load());
    CHECK_EQ(st2.meta().generation, gen);
    CHECK(st2.meta().active == 0 && st2.meta().slots[0].model_version == 5);
    CHECK(st2.meta().slots[1].state == SlotState::Empty);
    CHECK_EQ(crc32_le(0, (const uint8_t *)"123456789", 9), 0xCBF43926u);
}

// Pipeline with a scripted model
class ScriptedModel : public ModelRunner {
public:
    bool available = true;
    ModelContract c;
    int8_t out[kClassCount] = { -128, -128, -128, 127 };
    ScriptedModel()
    {
        memset(&c, 0, sizeof(c));
        c.hop_slots = 5;
        c.min_valid_slots = 16;
        c.max_gap_slots = 3;
        c.input_scale = 0.1f;
        c.output_scale = 1.0f / 256;
        c.output_zero_point = -128;
        c.occupied_mask = 0x0C;
        for (int i = 0; i < kFeatures; i++) {
            c.feature_std[i] = 1.0f;
        }
    }
    bool model_available() const override { return available; }
    const ModelContract &model_contract() const override { return c; }
    uint32_t model_version() const override { return 7; }
    bool model_infer(const int8_t *, int8_t *o) override
    {
        memcpy(o, out, sizeof(out));
        return true;
    }
};

void test_pipeline()
{
    test_begin("pipeline: decisions every 5 ticks, missing data -> uncertain, method switch");
    ScriptedModel model;
    PipelineConfig cfg{ PresenceParams{ 0.7f, 0.3f, 2000, 3000 }, PresenceParams{ 0.5f, 0.2f, 2000, 5000 },
                        factory_thresholds(), false, false };
    DecisionPipeline p(&model, cfg);
    Sample s;
    memset(&s, 0, sizeof(s));
    Decision d;
    int decisions = 0;
    for (uint32_t t = 0; t < 40; t++) {
        if (p.on_tick(t, &s, &d)) {
            decisions++;
            CHECK_EQ(t % 5, 4u);
        }
    }
    CHECK_EQ(decisions, 8);
    CHECK(d.method == kMethodModel && d.usable && d.pred == 3 && d.model_version == 7);
    CHECK(d.score > 0.99f && d.decision == kDecisionOccupied);
    // Radar silent: windows unusable, state uncertain, decision held
    for (uint32_t t = 40; t < 60; t++) {
        if (p.on_tick(t, nullptr, &d)) {
            CHECK(!d.usable && d.state == kStateUncertain && d.decision == kDecisionOccupied);
        }
    }
    // Model gone: factory threshold method on an all-zero room -> empty after confirm time
    model.available = false;
    for (uint32_t t = 60; t < 200; t++) {
        p.on_tick(t, &s, &d);
    }
    CHECK(d.method == kMethodThresholdFactory && d.usable && d.score == 0.0f);
    CHECK(d.decision == kDecisionEmpty && d.state == kStateEmpty);
    cfg.baseline_thresholds = true;
    CHECK(p.set_config(cfg));
    p.on_tick(204, &s, &d);
    CHECK(d.method == kMethodThresholdBaseline);
    cfg.threshold_params.enter = 2.0f;
    CHECK(!p.set_config(cfg));
}

} // namespace

void run_model_tests()
{
    test_packages();
    test_install_and_rollback();
    test_power_cuts();
    test_pipeline();
}
