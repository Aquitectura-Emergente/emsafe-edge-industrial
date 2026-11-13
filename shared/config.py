"""Configuración del Edge, leída de variables de entorno.

Las credenciales llegan solo por entorno. `from_env` recibe el entorno como
argumento para poder probarlo sin tocar `os.environ`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

_TRUE = {"1", "true", "yes", "si", "sí", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} debe ser numérico (recibido: {raw!r})")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{name} debe ser un número finito (recibido: {raw!r})")
    return value


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} debe ser un entero (recibido: {raw!r})")


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} debe ser true/false (recibido: {raw!r})")


def _names(env: Mapping[str, str], name: str, default: str) -> Tuple[str, ...]:
    raw = _text(env, name, default)
    names = tuple(dict.fromkeys(n.strip() for n in raw.split(",") if n.strip()))
    if not names:
        raise ValueError(f"{name} debe tener al menos un nombre BLE")
    return names


@dataclass(frozen=True)
class EdgeSettings:
    """Parámetros del nodo, leídos una vez al arrancar."""

    edge_node_id: str = "EDGE-PLANTA-01"
    organization_id: str = "org-demo"
    device_names: Tuple[str, ...] = ("EMSafe-DOS-01",)
    default_threshold_ut: float = 300.0
    saturation_ut: float = 790.0
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_tls: bool = False
    # Fuera del repr para que no aparezcan en logs.
    mqtt_username: Optional[str] = field(default=None, repr=False)
    mqtt_password: Optional[str] = field(default=None, repr=False)
    db_path: str = "edge_industrial.db"
    # Días que se conservan las lecturas YA publicadas (sirven para descartar
    # reenvíos del dosímetro). Las pendientes nunca se borran por antigüedad.
    outbox_retention_days: int = 7

    def __post_init__(self) -> None:
        if self.outbox_retention_days < 1:
            raise ValueError("EMSAFE_OUTBOX_RETENTION_DAYS debe ser al menos 1")
        if not 0 < self.saturation_ut:
            raise ValueError("EMSAFE_SATURATION_UT debe ser mayor que 0")
        # El umbral por defecto tiene que poder dispararse antes de saturar.
        if not 0 < self.default_threshold_ut < self.saturation_ut:
            raise ValueError(
                "EMSAFE_DEFAULT_THRESHOLD_UT debe ser mayor que 0 y menor que "
                f"EMSAFE_SATURATION_UT ({self.saturation_ut})"
            )
        if not 0 < self.mqtt_port < 65536:
            raise ValueError("EMSAFE_MQTT_PORT debe estar entre 1 y 65535")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "EdgeSettings":
        env = os.environ if env is None else env
        defaults = cls()
        return cls(
            edge_node_id=_text(env, "EMSAFE_EDGE_NODE_ID", defaults.edge_node_id),
            organization_id=_text(env, "EMSAFE_ORGANIZATION_ID", defaults.organization_id),
            device_names=_names(env, "EMSAFE_DEVICE_NAMES", ",".join(defaults.device_names)),
            default_threshold_ut=_float(
                env, "EMSAFE_DEFAULT_THRESHOLD_UT", defaults.default_threshold_ut
            ),
            saturation_ut=_float(env, "EMSAFE_SATURATION_UT", defaults.saturation_ut),
            mqtt_host=_text(env, "EMSAFE_MQTT_HOST", defaults.mqtt_host),
            mqtt_port=_int(env, "EMSAFE_MQTT_PORT", defaults.mqtt_port),
            mqtt_tls=_bool(env, "EMSAFE_MQTT_TLS", defaults.mqtt_tls),
            mqtt_username=env.get("EMSAFE_MQTT_USERNAME") or None,
            mqtt_password=env.get("EMSAFE_MQTT_PASSWORD") or None,
            db_path=_text(env, "EMSAFE_DB_PATH", defaults.db_path),
            outbox_retention_days=_int(
                env, "EMSAFE_OUTBOX_RETENTION_DAYS", defaults.outbox_retention_days
            ),
        )
