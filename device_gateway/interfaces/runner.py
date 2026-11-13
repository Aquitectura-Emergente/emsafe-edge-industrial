"""Orquestación del Edge: clientes BLE, ingesta, publicador y limpieza en un loop asyncio.

`build_runner(settings)` arma las piezas reales; los tests pasan dobles a `EdgeRunner`.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import List, Optional, Sequence

from device_gateway.application.apply_device_config import ApplyDeviceConfigUseCase
from device_gateway.application.ingest_reading import IngestReadingUseCase, IngestResult
from device_gateway.application.publish_outbox import PublishOutboxUseCase
from device_gateway.application.purge_outbox import PurgeOutboxUseCase
from device_gateway.application.track_connectivity import TrackConnectivityUseCase
from device_gateway.infrastructure.ble_dosimeter_client import ReadingReceived, SessionStarted

log = logging.getLogger(__name__)


class EdgeRunner:
    def __init__(
        self,
        *,
        queue: asyncio.Queue,
        ingest: IngestReadingUseCase,
        publish: PublishOutboxUseCase,
        purge: PurgeOutboxUseCase,
        connectivity: TrackConnectivityUseCase,
        apply_config: ApplyDeviceConfigUseCase,
        mqtt,
        ble_clients: Sequence,
        publish_interval_s: float = 1.0,
        purge_interval_s: float = 3600.0,
        shutdown_timeout_s: float = 5.0,
    ) -> None:
        self._queue = queue
        self._ingest = ingest
        self._publish = publish
        self._purge = purge
        self._connectivity = connectivity
        self._apply_config = apply_config
        self._mqtt = mqtt
        self._ble_clients = list(ble_clients)
        self._publish_interval_s = publish_interval_s
        self._purge_interval_s = purge_interval_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._stop: Optional[asyncio.Event] = None
        self._wake: Optional[asyncio.Event] = None

    def stop(self) -> None:
        """Pide un apagado ordenado. Se llama desde el loop."""
        if self._stop is not None:
            self._stop.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._mqtt.start(loop, self._apply_config.on_config_message, self._on_broker_connected)

        ble_tasks = [
            asyncio.create_task(c.run(), name=f"ble:{c.device_name}") for c in self._ble_clients
        ]
        worker_tasks = [
            asyncio.create_task(self._consume(), name="ingesta"),
            asyncio.create_task(self._publish_loop(), name="publicador"),
            asyncio.create_task(self._purge_loop(), name="limpieza"),
        ]
        log.info("Edge industrial en marcha: %d dosímetro(s)", len(self._ble_clients))
        try:
            await self._stop.wait()
        finally:
            await self._shutdown(ble_tasks, worker_tasks)

    # ---------- Tareas ----------

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if isinstance(item, SessionStarted):
                    self._ingest.session_started(item.device_id)
                elif isinstance(item, ReadingReceived):
                    raw = item.raw
                    result = self._ingest.ingest(item.device_id, raw.seq, raw.ts, raw.uT, raw.deferred)
                    # Las de tiempo real salen enseguida; las diferidas esperan
                    # al próximo ciclo para formar lotes.
                    if result is IngestResult.STORED and raw.deferred != 1:
                        self._wake.set()
            except Exception:
                # Una lectura con error no detiene la ingesta.
                log.exception("Error al ingerir %r", item)
            finally:
                self._queue.task_done()

    async def _publish_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._publish_interval_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if not self._mqtt.is_connected:
                continue
            try:
                await self._publish.run_once()
            except Exception:
                log.exception("Error en el ciclo de publicación")

    async def _purge_loop(self) -> None:
        while True:
            try:
                self._purge.run_once()
            except Exception:
                log.exception("Error en la limpieza del outbox")
            await asyncio.sleep(self._purge_interval_s)

    def _on_broker_connected(self) -> None:
        # paho lo invoca vía call_soon_threadsafe: corre en el loop.
        self._connectivity.republish_all()
        if self._wake is not None:
            self._wake.set()

    # ---------- Apagado ----------

    async def _shutdown(self, ble_tasks: List[asyncio.Task], worker_tasks: List[asyncio.Task]) -> None:
        log.info("Apagando el Edge...")
        # BLE primero, para que el OFFLINE de cada dosímetro salga con MQTT arriba.
        for client in self._ble_clients:
            try:
                await client.stop()
            except Exception:
                log.exception("Error al detener %s", getattr(client, "device_name", client))
        if ble_tasks:
            done, pending = await asyncio.wait(ble_tasks, timeout=self._shutdown_timeout_s)
            for task in pending:
                task.cancel()
        # Lo no publicado queda en el outbox.
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*ble_tasks, *worker_tasks, return_exceptions=True)
        try:
            self._mqtt.stop()
        except Exception:
            log.exception("Error al detener MQTT")
        log.info("Edge detenido")


def build_runner(settings) -> EdgeRunner:
    """Arma el Edge con los adaptadores reales."""
    from device_gateway.domain.services import BatchPolicy, ReadingFactory
    from device_gateway.infrastructure.ble_dosimeter_client import BleDosimeterClient, BleDosimeterLinks
    from device_gateway.infrastructure.mqtt_client import MqttGateway
    from device_gateway.infrastructure.mqtt_payloads import parse_device_config
    from device_gateway.infrastructure.sqlite_config_store import SqliteConfigStore
    from device_gateway.infrastructure.sqlite_outbox import SqliteReadingOutbox
    from shared.clock import SystemClock
    from shared.database import init_db

    init_db(settings.db_path)
    clock = SystemClock()
    outbox = SqliteReadingOutbox()
    queue: asyncio.Queue = asyncio.Queue()
    mqtt = MqttGateway(settings, clock, settings.device_names)
    links = BleDosimeterLinks()
    connectivity = TrackConnectivityUseCase(mqtt, clock)
    apply_config = ApplyDeviceConfigUseCase(
        SqliteConfigStore(),
        links,
        partial(parse_device_config, saturation_ut=settings.saturation_ut),
        settings.default_threshold_ut,
    )
    ble_clients = []
    for name in settings.device_names:
        client = BleDosimeterClient(name, queue, apply_config.threshold_for, connectivity, clock)
        links.register(client)
        ble_clients.append(client)

    return EdgeRunner(
        queue=queue,
        ingest=IngestReadingUseCase(outbox, ReadingFactory(settings.saturation_ut), clock),
        publish=PublishOutboxUseCase(outbox, mqtt, clock, BatchPolicy()),
        purge=PurgeOutboxUseCase(outbox, clock, settings.outbox_retention_days),
        connectivity=connectivity,
        apply_config=apply_config,
        mqtt=mqtt,
        ble_clients=ble_clients,
    )
