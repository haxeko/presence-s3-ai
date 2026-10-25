// On-target test of the presence component (ESP32-S3 or QEMU esp32s3). Prints one line per check and a final
// "PRESENCE TARGET TEST PASS|FAIL" line that firmware/test/qemu/run_target_test.sh looks for.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "crypto.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "model_manager.h"
#include "model_package.h"
#include "model_store.h"
#include "nvs_flash.h"
#include "partition_region.h"
#include "tflm_engine.h"
#include "window_features.h"

using namespace presence;

extern const char features_txt[] asm("_binary_features_txt_start");
extern const char pub_pem[] asm("_binary_signing_pub_pem_start");
extern const uint8_t pkg1_start[] asm("_binary_model_v1_pkg_start");
extern const uint8_t pkg1_end[] asm("_binary_model_v1_pkg_end");
extern const uint8_t pkg2_start[] asm("_binary_model_v2_pkg_start");
extern const uint8_t pkg2_end[] asm("_binary_model_v2_pkg_end");

static int s_failures;

static void check(bool ok, const char *what)
{
    printf("%s %s\n", ok ? "  ok  " : "  FAIL", what);
    if (!ok) {
        s_failures++;
    }
}

static float hexf(const char *h)
{
    uint8_t b[4];
    for (int i = 0; i < 4; i++) {
        char t[3] = { h[2 * i], h[2 * i + 1], 0 };
        b[i] = (uint8_t)strtoul(t, nullptr, 16);
    }
    uint32_t u = (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
    float f;
    memcpy(&f, &u, 4);
    return f;
}

// Same fixture as the host tests: features computed here must equal Python bit for bit.
static void test_features()
{
    const char *p = features_txt;
    int windows = 0, mismatches = 0;
    char line[1100];
    auto next_line = [&](char *out) -> bool {
        if (!*p) {
            return false;
        }
        const char *e = strchr(p, '\n');
        const size_t n = e ? (size_t)(e - p) : strlen(p);
        const size_t m = n < 1099 ? n : 1099;
        memcpy(out, p, m);
        out[m] = 0;
        p = e ? e + 1 : p + n;
        return true;
    };
    while (next_line(line)) {
        if (strncmp(line, "window ", 7) != 0) {
            continue;
        }
        Window w;
        for (int s = 0; s < kWindowSlots; s++) {
            next_line(line);
            char *tok = strtok(line, " ");
            w.valid[s] = tok && tok[0] == '1';
            for (int c = 0; c < kChannels; c++) {
                tok = strtok(nullptr, " ");
                w.energy[s][c] = tok ? (uint8_t)atoi(tok) : 0;
            }
        }
        next_line(line); // usable
        next_line(line); // features <72 hex>
        float out[kFeatures];
        compute_features(w, out);
        char *tok = strtok(line, " ");
        for (int k = 0; k < kFeatures; k++) {
            tok = strtok(nullptr, " ");
            const float e = tok ? hexf(tok) : NAN;
            uint32_t a, b;
            memcpy(&a, &out[k], 4);
            memcpy(&b, &e, 4);
            if (a != b) {
                mismatches++;
            }
        }
        windows++;
    }
    printf("features: %d windows, %d values differ from Python\n", windows, mismatches);
    check(windows >= 20 && mismatches == 0, "features bit-identical to Python (float32, ESP32-S3)");
}

static void test_packages()
{
    const uint8_t *pk[2] = { pkg1_start, pkg2_start };
    const size_t len[2] = { (size_t)(pkg1_end - pkg1_start), (size_t)(pkg2_end - pkg2_start) };
    for (int i = 0; i < 2; i++) {
        PkgInfo info;
        bool ok = parse_package(pk[i], len[i], &info) == PkgError::Ok && check_section_hashes(pk[i], info) == PkgError::Ok;
        uint8_t digest[32];
        const int64_t t0 = esp_timer_get_time();
        sha256(pk[i], info.total_size, digest);
        ok = ok && check_signature(info, digest, pub_pem) == PkgError::Ok;
        const int64_t t1 = esp_timer_get_time();
        printf("package %s v%u: %u bytes, sha256 + ECDSA verify %lld us\n", info.model_id,
               (unsigned)info.model_version, (unsigned)len[i], (long long)(t1 - t0));
        check(ok, "package parsed, section hashes and signature verified with mbedTLS");
    }
    // Tampered byte: signature must fail
    uint8_t *copy = (uint8_t *)malloc(len[0]);
    memcpy(copy, pk[0], len[0]);
    copy[30] ^= 1; // model_id byte, covered by the signature but not by any section hash
    PkgInfo info;
    uint8_t digest[32];
    parse_package(copy, len[0], &info);
    sha256(copy, info.total_size, digest);
    check(check_signature(info, digest, pub_pem) == PkgError::Signature, "tampered package rejected");
    free(copy);
}

static SemaphoreHandle_t s_lock;
static void lock_fn() { xSemaphoreTakeRecursive(s_lock, portMAX_DELAY); }
static void unlock_fn() { xSemaphoreGiveRecursive(s_lock); }
static size_t free_heap() { return heap_caps_get_free_size(MALLOC_CAP_8BIT); }
static int64_t now_us() { return esp_timer_get_time(); }

static bool download(ModelStore &store, const uint8_t *p, size_t n, int *slot, uint8_t sha[32])
{
    PkgInfo info;
    if (parse_package(p, n, &info) != PkgError::Ok) {
        return false;
    }
    sha256(p, n, sha);
    *slot = store.inactive_slot();
    if (!store.begin_write(*slot, info.model_version, info.model_id, (uint32_t)n, sha)) {
        return false;
    }
    for (size_t off = 0; off < n; off += 1024) {
        if (!store.write_chunk(*slot, off, p + off, n - off < 1024 ? n - off : 1024)) {
            return false;
        }
    }
    return true;
}

static void print_check(const char *what, const ModelCheck &c)
{
    printf("%s: %s (%s), selftest %u vectors, max diff feature %.3g input %d output %d, inference max %u us, "
           "arena %u bytes\n",
           what, model_error_name(c.error), c.detail, (unsigned)c.selftest.vectors, c.selftest.max_feature_diff,
           c.selftest.max_input_diff, c.selftest.max_output_diff, (unsigned)c.selftest.inference_us_max,
           (unsigned)c.selftest.arena_used);
}

static void test_update()
{
    PartitionRegion meta(PartitionRegion::find("model_meta"));
    PartitionRegion a(PartitionRegion::find("model_a"));
    PartitionRegion b(PartitionRegion::find("model_b"));
    check(meta.valid() && a.valid() && b.valid(), "model_meta, model_a, model_b partitions found");
    if (!meta.valid() || !a.valid() || !b.valid()) {
        return;
    }
    meta.erase(0, meta.size()); // fresh metadata for a repeatable test
    ModelStore store(&meta, &a, &b);
    static TflmEngine e1, e2;
    s_lock = xSemaphoreCreateRecursiveMutex();
    ModelManagerConfig cfg = {};
    cfg.public_key_pem = pub_pem;
    cfg.radar_gate_cm = 75;
    cfg.max_package_bytes = 192 * 1024;
    cfg.free_heap_reserve = 48 * 1024;
    cfg.free_heap = free_heap;
    cfg.now_us = now_us;
    cfg.lock = lock_fn;
    cfg.unlock = unlock_fn;
    ModelManager mgr(store, e1, e2, cfg);
    ModelCheck c;
    bool rolled = false;
    check(mgr.boot(&c, nullptr, &rolled) == ModelError::NoModel, "boot without model: threshold method");

    const size_t heap0 = free_heap();
    int slot;
    uint8_t sha[32];
    check(download(store, pkg1_start, pkg1_end - pkg1_start, &slot, sha), "v1 written to a slot");
    ModelError e = mgr.install(slot, sha, &c);
    print_check("install v1", c);
    check(e == ModelError::Ok && mgr.active().model_version == 1, "v1 verified, self-tested (TFLM + esp-nn) and active");
    printf("heap used by the active model: %d bytes (package + arena + interpreter)\n", (int)(heap0 - free_heap()));

    check(download(store, pkg2_start, pkg2_end - pkg2_start, &slot, sha), "v2 written to the other slot");
    e = mgr.install(slot, sha, &c);
    print_check("install v2", c);
    check(e == ModelError::Ok && mgr.active().model_version == 2, "v2 active, v1 kept");
    e = mgr.rollback(&c);
    check(e == ModelError::Ok && mgr.active().model_version == 1, "rollback to v1 without download");
    e = mgr.rollback(&c);
    check(e == ModelError::Ok && mgr.active().model_version == 2, "back to v2");

    // Inference timing on the active model (QEMU timings are not real hardware timings)
    int8_t in[kFeatures], out[kClassCount];
    for (int i = 0; i < kFeatures; i++) {
        in[i] = (int8_t)(i * 3 - 100);
    }
    const int64_t t0 = esp_timer_get_time();
    for (int i = 0; i < 1000; i++) {
        mgr.infer(in, out);
    }
    const int64_t t1 = esp_timer_get_time();
    printf("inference: %.1f us per call (1000 calls)\n", (t1 - t0) / 1000.0);

    // Corrupt the inactive slot (v1): rollback must refuse it and keep v2 running
    const int inactive = store.meta().previous;
    PartitionRegion &r = inactive == 0 ? a : b;
    uint8_t byte;
    r.read(500, &byte, 1);
    byte ^= 0xFF;
    r.erase(0, 4096); // destroy the first sector of the old package
    e = mgr.rollback(&c);
    check(e == ModelError::Sha256 && mgr.active().model_version == 2, "corrupted previous slot refused, v2 kept");

    // Boot again from flash (new manager, same partitions)
    ModelStore store2(&meta, &a, &b);
    static TflmEngine e3, e4;
    ModelManager mgr2(store2, e3, e4, cfg);
    e = mgr2.boot(&c, nullptr, &rolled);
    check(e == ModelError::Ok && !rolled && mgr2.active().model_version == 2, "boot loads the active slot (v2)");
}

extern "C" void app_main(void)
{
    nvs_flash_init();
    printf("PRESENCE TARGET TEST START (%s)\n", esp_get_idf_version());
    test_features();
    test_packages();
    test_update();
    printf("free heap %u, minimum %u\n", (unsigned)free_heap(),
           (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT));
    printf("PRESENCE TARGET TEST %s (%d failures)\n", s_failures ? "FAIL" : "PASS", s_failures);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
