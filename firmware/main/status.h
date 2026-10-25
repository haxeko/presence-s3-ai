// JSON of presence/v1/dev/<id>/status (retained) and .../state (retained, on every change).
#pragma once

#include <stddef.h>

namespace app {

void status_build(char *out, size_t size);
void status_build_state(char *out, size_t size);
// Human-readable summary for the serial console
void status_print();

} // namespace app
