"""Prepara cada escenario con el outbox SQLite y el adaptador MQTT reales.

Lo único simulado es el broker: un doble del cliente paho que se puede
desconectar y que entrega el PUBACK por el mismo callback que usa paho.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from device_gateway.application.ingest_reading import IngestReadingUseCase
from device_gateway.application.publish_outbox import PublishOutboxUseCase
from device_gateway.domain.services import ReadingFactory
from device_gateway.infrastructure.models import ReadingRow
from device_gateway.infrastructure.mqtt_client import MqttGateway
from device_gateway.infrastructure.sqlite_outbox import SqliteReadingOutbox
from shared.config import EdgeSettings
from shared.database import init_db


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 11, 12, 15, 4, 4, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current


class _Info:
    def __init__(self, mid: int) -> None:
        self.mid, self.rc = mid, 0

    def wait_for_publish(self, timeout=None) -> None:
        return None


class FakeBroker:
    """Doble del cliente paho. Registra lo publicado y responde el PUBACK
    después de que el adaptador registró la espera, como hace paho."""

    def __init__(self) -> None:
        self.telemetry = []        # (mid, payload) en orden de publicación
        self.status_on_ack = []    # estados del outbox de ese mensaje al llegar su PUBACK
        self.on_connect = self.on_disconnect = self.on_publish = self.on_message = None
        self._mid = 0

    # --- API de paho que usa el adaptador ---
    def username_pw_set(self, *_):
        pass

    def tls_set(self):
        pass

    def will_set(self, *_, **__):
        pass

    def reconnect_delay_set(self, **_):
        pass

    def connect_async(self, *_, **__):
        pass  # el broker no responde hasta que el escenario lo levanta

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, *_, **__):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self._mid += 1
        mid = self._mid
        if topic.endswith("/telemetry"):
            message = json.loads(payload)
            self.telemetry.append((mid, message))
            asyncio.get_running_loop().call_soon(self._puback, mid, message["messageId"])
        return _Info(mid)

    # --- control desde los escenarios ---
    def become_available(self) -> None:
        self.on_connect(self, None, None, 0, None)

    def _puback(self, mid: int, message_id: str) -> None:
        rows = ReadingRow.select(ReadingRow.status).where(ReadingRow.message_id == message_id)
        self.status_on_ack.append({row.status for row in rows})
        self.on_publish(self, None, mid, 0, None)


async def _ignore_config(_device_id: str, _payload: bytes) -> None:
    return None


def before_scenario(context, scenario):
    context.database = init_db(":memory:")
    context.loop = asyncio.new_event_loop()
    context.clock = FixedClock()
    settings = EdgeSettings()
    context.device_id = settings.device_names[0]
    context.outbox = SqliteReadingOutbox()
    context.ingest = IngestReadingUseCase(context.outbox, ReadingFactory(settings.saturation_ut), context.clock)
    context.broker = FakeBroker()
    context.gateway = MqttGateway(
        settings, context.clock, [context.device_id], client_factory=lambda _s: context.broker
    )
    context.gateway.start(context.loop, _ignore_config)
    context.publish = PublishOutboxUseCase(
        context.outbox, context.gateway, context.clock, ack_timeout_s=2.0
    )


def after_scenario(context, scenario):
    context.loop.close()
    context.database.close()
