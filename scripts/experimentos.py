"""Experimentos reproducibles. Ejecutar con los cinco procesos activos.

Uso: python scripts/experimentos.py
Guarda JSON para el informe en evidence/resultados.json.
"""
import concurrent.futures
import json
import time
import uuid
from pathlib import Path

import httpx

ROOT = "http://127.0.0.1:"
SAGA, HAB, PAG, CON, PAS = (ROOT + str(p) for p in (8000, 8001, 8002, 8003, 8004))
reporte = {}

def peticion(method, url, **kwargs):
    with httpx.Client(timeout=8) as c:
        r = c.request(method, url, **kwargs)
    try:
        cuerpo = r.json()
    except ValueError:
        cuerpo = {"respuesta": r.text}
    return {"http": r.status_code, "datos": cuerpo}

def reserva(rid, habitacion):
    return peticion("POST", f"{SAGA}/reservas", json={
        "id_reserva": rid, "habitacion_id": habitacion,
        "fecha_inicio": "2027-01-10", "fecha_fin": "2027-01-13", "monto": "80.00"},
        headers={"Idempotency-Key": rid})

def limpiar():
    peticion("POST", f"{CON}/chaos", json={"forzar_error": False})
    peticion("POST", f"{PAS}/chaos", json={"forzar_error": False, "latencia_segundos": 0})
    peticion("POST", f"{PAG}/demo/reset-resiliencia")

def tarea_pago(i, prefijo):
    rid = f"{prefijo}-{i}"
    inicio = time.perf_counter()
    r = peticion("POST", f"{PAG}/pagos", json={
        "id_pago": f"pay-{rid}", "id_reserva": rid, "monto": "80.00"})
    return {"id_pago": f"pay-{rid}", "duracion_s": round(time.perf_counter() - inicio, 3), **r}

def run():
    limpiar()
    prefijo = uuid.uuid4().hex[:8]
    habitacion_base = 10000 + int(prefijo, 16)
    print("ESCENARIO 1 - Reserva exitosa")
    rid = f"feliz-{prefijo}"
    creado = reserva(rid, habitacion_base)
    reporte["camino_feliz"] = {
        "respuesta": creado,
        "habitacion": peticion("GET", f"{HAB}/reservas/{rid}"),
        "pago": peticion("GET", f"{PAG}/pagos/pay-{rid}"),
        "confirmacion": peticion("GET", f"{CON}/confirmaciones/voucher-{rid}"),
        "reintento_idempotente": reserva(rid, habitacion_base),
    }
    print(" Estado:", creado["datos"]["estado"])
    assert creado["datos"]["estado"] == "CONFIRMADA"

    print("ESCENARIO 2 - Confirmacion falla y Saga revierte")
    peticion("POST", f"{CON}/chaos", json={"forzar_error": True})
    rid = f"comp-{prefijo}"
    try:
        creado = reserva(rid, habitacion_base + 1)
    finally:
        peticion("POST", f"{CON}/chaos", json={"forzar_error": False})
    reporte["compensacion"] = {
        "respuesta": creado,
        "habitacion": peticion("GET", f"{HAB}/reservas/{rid}"),
        "pago": peticion("GET", f"{PAG}/pagos/pay-{rid}"),
        "confirmacion": peticion("GET", f"{CON}/confirmaciones/voucher-{rid}"),
    }
    print(" Estado:", creado["datos"]["estado"])
    assert creado["datos"]["estado"] == "COMPENSADA"
    assert reporte["compensacion"]["pago"]["datos"]["estado"] == "REEMBOLSADO"
    assert reporte["compensacion"]["habitacion"]["datos"]["estado"] == "LIBERADA"

    print("ESCENARIO 3a - Lentitud, timeout y Bulkhead bajo concurrencia")
    peticion("POST", f"{PAG}/demo/reset-resiliencia")
    peticion("POST", f"{PAS}/chaos", json={"latencia_segundos": 2, "forzar_error": False})
    pref = f"carga-{prefijo}"
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        resultados = list(pool.map(lambda i: tarea_pago(i, pref), range(12)))
    # El circuito abre al acumular timeouts. Medir fail-fast al solicitar una vez mas.
    inicio = time.perf_counter()
    rechazo_rapido = tarea_pago(100, pref)
    latencia_failfast = round(time.perf_counter() - inicio, 3)
    metricas = peticion("GET", f"{PAG}/metricas")
    salud = {"habitaciones": peticion("GET", f"{HAB}/health"),
             "confirmaciones": peticion("GET", f"{CON}/health")}
    reporte["lentitud_bulkhead"] = {"peticiones": resultados,
        "rechazo_rapido": rechazo_rapido, "latencia_failfast_s": latencia_failfast,
        "metricas": metricas, "servicios_sanos": salud}
    print(" Metricas:", metricas["datos"])
    assert metricas["datos"]["rechazos_bulkhead"] > 0
    assert metricas["datos"]["timeouts"] > 0

    # Conciliacion de cobros terminados despues del timeout; no dejarlos cobrados.
    time.sleep(2.2)
    cleanup_cobros = []
    for r in resultados:
        pago_id = r["id_pago"]
        s = peticion("POST", f"{PAG}/pagos/{pago_id}/reconciliar")
        if s["datos"].get("estado") == "COBRADO":
            s = peticion("POST", f"{PAG}/pagos/{pago_id}/reembolsar")
        cleanup_cobros.append(s)
    reporte["lentitud_bulkhead"]["limpieza_pagos"] = cleanup_cobros

    print("ESCENARIO 3b - Pasarela caida y Circuit Breaker")
    peticion("POST", f"{PAS}/chaos", json={"latencia_segundos": 0, "forzar_error": True})
    peticion("POST", f"{PAG}/demo/reset-resiliencia")
    fallas = [tarea_pago(i, f"caida-{prefijo}") for i in range(3)]
    metricas = peticion("GET", f"{PAG}/metricas")
    reporte["caida_circuit_breaker"] = {"peticiones": fallas, "metricas": metricas}
    print(" Metricas:", metricas["datos"])
    assert metricas["datos"]["circuit_breaker"] == "OPEN"
    assert metricas["datos"]["rechazos_circuito"] >= 1

    print("ESCENARIO 3c - Recuperacion HALF_OPEN -> CLOSED")
    peticion("POST", f"{PAS}/chaos", json={"latencia_segundos": 0, "forzar_error": False})
    # Pasar el periodo de enfriamiento de CB_RECOVERY_SECONDS=4.
    time.sleep(4.2)
    exito_recuperado = tarea_pago(1, f"recuperacion-{prefijo}")
    metricas_recuperadas = peticion("GET", f"{PAG}/metricas")
    reporte["recuperacion_circuit_breaker"] = {
        "peticion": exito_recuperado, "metricas": metricas_recuperadas}
    print(" Estado recuperado:", metricas_recuperadas["datos"]["circuit_breaker"])
    assert metricas_recuperadas["datos"]["circuit_breaker"] == "CLOSED"

    print("ESCENARIO EXTRA - Timeout incierto de Saga y conciliacion")
    peticion("POST", f"{PAS}/chaos", json={"latencia_segundos": 2, "forzar_error": False})
    peticion("POST", f"{PAG}/demo/reset-resiliencia")
    rid = f"tarde-{prefijo}"
    original = reserva(rid, habitacion_base + 2)
    time.sleep(2.2)
    conciliado = peticion("POST", f"{SAGA}/reservas/{rid}/reconciliar")
    reporte["timeout_incierto"] = {
        "antes": original, "despues": conciliado,
        "pago": peticion("GET", f"{PAG}/pagos/pay-{rid}"),
        "habitacion": peticion("GET", f"{HAB}/reservas/{rid}")}
    print(" Estados:", original["datos"]["estado"], "->", conciliado["datos"]["estado"])
    assert original["datos"]["estado"] == "PENDIENTE_PAGO"
    assert conciliado["datos"]["estado"] == "COMPENSADA"
    limpiar()
    ruta = Path("evidence/resultados.json")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK. Resultados guardados en {ruta}")

if __name__ == "__main__":
    try:
        run()
    finally:
        limpiar()
