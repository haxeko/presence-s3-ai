// Model update (independent of firmware OTA): desired model from MQTT -> HTTP(S) download into the inactive A/B
// slot -> SHA-256, signature, compatibility, memory, self-test -> activation; the running model keeps working
// meanwhile and the previous one is kept for rollback. Progress and errors go to presence/v1/dev/<id>/event.
#pragma once

#include <stddef.h>

namespace presence {
class ModelStore;
}

namespace app {

// Creates the model manager (g.models); no model is loaded yet.
void model_update_create();
// Loads the active model (falls back to the previous slot, then to the threshold method) and starts the update
// task. Call after radar_start(): the radar's gate length is checked against the model contract.
void model_update_boot();
// Retained presence/v1/dev/<id>/desired/model message (empty payload: nothing desired).
void model_update_desired(const char *json, size_t len);
// Phase of the last update for the status message: idle, download, verify, active, failed...
void model_update_status(char *phase, size_t phase_len, char *error, size_t error_len, unsigned *version);
// Console: switch to the previous slot.
bool model_update_rollback();
// For the status message (read-only).
presence::ModelStore *model_store_for_status();

} // namespace app
