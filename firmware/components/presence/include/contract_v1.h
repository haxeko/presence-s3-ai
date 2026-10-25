// Input contract v1 (feature set "energy18_stats4_v1"), shared with training/presence_ml/contract.py.
// Changing anything here (channels, statistics, order, formulas, window, rate, quantization) needs a new
// contract version, a new firmware model API version and retraining.
#pragma once

#include <stdint.h>

namespace presence {

constexpr uint32_t kContractVersion = 1;
constexpr uint32_t kFeatureSetId = 1;
// Model compatibility of this firmware: packages declare the firmware model API range they accept.
constexpr uint32_t kFirmwareModelApi = 1;

constexpr int kGates = 9;
constexpr int kChannels = 2 * kGates; // m0..m8 moving energy, s0..s8 still energy
constexpr int kStats = 4;             // mean, std, max, mad (stat-major: index = stat * 18 + channel)
constexpr int kFeatures = kStats * kChannels; // 72
constexpr int kEnergyMax = 100;

constexpr uint32_t kSamplePeriodMs = 200; // 5 Hz
constexpr int kWindowSlots = 20;          // 4 s
constexpr int kHopSlots = 5;              // 1 s
constexpr int kMinValidSlots = 16;
constexpr int kMaxGapSlots = 3;
constexpr uint32_t kGateResolutionCm = 75;

constexpr int kClassCount = 4; // empty_quiet, empty_disturbance, occupied_still, occupied_moving
constexpr uint32_t kOccupiedMask = 0x0C;

// Decision method (column "meth")
constexpr uint8_t kMethodNone = 0;
constexpr uint8_t kMethodThresholdFactory = 1;
constexpr uint8_t kMethodThresholdBaseline = 2;
constexpr uint8_t kMethodModel = 3;

// Reported state (column "ps") and confirmed decision (column "pd")
constexpr uint8_t kStateUncertain = 0;
constexpr uint8_t kStateEmpty = 1;
constexpr uint8_t kStateOccupied = 2;
constexpr uint8_t kDecisionUnknown = 0;
constexpr uint8_t kDecisionEmpty = 1;
constexpr uint8_t kDecisionOccupied = 2;

} // namespace presence
