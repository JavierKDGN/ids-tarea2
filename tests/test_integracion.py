"""Pruebas de integracion por HTTP verdadero, sin Docker aún. pytest -q

Cada suite inicia 5 procesos locales con bases SQLite temporales, aisladas.
"""
import concurrent.futures
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    carpeta = tmp_path_factory.mktemp("saga_ab")
    nombres = ("saga", "habitaciones", "pagos", "confirmaciones", "pasarela")
    puertos = {name: puerto_libre() for name in nombres}
    direcciones = {name: f"http://127.0.0.1:{puertos[name]}" for name in nombres}
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT), "SAGA_DB": str(carpeta / "saga.db"),
        "HABITACIONES_DB": str(carpeta / "habitaciones.db"),
        "PAGOS_DB": str(carpeta / "pagos.db"),
        "CONFIRMACIONES_DB": str(carpeta / "confirmaciones.db"),
        "PASARELA_DB": str(carpeta / "pasarela.db"),
        "HABITACIONES_URL": direcciones["habitaciones"],
        "PAGOS_URL": direcciones["pagos"],
        "CONFIRMACION_URL": direcciones["confirmaciones"],
        "PASARELA_URL": direcciones["pasarela"],
        "PASARELA_TIMEOUT": "0.5", "SERVICES_TIMEOUT": "2.0",
        "CB_FAILURE_THRESHOLD": "2", "CB_RECOVERY_SECONDS": "3",
        "BULKHEAD_MAX_CONCURRENT": "2",
    })
    procesos, logs = [], []
    try:
        for name in nombres:
            f = open(carpeta / f"{name}.log", "w")
            logs.append(f)
            procesos.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", f"src.servicios.{name}.main:app",
                 "--host", "127.0.0.1", "--port", str(puertos[name]), "--no-access-log"],
                cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT))
        for name in nombres:
            for _ in range(150):
                try:
                    if httpx.get(f"{direcciones[name]}/health", timeout=0.3).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError(f"No arranco {name}. Ver {carpeta / (name + '.log')}")
        yield direcciones
    finally:
        for p in procesos:
            p.terminate()
        for p in procesos:
            try:
                p.wait(timeout=4)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in logs:
            f.close()


def crear(server, rid, habitacion=10, monto="125.50", desde="2027-05-01", hasta="2027-05-03"):
    return httpx.post(server["saga"] + "/reservas", json={
        "id_reserva": rid, "habitacion_id": habitacion,
        "fecha_inicio": desde, "fecha_fin": hasta, "monto": monto},
        headers={"Idempotency-Key": rid}, timeout=5)


def chaos(server, caida=False, delay=0):
    httpx.post(server["confirmaciones"] + "/chaos", json={"forzar_error": False})
    httpx.post(server["pasarela"] + "/chaos",
               json={"latencia_segundos": delay, "forzar_error": caida})
    httpx.post(server["pagos"] + "/demo/reset-resiliencia")


def test_feliz_y_reintentos_idempotentes(server):
    chaos(server)
    rid = "feliz-" + uuid.uuid4().hex[:8]
    primero = crear(server, rid)
    assert primero.status_code == 201, primero.text
    assert primero.json()["estado"] == "CONFIRMADA"
    assert crear(server, rid).json()["estado"] == "CONFIRMADA"
    assert httpx.get(f"{server['pagos']}/pagos/pay-{rid}").json()["monto"] == "125.50"
    assert httpx.get(f"{server['confirmaciones']}/confirmaciones/voucher-{rid}").json()["estado"] == "EMITIDO"
    assert crear(server, rid, monto="150.00").status_code == 409


def test_superposicion_habitaciones(server):
    chaos(server)
    assert crear(server, "ocupada-1", habitacion=22).json()["estado"] == "CONFIRMADA"
    assert crear(server, "ocupada-2", habitacion=22).json()["estado"] == "FALLIDA"
    assert crear(server, "contigua", habitacion=22, desde="2027-05-03", hasta="2027-05-05").json()["estado"] == "CONFIRMADA"


def test_compensar_reembolso_antes_de_liberar(server):
    chaos(server)
    httpx.post(server["confirmaciones"] + "/chaos", json={"forzar_error": True})
    rid = "fallo-conf-" + uuid.uuid4().hex[:8]
    try:
        r = crear(server, rid, habitacion=30)
    finally:
        httpx.post(server["confirmaciones"] + "/chaos", json={"forzar_error": False})
    assert r.status_code == 502, r.text
    assert r.json()["estado"] == "COMPENSADA"
    assert httpx.get(f"{server['pagos']}/pagos/pay-{rid}").json()["estado"] == "REEMBOLSADO"
    assert httpx.get(f"{server['habitaciones']}/reservas/{rid}").json()["estado"] == "LIBERADA"
    assert crear(server, rid, habitacion=30).json()["estado"] == "COMPENSADA"
    assert httpx.post(f"{server['habitaciones']}/reservas/{rid}/liberar").json()["estado"] == "LIBERADA"
    assert httpx.post(f"{server['pagos']}/pagos/pay-{rid}/reembolsar").json()["estado"] == "REEMBOLSADO"


def test_bulkhead_y_circuit_breaker(server):
    chaos(server, delay=1.5)
    def pagar(i):
        rid = f"paralelo-{uuid.uuid4().hex[:10]}"
        return httpx.post(server["pagos"] + "/pagos", json={
            "id_pago": f"pay-{rid}", "id_reserva": rid, "monto": "10.00"}, timeout=4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(pagar, range(8)))
    assert all(r.status_code in (202, 503) for r in results)
    metricas = httpx.get(server["pagos"] + "/metricas").json()
    assert metricas["rechazos_bulkhead"] > 0, metricas
    assert metricas["timeouts"] >= 2, metricas
    assert metricas["circuit_breaker"] == "OPEN", metricas
    inicio = time.monotonic()
    rapido = pagar(100)
    assert time.monotonic() - inicio < 0.8
    assert rapido.status_code == 503
    assert httpx.get(server["pagos"] + "/metricas").json()["rechazos_circuito"] >= 1
    assert httpx.get(server["habitaciones"] + "/health").status_code == 200
    assert httpx.get(server["confirmaciones"] + "/health").status_code == 200
    # Las cobranzas tardias podrian ocurrir; la suite usa su propia BD temporal.


def test_timeout_de_pago_no_deja_cobro_huerfano(server):
    chaos(server, delay=1.5)
    rid = "incierto-" + uuid.uuid4().hex[:8]
    r = crear(server, rid, habitacion=40)
    assert r.status_code == 202, r.text
    assert r.json()["estado"] == "PENDIENTE_PAGO"
    assert httpx.get(f"{server['habitaciones']}/reservas/{rid}").json()["estado"] == "RESERVADA"
    time.sleep(1.8)
    r2 = httpx.post(f"{server['saga']}/reservas/{rid}/reconciliar", timeout=6)
    assert r2.json()["estado"] == "COMPENSADA", r2.text
    assert httpx.get(f"{server['pagos']}/pagos/pay-{rid}").json()["estado"] == "REEMBOLSADO"
    assert httpx.get(f"{server['habitaciones']}/reservas/{rid}").json()["estado"] == "LIBERADA"
