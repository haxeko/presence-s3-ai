// Features, window rules, quantization, presence logic and threshold method against Python (fixtures).
#include "window_features.h"
#include "model_contract.h"
#include "presence_logic.h"
#include "test_util.h"
#include "threshold_detector.h"

using namespace presence;

namespace {

// Reads 20 window rows "v e0..e17" starting at lines[i].
Window read_window(const std::vector<std::string> &lines, size_t i)
{
    Window w;
    for (int s = 0; s < kWindowSlots; s++) {
        const std::vector<std::string> t = split(lines[i + s]);
        w.valid[s] = t[0] == "1";
        for (int c = 0; c < kChannels; c++) {
            w.energy[s][c] = (uint8_t)atoi(t[1 + c].c_str());
        }
    }
    return w;
}

void test_features_fixture()
{
    test_begin("features: 72 values bit-identical to Python, window rules");
    const std::vector<std::string> lines = read_lines(fixture_path("features.txt"));
    int windows = 0;
    int bit_mismatch = 0;
    ModelContract c;
    memset(&c, 0, sizeof(c));
    for (size_t i = 0; i < lines.size(); i++) {
        if (starts_with(lines[i], "window ")) {
            const Window w = read_window(lines, i + 1);
            const std::vector<std::string> usable = split(lines[i + 21]);
            CHECK(usable[0] == "usable");
            CHECK_EQ((int)w.usable(kMinValidSlots, kMaxGapSlots), atoi(usable[1].c_str()));
            const std::vector<std::string> f = split(lines[i + 22]);
            CHECK(f[0] == "features" && f.size() == 1 + kFeatures);
            float out[kFeatures];
            CHECK(compute_features(w, out));
            for (int k = 0; k < kFeatures; k++) {
                if (f32_bits(out[k]) != f32_bits(hex_f32(f[1 + k]))) {
                    bit_mismatch++;
                    fprintf(stderr, "    %s feature %d: %.9g vs %.9g\n", lines[i].c_str(), k, out[k], hex_f32(f[1 + k]));
                }
            }
            windows++;
            i += 22;
        } else if (starts_with(lines[i], "normalization ")) {
            const std::vector<std::string> t = split(lines[i]);
            CHECK(t.size() == 2 + 2 * kFeatures);
            for (int k = 0; k < kFeatures; k++) {
                c.feature_mean[k] = hex_f32(t[1 + k]);
                c.feature_std[k] = hex_f32(t[2 + kFeatures + k]);
            }
        } else if (starts_with(lines[i], "quant ")) {
            const std::vector<std::string> t = split(lines[i]);
            c.input_scale = hex_f32(t[1]);
            c.input_zero_point = atoi(t[2].c_str());
        } else if (starts_with(lines[i], "qcase ")) {
            const std::vector<std::string> t = split(lines[i]);
            float f[kFeatures];
            for (int k = 0; k < kFeatures; k++) {
                f[k] = hex_f32(t[1 + k]);
            }
            int8_t q[kFeatures];
            quantize_features(c, f, q);
            for (int k = 0; k < kFeatures; k++) {
                CHECK_EQ((int)q[k], atoi(t[2 + kFeatures + k].c_str()));
            }
        } else if (starts_with(lines[i], "qedge ")) {
            // z values quantized with mean 0, std 1: lroundf half away from zero, clamping
            const std::vector<std::string> t = split(lines[i]);
            ModelContract e;
            memset(&e, 0, sizeof(e));
            e.input_scale = hex_f32(t[1]);
            e.input_zero_point = atoi(t[2].c_str());
            for (int k = 0; k < kFeatures; k++) {
                e.feature_std[k] = 1.0f;
            }
            size_t bar = 0;
            while (t[bar] != "|") {
                bar++;
            }
            const size_t n = bar - 3;
            float z[kFeatures] = {};
            for (size_t k = 0; k < n; k++) {
                z[k] = hex_f32(t[3 + k]);
            }
            int8_t q[kFeatures];
            quantize_features(e, z, q);
            for (size_t k = 0; k < n; k++) {
                CHECK_EQ((int)q[k], atoi(t[bar + 1 + k].c_str()));
            }
        }
    }
    CHECK(windows >= 20);
    CHECK_EQ(bit_mismatch, 0);
}

void test_slot_window()
{
    test_begin("features: sliding window keeps the last 20 ticks, skipped ticks are missing");
    SlotWindow sw;
    Window w;
    sw.snapshot(&w);
    CHECK_EQ(w.n_valid(), 0);
    uint8_t e[kChannels];
    for (uint32_t t = 0; t < 25; t++) {
        memset(e, (int)t, sizeof(e));
        sw.push(t, e);
    }
    sw.snapshot(&w);
    CHECK_EQ(w.n_valid(), 20);
    CHECK_EQ((int)w.energy[0][0], 5); // oldest = tick 5
    CHECK_EQ((int)w.energy[19][17], 24);
    sw.push(25, nullptr);
    sw.push(28, e); // 26, 27 skipped
    sw.snapshot(&w);
    CHECK(!w.valid[16] && !w.valid[17] && !w.valid[18] && w.valid[19]);
    CHECK_EQ(w.n_valid(), 17); // ticks 9..24 and 28
    CHECK_EQ(w.longest_gap(), 3);
    CHECK(w.usable(17, 3) && !w.usable(18, 3) && !w.usable(16, 2));
    sw.push(28, nullptr); // repeated tick ignored
    sw.snapshot(&w);
    CHECK(w.valid[19]);
    sw.push(100, e); // long silence: only the newest slot is valid
    sw.snapshot(&w);
    CHECK_EQ(w.n_valid(), 1);
    CHECK(w.valid[19]);
}

void test_presence_fixture()
{
    test_begin("presence: state and decision sequences equal Python");
    const std::vector<std::string> lines = read_lines(fixture_path("presence.txt"));
    PresenceLogic logic(model_presence_defaults());
    int steps = 0;
    for (const std::string &line : lines) {
        const std::vector<std::string> t = split(line);
        if (t[0] == "params") {
            PresenceParams p{ hex_f32(t[1]), hex_f32(t[2]), (uint32_t)atoi(t[3].c_str()),
                              (uint32_t)atoi(t[4].c_str()) };
            CHECK(p.valid());
            logic = PresenceLogic(p);
        } else if (t[0] == "step") {
            float s = 0;
            const bool none = t[1] == "none";
            if (!none) {
                s = hex_f32(t[1]);
            }
            logic.step(none ? nullptr : &s);
            CHECK_EQ((int)logic.state(), atoi(t[2].c_str()));
            CHECK_EQ((int)logic.decision(), atoi(t[3].c_str()));
            steps++;
        }
    }
    CHECK(steps > 300);

    test_begin("presence: missing data never confirms empty; parameters validated");
    PresenceLogic l(PresenceParams{ 0.7f, 0.3f, 2000, 5000 });
    float hi = 0.9f, lo = 0.1f;
    l.step(&hi);
    l.step(&hi);
    CHECK(l.decision() == kDecisionOccupied && l.state() == kStateOccupied);
    for (int i = 0; i < 100; i++) {
        l.step(nullptr);
    }
    CHECK(l.decision() == kDecisionOccupied && l.state() == kStateUncertain);
    for (int i = 0; i < 4; i++) {
        l.step(&lo);
    }
    CHECK(l.decision() == kDecisionOccupied && l.state() == kStateUncertain);
    l.step(&lo);
    CHECK(l.decision() == kDecisionEmpty && l.state() == kStateEmpty);
    CHECK(!PresenceParams({ 0.3f, 0.3f, 1000, 5000 }).valid());
    CHECK(!PresenceParams({ 0.7f, 0.3f, 1000, 500 }).valid());
    CHECK(!l.set_params(PresenceParams{ 1.5f, 0.3f, 1000, 5000 }));
}

void test_threshold_fixture()
{
    test_begin("threshold: window scores equal Python");
    const std::vector<std::string> lines = read_lines(fixture_path("threshold.txt"));
    Thresholds thr = factory_thresholds();
    int windows = 0;
    for (size_t i = 0; i < lines.size(); i++) {
        const std::vector<std::string> t = split(lines[i]);
        if (t[0] == "thr") {
            for (int g = 0; g < kGates; g++) {
                thr.moving[g] = (uint8_t)atoi(t[1 + g].c_str());
                thr.still[g] = (uint8_t)atoi(t[1 + kGates + g].c_str());
            }
            CHECK(thr.valid());
        } else if (t[0] == "window") {
            const Window w = read_window(lines, i + 1);
            const std::vector<std::string> s = split(lines[i + 21]);
            CHECK(s[0] == "score");
            float score = -1;
            CHECK(threshold_score(w, thr, &score));
            CHECK_EQ(f32_bits(score), f32_bits(hex_f32(s[1])));
            windows++;
            i += 21;
        }
    }
    CHECK_EQ(windows, 8);

    test_begin("threshold: 100 disables a gate, energy must be above the threshold");
    Thresholds t = factory_thresholds();
    uint8_t e[kChannels] = {};
    e[kGates + 0] = 100; // still gate 0 disabled
    CHECK(!sample_hit(e, t));
    e[3] = 30; // moving gate 3 threshold 30: equal is not a hit
    CHECK(!sample_hit(e, t));
    e[3] = 31;
    CHECK(sample_hit(e, t));
}

} // namespace

void run_feature_tests()
{
    test_features_fixture();
    test_slot_window();
    test_presence_fixture();
    test_threshold_fixture();
}
