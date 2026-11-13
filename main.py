"""Punto de entrada del Edge industrial. La configuración se lee del entorno (ver README)."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from device_gateway.interfaces.runner import build_runner
from shared.config import EdgeSettings


def _configure_logging() -> None:
    level = os.environ.get("EMSAFE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # bleak es muy verboso en DEBUG.
    logging.getLogger("bleak").setLevel(logging.WARNING)


async def _main() -> None:
    try:
        settings = EdgeSettings.from_env()
    except ValueError as exc:
        logging.getLogger("main").error("Configuración inválida: %s", exc)
        sys.exit(2)
    logging.getLogger("main").info(
        "Nodo %s (organización %s), dosímetros: %s, broker %s:%s, base %s",
        settings.edge_node_id, settings.organization_id, ", ".join(settings.device_names),
        settings.mqtt_host, settings.mqtt_port, settings.db_path,
    )
    runner = build_runner(settings)
    loop = asyncio.get_running_loop()

    def request_stop(*_args) -> None:
        # Un segundo Ctrl+C lanza KeyboardInterrupt por si el apagado se cuelga.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        loop.call_soon_threadsafe(runner.stop)

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            loop.add_signal_handler(signal.SIGTERM, runner.stop)
        except (NotImplementedError, RuntimeError):
            pass  # no disponible en Windows
    await runner.run()


if __name__ == "__main__":
    _configure_logging()
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logging.getLogger("main").warning("Salida forzada")
