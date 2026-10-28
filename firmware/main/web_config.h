// Configuration web page on the device (http://<device ip>/): network and MQTT settings without the serial console.
// Values are stored in NVS (same keys as the console) and applied after an automatic restart.
// No authentication: anyone on the LAN can change these settings (model packages still need a valid signature).
// Disable with CONFIG_PRESENCE_WEB_CONFIG=n.
#pragma once

namespace app {

void web_config_start();

} // namespace app
