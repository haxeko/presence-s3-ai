"""Cầu nối MQTT (paho-mqtt 2.x): nhận mẫu/trạng thái/sự kiện từ thiết bị, gửi cấu hình và mô hình mong muốn.

Chủ đề (prefix mặc định presence/v1, xem docs/mqtt-topics.md):
    dev/<id>/samples          thiết bị -> backend   lô mẫu 5 Hz (QoS 1)
    dev/<id>/status           thiết bị -> backend   trạng thái (retained, LWT {"online": false})
    dev/<id>/state            thiết bị -> backend   trạng thái hiện diện (retained)
    dev/<id>/event            thiết bị -> backend   tiến trình cập nhật mô hình, cấu hình bị từ chối, lệnh reboot
    dev/<id>/desired/model    backend -> thiết bị   mô hình mong muốn (retained)
    dev/<id>/desired/config   backend -> thiết bị   cấu hình (retained)
    dev/<id>/cmd              backend -> thiết bị   lệnh một lần (reboot, radar_reinit, ping, republish)
"""

from __future__ import annotations

import json
import logging
import ssl
import threading

from .config import Settings
from .service import PresenceService

log = logging.getLogger("presence.mqtt")


class MqttBridge:
    def __init__(self, settings: Settings, service: PresenceService | None = None):
        import paho.mqtt.client as mqtt

        self.s = settings
        self.svc = service
        self.connected = threading.Event()
        # Phiên bền (clean_session=False, client_id cố định): khi backend mất kết nối hoặc khởi động lại, broker giữ
        # các lô mẫu QoS 1 cho nó (max_queued_messages trong mosquitto.conf). Nếu không, mẫu thiết bị gửi trong lúc
        # backend vắng mặt sẽ mất dù thiết bị đã nhận PUBACK.
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=settings.mqtt_client_id,
                                  clean_session=False)
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        if settings.mqtt_tls:
            ctx = ssl.create_default_context(cafile=settings.mqtt_ca_file or None)
            self.client.tls_set_context(ctx)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 30)
        self.stats = {"messages": 0, "errors": 0}

    # Publisher
    def publish(self, topic: str, payload: dict | None, retain: bool = False, qos: int = 1) -> bool:
        data = b"" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        info = self.client.publish(topic, data, qos=qos, retain=retain)
        if info.rc != 0:
            log.warning("publish %s rc=%s", topic, info.rc)
            return False
        return True

    def start(self) -> None:
        self.client.connect_async(self.s.mqtt_host, self.s.mqtt_port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        p = self.s.topic_prefix
        for suffix in ("samples", "status", "state", "event"):
            client.subscribe(f"{p}/dev/+/{suffix}", qos=1)
        self.connected.set()
        log.info("MQTT connected to %s:%s", self.s.mqtt_host, self.s.mqtt_port)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected.clear()
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        self.stats["messages"] += 1
        try:
            parts = msg.topic.split("/")
            prefix_len = len(self.s.topic_prefix.split("/"))
            if len(parts) != prefix_len + 3 or parts[prefix_len] != "dev":
                return
            device_id, kind = parts[prefix_len + 1], parts[prefix_len + 2]
            if not msg.payload:
                return
            payload = json.loads(msg.payload.decode("utf-8"))
            if self.svc is None:
                return
            if kind == "samples":
                self.svc.handle_samples(device_id, payload)
            elif kind == "status":
                self.svc.handle_status(device_id, payload)
            elif kind == "event":
                self.svc.handle_event(device_id, payload)
            elif kind == "state":
                self.svc.ensure_device(device_id)
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            log.exception("message on %s", msg.topic)
