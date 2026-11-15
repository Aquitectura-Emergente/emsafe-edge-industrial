"""Pasos de reconexion.feature: ingesta, outbox y publicación reales; broker simulado."""
from behave import given, then, when

from device_gateway.application.ingest_reading import IngestResult
from device_gateway.infrastructure.models import ReadingRow, ReadingStatus

TS = 1762959843  # hora del dosímetro (epoch) de la primera lectura


def _recibir(context, sequence):
    return context.ingest.ingest(context.device_id, sequence, TS + sequence, 120.0 + sequence, 0)


def _ciclo_de_publicacion(context):
    return context.loop.run_until_complete(context.publish.run_once())


@given("que el broker MQTT no está disponible")
def step_broker_caido(context):
    assert not context.gateway.is_connected


@when("el Edge recibe {cantidad:d} lecturas del dosímetro")
def step_recibe_lecturas(context, cantidad):
    context.secuencias = list(range(1, cantidad + 1))
    for seq in context.secuencias:
        assert _recibir(context, seq) is IngestResult.STORED
    # El publicador sigue intentando mientras no hay broker.
    context.reporte = _ciclo_de_publicacion(context)


@then("las {cantidad:d} lecturas quedan pendientes en el outbox")
def step_quedan_pendientes(context, cantidad):
    assert context.reporte.published == 0, context.reporte
    assert context.broker.telemetry == []
    assert context.outbox.pending_count() == cantidad
    assert ReadingRow.select().where(ReadingRow.status == ReadingStatus.PUBLISHED).count() == 0


@when("el broker vuelve a estar disponible")
def step_broker_vuelve(context):
    context.broker.become_available()
    assert context.gateway.is_connected
    context.reporte = _ciclo_de_publicacion(context)


@then("el Edge publica las {cantidad:d} lecturas en orden")
def step_publica_en_orden(context, cantidad):
    assert not context.reporte.failed, context.reporte
    publicadas = [
        r["sequence"] for _mid, mensaje in context.broker.telemetry for r in mensaje["readings"]
    ]
    assert publicadas == context.secuencias[:cantidad], publicadas
    assert len(publicadas) == cantidad


@then("las marca como publicadas solo después del PUBACK")
def step_publicadas_tras_puback(context):
    # Al llegar cada PUBACK, sus lecturas aún esperaban confirmación.
    assert context.broker.status_on_ack, "no llegó ningún PUBACK"
    assert all(estados == {ReadingStatus.BATCHED} for estados in context.broker.status_on_ack), \
        context.broker.status_on_ack
    assert context.outbox.pending_count() == 0
    assert ReadingRow.select().where(ReadingRow.status != ReadingStatus.PUBLISHED).count() == 0


@given("que el Edge ya recibió la lectura con secuencia {sequence:d}")
def step_ya_recibida(context, sequence):
    assert _recibir(context, sequence) is IngestResult.STORED


@when("el dosímetro vuelve a enviar la lectura con secuencia {sequence:d}")
def step_reenvia(context, sequence):
    context.resultado = _recibir(context, sequence)


@then("el outbox contiene una sola lectura con secuencia {sequence:d}")
def step_una_sola(context, sequence):
    assert context.resultado is IngestResult.DUPLICATE
    filas = ReadingRow.select().where(
        (ReadingRow.device_id == context.device_id) & (ReadingRow.sequence == sequence)
    )
    assert filas.count() == 1
