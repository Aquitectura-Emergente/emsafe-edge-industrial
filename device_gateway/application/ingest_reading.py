"""Caso de uso: validar, deduplicar y guardar en el outbox una lectura recibida por BLE."""
from __future__ import annotations

import logging
from enum import Enum
from typing import Dict

from device_gateway.domain.ports import Clock, ReadingOutbox
from device_gateway.domain.services import ReadingFactory
from device_gateway.domain.value_objects import InvalidReading

log = logging.getLogger(__name__)


class IngestResult(Enum):
    STORED = "STORED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


class IngestReadingUseCase:
    def __init__(self, outbox: ReadingOutbox, factory: ReadingFactory, clock: Clock) -> None:
        self._outbox = outbox
        self._factory = factory
        self._clock = clock
        # Último `sequence` con ts = 0 de la sesión BLE actual, por dispositivo.
        self._last_unsynced_seq: Dict[str, int] = {}

    def session_started(self, device_id: str) -> None:
        """Nueva conexión BLE: el firmware reenvía su buffer desde el principio, así que `sequence` reinicia."""
        self._last_unsynced_seq.pop(device_id, None)

    def ingest(self, device_id: str, sequence: int, ts: int, value, deferred: int) -> IngestResult:
        received_at = self._clock.now()
        try:
            reading = self._factory.from_raw(device_id, sequence, ts, value, deferred, received_at)
        except InvalidReading as exc:
            log.warning(
                "Lectura descartada de %s (seq=%s, ts=%s): %s", device_id, sequence, ts, exc
            )
            return IngestResult.REJECTED

        if ts == 0:
            self._check_boot_regression(device_id, sequence)

        if not self._outbox.add(reading):
            log.debug("Lectura duplicada de %s (seq=%s, ts=%s): se ignora", device_id, sequence, ts)
            return IngestResult.DUPLICATE
        return IngestResult.STORED

    def _check_boot_regression(self, device_id: str, sequence: int) -> None:
        """Avisa si el buffer mezcla varios arranques sin hora.

        En una sesión el buffer llega en orden: si `sequence` retrocede entre
        lecturas con ts = 0, hay lecturas de otro arranque que pueden
        descartarse como duplicadas (falta un `bootId` en el firmware).
        """
        previous = self._last_unsynced_seq.get(device_id)
        if previous is not None and sequence <= previous:
            log.warning(
                "El sequence de %s retrocedió de %s a %s en lecturas sin hora (ts=0): el "
                "buffer mezcla varios arranques del dosímetro y algunas lecturas pueden "
                "descartarse como duplicadas (limitación conocida, pendiente bootId).",
                device_id, previous, sequence,
            )
        self._last_unsynced_seq[device_id] = sequence
