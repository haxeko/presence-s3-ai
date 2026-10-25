// Host tests of the firmware core (firmware/main/core), no ESP32 needed.
// Fixtures in firmware/test/fixtures are written by `python -m presence_ml.fixtures` (Python side of each check).
#include "test_util.h"

void run_ld2410_tests();
void run_feature_tests();
void run_sampling_tests();
void run_model_tests();

int main()
{
    printf("LD2410 parser\n");
    run_ld2410_tests();
    printf("Features, presence logic, threshold method\n");
    run_feature_tests();
    printf("Sampler, offline buffer, JSON rows\n");
    run_sampling_tests();
    printf("Model packages, A/B update, pipeline\n");
    run_model_tests();
    return test_summary();
}
