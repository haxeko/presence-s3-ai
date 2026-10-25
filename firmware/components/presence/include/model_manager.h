// Model lifecycle on the device: checks a package stored in a slot, loads it into a spare engine, runs the
// self-test, and only then makes it active (the running model keeps working meanwhile). Rolls back to the
// previous slot when the active one fails at boot. No ESP-IDF dependency: host-tested with a RAM flash and a
// fake engine; the platform code adds the HTTP download and the locking.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "inference_engine.h"
#include "model_contract.h"
#include "model_package.h"
#include "model_selftest.h"
#include "model_store.h"
#include "pipeline.h"

namespace presence {

enum class ModelError {
    Ok = 0,
    NoModel,
    Flash,
    Size,          // package larger than allowed or than the slot
    Memory,        // not enough RAM for package + arena
    Sha256,        // package differs from the announced SHA-256
    Package,       // see pkg_error
    Contract,      // see contract_error
    EngineLoad,    // TFLM could not prepare the model (ops, arena, schema)
    SelfTest,      // see selftest
    NotReady,      // slot not READY
    Download,      // network, TLS or HTTP error while downloading (nothing activated)
};

const char *model_error_name(ModelError e);

struct ModelCheck {
    ModelError error;
    PkgError pkg_error;
    ContractError contract_error;
    SelfTestReport selftest;
    char detail[64];
};

struct ActiveModel {
    bool present;
    int slot;
    uint32_t model_version;
    char model_id[kPkgModelIdLen + 1];
    uint32_t flags;
    ModelContract contract;
    size_t tflite_len;
    size_t arena_used;
};

struct ModelManagerConfig {
    const char *public_key_pem; // nullptr: unsigned packages accepted (integrity by SHA-256 only)
    uint32_t radar_gate_cm;     // 0: unknown
    size_t max_package_bytes;
    size_t free_heap_reserve;           // RAM that must stay free after loading a model
    size_t (*free_heap)();              // nullptr: memory not checked
    void *(*alloc)(size_t size);        // 16-byte aligned buffers; nullptr: malloc
    void (*release)(void *p);
    MicrosFn now_us;
    // Optional lock around the switch of the running model and around infer() (inference task vs. update task)
    void (*lock)();
    void (*unlock)();
};

class ModelManager : public ModelRunner {
public:
    // Two engines: one runs the active model while the other loads and tests a candidate.
    ModelManager(ModelStore &store, InferenceEngine &engine_a, InferenceEngine &engine_b, const ModelManagerConfig &cfg);
    ~ModelManager();

    // Loads the active slot; when it fails, marks it BAD and falls back to the previous one. Returns the result
    // of the model finally used (NoModel when none); `failed` (optional) receives why the first slot failed.
    ModelError boot(ModelCheck *check, ModelCheck *failed, bool *rolled_back);

    // A complete package was written into `slot` (state WRITING): checks it against the announced SHA-256,
    // tests it, then activates it. On failure the slot is marked BAD and the running model is unchanged.
    ModelError install(int slot, const uint8_t expected_sha256[32], ModelCheck *check);
    // Activates the previous slot (READY, checked again before use).
    ModelError rollback(ModelCheck *check);

    const ActiveModel &active() const { return active_; }
    // Runs the active model. False when there is none.
    bool infer(const int8_t in[kFeatures], int8_t out[kClassCount]);

    // ModelRunner
    bool model_available() const override { return active_.present; }
    const ModelContract &model_contract() const override { return active_.contract; }
    uint32_t model_version() const override { return active_.model_version; }
    bool model_infer(const int8_t in[kFeatures], int8_t out[kClassCount]) override { return infer(in, out); }
    void set_radar_gate_cm(uint32_t gate_cm) { cfg_.radar_gate_cm = gate_cm; }

private:
    // Reads, verifies and loads the package of `slot` into the spare engine; keeps its buffer on success.
    ModelError prepare(int slot, const uint8_t *expected_sha256, ModelCheck *check);
    void commit_prepared(int slot);
    void drop_prepared();

    ModelStore &store_;
    InferenceEngine *engines_[2];
    ModelManagerConfig cfg_;
    int running_; // engine index running the active model, -1 none
    uint8_t *buffers_[2];
    ActiveModel active_;
    ActiveModel prepared_;
};

} // namespace presence
