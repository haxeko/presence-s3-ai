// Trust anchors for mqtts:// and https://. A private CA (firmware/certs/ca.pem, embedded at build time when the file
// exists) takes precedence over the ESP-IDF bundle of public CAs. Without either, TLS connections fail: there is no
// "skip verification" path.
#pragma once

#include "sdkconfig.h"

#if PRESENCE_PRIVATE_CA
extern const char presence_private_ca_pem[] asm("_binary_ca_pem_start");
#define PRESENCE_TLS_TRUST "private_ca"
#elif CONFIG_PRESENCE_TLS_USE_CRT_BUNDLE
#define PRESENCE_TLS_TRUST "crt_bundle"
#else
#define PRESENCE_TLS_TRUST "none"
#endif
