#include "tflm_engine.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <new>

#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"

#ifdef ESP_PLATFORM
#include "esp_heap_caps.h"
#endif

namespace presence {

// FULLY_CONNECTED + SOFTMAX is what the converter emits for the 72-32-16-4 MLP; the others cover variants a
// converter may insert (reshape of the input, explicit quantize/dequantize). A model needing anything else is
// rejected by AllocateTensors and therefore by the self-test.
typedef tflite::MicroMutableOpResolver<5> Resolver;

static uint8_t *alloc_arena(size_t n)
{
#ifdef ESP_PLATFORM
    return (uint8_t *)heap_caps_aligned_alloc(16, n, MALLOC_CAP_8BIT);
#else
    void *p = nullptr;
    return posix_memalign(&p, 16, n) == 0 ? (uint8_t *)p : nullptr;
#endif
}

static void free_arena(uint8_t *p)
{
#ifdef ESP_PLATFORM
    heap_caps_free(p);
#else
    free(p);
#endif
}

TflmEngine::TflmEngine() : interpreter_(nullptr), resolver_(nullptr), arena_(nullptr), arena_used_(0), quant_{}
{
}

TflmEngine::~TflmEngine()
{
    unload();
}

void TflmEngine::unload()
{
    if (interpreter_) {
        delete (tflite::MicroInterpreter *)interpreter_;
        interpreter_ = nullptr;
    }
    if (resolver_) {
        delete (Resolver *)resolver_;
        resolver_ = nullptr;
    }
    if (arena_) {
        free_arena(arena_);
        arena_ = nullptr;
    }
    arena_used_ = 0;
    quant_ = EngineQuant{};
}

bool TflmEngine::load(const uint8_t *tflite_data, size_t len, size_t arena_bytes, char *err, size_t err_len)
{
    unload();
    if (((uintptr_t)tflite_data & 15) != 0) {
        snprintf(err, err_len, "flatbuffer not 16-byte aligned");
        return false;
    }
    flatbuffers::Verifier verifier(tflite_data, len);
    if (!tflite::VerifyModelBuffer(verifier)) {
        snprintf(err, err_len, "invalid tflite flatbuffer");
        return false;
    }
    const tflite::Model *model = tflite::GetModel(tflite_data);
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        snprintf(err, err_len, "schema %u != %d", (unsigned)model->version(), TFLITE_SCHEMA_VERSION);
        return false;
    }
    if (arena_bytes < 1024 || arena_bytes > 256 * 1024) {
        snprintf(err, err_len, "arena size %u", (unsigned)arena_bytes);
        return false;
    }
    arena_ = alloc_arena(arena_bytes);
    Resolver *resolver = new (std::nothrow) Resolver();
    if (!arena_ || !resolver) {
        delete resolver;
        unload();
        snprintf(err, err_len, "no memory for arena");
        return false;
    }
    resolver->AddFullyConnected();
    resolver->AddSoftmax();
    resolver->AddReshape();
    resolver->AddQuantize();
    resolver->AddDequantize();
    resolver_ = resolver;
    tflite::MicroInterpreter *it = new (std::nothrow) tflite::MicroInterpreter(model, *resolver, arena_, arena_bytes);
    if (!it) {
        unload();
        snprintf(err, err_len, "no memory for interpreter");
        return false;
    }
    interpreter_ = it;
    if (it->AllocateTensors() != kTfLiteOk) {
        unload();
        snprintf(err, err_len, "AllocateTensors failed (ops or arena)");
        return false;
    }
    TfLiteTensor *in = it->input(0);
    TfLiteTensor *out = it->output(0);
    if (it->inputs_size() != 1 || it->outputs_size() != 1 || in->type != kTfLiteInt8 || out->type != kTfLiteInt8 ||
        in->dims->size != 2 || in->dims->data[1] != kFeatures || out->dims->size != 2 ||
        out->dims->data[1] != kClassCount) {
        unload();
        snprintf(err, err_len, "model I/O is not int8 [1,%d] -> [1,%d]", kFeatures, kClassCount);
        return false;
    }
    quant_.input_scale = in->params.scale;
    quant_.input_zero_point = in->params.zero_point;
    quant_.output_scale = out->params.scale;
    quant_.output_zero_point = out->params.zero_point;
    arena_used_ = it->arena_used_bytes();
    return true;
}

bool TflmEngine::invoke(const int8_t *in, int8_t *out)
{
    tflite::MicroInterpreter *it = (tflite::MicroInterpreter *)interpreter_;
    if (!it) {
        return false;
    }
    memcpy(it->input(0)->data.int8, in, kFeatures);
    if (it->Invoke() != kTfLiteOk) {
        return false;
    }
    memcpy(out, it->output(0)->data.int8, kClassCount);
    return true;
}

} // namespace presence
