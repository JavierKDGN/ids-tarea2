"""Saga orquestada con registro persistente y compensaciones inversas.

Los estados inciertos se
conservan: JAMAS se declara compensado un pago que podria haberse efectuado.
"""
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from src.common.correlacion import corr_actual

log = logging.getLogger("saga")
DB = os.getenv("SAGA_DB", "saga.db")
ROOM = os.getenv("HABITACIONES_URL", "http://127.0.0.1:8001")
PAY = os.getenv("PAGOS_URL", "http://127.0.0.1:8002")
CONF = os.getenv("CONFIRMACION_URL", "http://127.0.0.1:8003")
TIMEOUT = float(os.getenv("SERVICES_TIMEOUT", "2.5"))
_guard = threading.Lock()
_activas = set()

@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

with db() as conn:
    conn.execute("""CREATE TABLE IF NOT EXISTS sagas (
        id_reserva TEXT PRIMARY KEY, clave_cliente TEXT UNIQUE,
        habitacion_id INTEGER NOT NULL, fecha_inicio TEXT NOT NULL,
        fecha_fin TEXT NOT NULL, monto_centavos INTEGER NOT NULL,
        id_pago TEXT NOT NULL, id_voucher TEXT NOT NULL,
        estado TEXT NOT NULL, error TEXT,
        actualizado TEXT NOT NULL
    )""")

class ContratoInvalido(Exception):
    pass

def consultar(id_reserva=None, clave=None):
    with db() as conn:
        if clave:
            fila = conn.execute("SELECT * FROM sagas WHERE clave_cliente=?", (clave,)).fetchone()
        else:
            fila = conn.execute("SELECT * FROM sagas WHERE id_reserva=?", (id_reserva,)).fetchone()
        return dict(fila) if fila else None

def registrar(req, clave):
    datos = {
        "habitacion_id": req.habitacion_id, "fecha_inicio": req.fecha_inicio.isoformat(),
        "fecha_fin": req.fecha_fin.isoformat(), "monto_centavos": int(req.monto * 100)
    }
    with db() as conn:
        original = None
        if clave:
            original = conn.execute("SELECT * FROM sagas WHERE clave_cliente=?", (clave,)).fetchone()
        if not original and req.id_reserva:
            original = conn.execute("SELECT * FROM sagas WHERE id_reserva=?", (req.id_reserva,)).fetchone()
        if original:
            for campo, esperado in datos.items():
                if original[campo] != esperado:
                    raise ContratoInvalido("Idempotency-Key/id_reserva usado con otra solicitud")
            if req.id_reserva and original["id_reserva"] != req.id_reserva:
                raise ContratoInvalido("La clave identifica otra reserva")
            if clave and original["clave_cliente"] not in (None, clave):
                raise ContratoInvalido("Esta reserva tiene otra clave de idempotencia")
            if clave and original["clave_cliente"] is None:
                # Si el cliente incorporo la clave en un reintento del mismo
                # id_reserva, vincularla de forma atomica, no generar otro ID.
                conn.execute("UPDATE sagas SET clave_cliente=? WHERE id_reserva=?", (clave, original["id_reserva"]))
                resultado = dict(original)
                resultado["clave_cliente"] = clave
                return resultado, False
            return dict(original), False
        id_reserva = req.id_reserva or f"res-{uuid.uuid4().hex[:12]}"
        fila = {"id_reserva": id_reserva, "clave_cliente": clave,
                **datos, "id_pago": f"pay-{id_reserva}",
                "id_voucher": f"voucher-{id_reserva}",
                "estado": "INICIADA", "error": None,
                "actualizado": datetime.now(timezone.utc).isoformat()}
        conn.execute("""INSERT INTO sagas
            (id_reserva, clave_cliente, habitacion_id, fecha_inicio, fecha_fin,
             monto_centavos, id_pago, id_voucher, estado, error, actualizado)
            VALUES (:id_reserva, :clave_cliente, :habitacion_id, :fecha_inicio,
                    :fecha_fin, :monto_centavos, :id_pago, :id_voucher, :estado,
                    :error, :actualizado)""", fila)
    return fila, True

def cambiar(reserva, estado, error=None):
    with db() as conn:
        conn.execute("UPDATE sagas SET estado=?, error=?, actualizado=? WHERE id_reserva=?",
                     (estado, error, datetime.now(timezone.utc).isoformat(), reserva))
    log.info("evento=estado_saga reserva=%s estado=%s error=%s corr=%s", reserva, estado, error, corr_actual.get())

def obtener(client, url, corr):
    r = client.get(url, headers={"X-Correlation-ID": corr_actual.get() or corr})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def post(client, url, corr, datos=None):
    return client.post(url, json=datos, headers={"X-Correlation-ID": corr_actual.get() or corr})

def estado_remoto(client, url, corr):
    d = obtener(client, url, corr)
    return d.get("estado") if d else None

def compensar(client, s, motivo):
    """Anular voucher emitido (si existe) -> reembolsar -> liberar.

    Si reembolso falla NO se libera habitacion: el cobro quedaria huerfano.
    Reejecutable: todas las compensaciones del dominio son idempotentes.
    """
    rid = s["id_reserva"]
    cambiar(rid, "COMPENSANDO", motivo)
    try:
        voucher = estado_remoto(client, f"{CONF}/confirmaciones/{s['id_voucher']}", rid)
        if voucher == "EMITIDO":
            r = post(client, f"{CONF}/confirmaciones/{s['id_voucher']}/anular", rid)
            r.raise_for_status()
            if r.json().get("estado") != "ANULADO":
                raise RuntimeError("Anulacion sin confirmacion")
            log.info("evento=compensacion paso=anular_confirmacion reserva=%s", rid)
        pago = estado_remoto(client, f"{PAY}/pagos/{s['id_pago']}", rid)
        if pago == "COBRADO":
            r = post(client, f"{PAY}/pagos/{s['id_pago']}/reembolsar", rid)
            r.raise_for_status()
            if r.json().get("estado") != "REEMBOLSADO":
                raise RuntimeError("Reembolso sin confirmar")
            log.info("evento=compensacion paso=reembolsar reserva=%s", rid)
        elif pago in ("INCIERTO", "EN_PROCESO"):
            cambiar(rid, "PENDIENTE_PAGO", "Cobro incierto: imposible liberar con seguridad")
            return
        elif pago not in (None, "RECHAZADO", "REEMBOLSADO"):
            raise RuntimeError(f"Estado de pago inesperado: {pago}")
        hab = estado_remoto(client, f"{ROOM}/reservas/{rid}", rid)
        if hab == "RESERVADA":
            r = post(client, f"{ROOM}/reservas/{rid}/liberar", rid)
            r.raise_for_status()
            if r.json().get("estado") != "LIBERADA":
                raise RuntimeError("Liberacion sin confirmar")
            log.info("evento=compensacion paso=liberar_habitacion reserva=%s", rid)
        elif hab != "LIBERADA":
            raise RuntimeError("Habitacion no verificable antes de completar compensacion")
        cambiar(rid, "COMPENSADA", motivo)
    except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError, ValueError) as exc:
        log.exception("evento=compensacion_pendiente reserva=%s", rid)
        cambiar(rid, "PENDIENTE_COMPENSACION", str(exc))

def avanzar(s):
    rid = s["id_reserva"]
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            estado = s["estado"]
            if estado in ("CONFIRMADA", "COMPENSADA", "FALLIDA"):
                return
            if estado in ("PENDIENTE_COMPENSACION", "COMPENSANDO"):
                return compensar(client, s, s.get("error") or "Compensacion pendiente")
            if estado == "PENDIENTE_PAGO":
                try:
                    r = post(client, f"{PAY}/pagos/{s['id_pago']}/reconciliar", rid)
                    if r.status_code == 202:
                        return
                    r.raise_for_status()
                    pago = r.json().get("estado")
                    if pago in ("COBRADO", "REEMBOLSADO", "RECHAZADO"):
                        return compensar(client, s, "Pago resuelto despues de respuesta incierta")
                    return
                except httpx.HTTPError as exc:
                    cambiar(rid, "PENDIENTE_PAGO", f"Error al consultar pago: {exc}")
                    return
            if estado == "PENDIENTE_VERIFICACION":
                # Recuperacion conservadora tras un error de comunicacion inesperado.
                try:
                    pago = estado_remoto(client, f"{PAY}/pagos/{s['id_pago']}", rid)
                    voucher = estado_remoto(client, f"{CONF}/confirmaciones/{s['id_voucher']}", rid)
                    if voucher == "EMITIDO" and pago == "COBRADO":
                        cambiar(rid, "CONFIRMADA")
                    elif pago in ("INCIERTO", "EN_PROCESO"):
                        cambiar(rid, "PENDIENTE_PAGO", "Pago necesita conciliacion")
                    elif pago in ("RECHAZADO", "REEMBOLSADO"):
                        compensar(client, s, "Recuperacion de una falla")
                    elif pago == "COBRADO":
                        cambiar(rid, "PENDIENTE_CONFIRMACION", "Resultado de emision no verificado")
                except httpx.HTTPError:
                    pass
                return
            if estado == "PENDIENTE_HABITACION":
                try:
                    hab = estado_remoto(client, f"{ROOM}/reservas/{rid}", rid)
                    if hab != "RESERVADA":
                        return
                except httpx.RequestError:
                    return
                cambiar(rid, "HABITACION_RESERVADA")
                estado = "HABITACION_RESERVADA"
            if estado == "PENDIENTE_CONFIRMACION":
                try:
                    voucher = estado_remoto(client, f"{CONF}/confirmaciones/{s['id_voucher']}", rid)
                except httpx.RequestError:
                    return
                if voucher == "EMITIDO":
                    cambiar(rid, "CONFIRMADA")
                # 404 no prueba que una operacion demorada no se concrete.
                return
            if estado == "INICIADA":
                try:
                    r = post(client, f"{ROOM}/reservas", rid, {
                        "id_reserva": rid, "habitacion_id": s["habitacion_id"],
                        "fecha_inicio": s["fecha_inicio"], "fecha_fin": s["fecha_fin"]})
                    if r.status_code not in (200, 201) or r.json().get("estado") != "RESERVADA":
                        # Si el servidor reporto error despues de guardar, verificar antes
                        # de declarar el fracaso y dejar una reserva huerfana.
                        try:
                            hab = estado_remoto(client, f"{ROOM}/reservas/{rid}", rid)
                            if hab == "RESERVADA":
                                return compensar(client, s, f"Habitaciones: HTTP {r.status_code}")
                            if hab is None:
                                cambiar(rid, "FALLIDA", f"Habitaciones: HTTP {r.status_code}")
                            else:
                                cambiar(rid, "PENDIENTE_VERIFICACION", f"Habitaciones: {hab}")
                        except httpx.HTTPError:
                            cambiar(rid, "PENDIENTE_VERIFICACION", "Respuesta de Habitaciones no verificable")
                        return
                except httpx.RequestError as exc:
                    cambiar(rid, "PENDIENTE_HABITACION", str(exc))
                    return
                cambiar(rid, "HABITACION_RESERVADA")
                estado = "HABITACION_RESERVADA"
            if estado == "HABITACION_RESERVADA":
                try:
                    r = post(client, f"{PAY}/pagos", rid, {
                        "id_pago": s["id_pago"], "id_reserva": rid,
                        "monto": f'{Decimal(s["monto_centavos"]) / 100:.2f}'})
                    # IMPORTANTE: HTTP 202 NO es pago cobrado.
                    if r.status_code == 202 or r.json().get("estado") in ("INCIERTO", "EN_PROCESO"):
                        cambiar(rid, "PENDIENTE_PAGO", "Pago sin confirmacion definitiva")
                        return
                    if r.status_code not in (200, 201) or r.json().get("estado") != "COBRADO":
                        return compensar(client, s, f"Pago fallido HTTP {r.status_code}")
                except (httpx.RequestError, ValueError) as exc:
                    cambiar(rid, "PENDIENTE_PAGO", f"Resultado de pago incierto: {exc}")
                    return
                cambiar(rid, "PAGO_COBRADO")
                estado = "PAGO_COBRADO"
            if estado == "PAGO_COBRADO":
                try:
                    # Consultar siempre el estado real del pago antes de emitir.
                    pago = estado_remoto(client, f"{PAY}/pagos/{s['id_pago']}", rid)
                    if pago != "COBRADO":
                        cambiar(rid, "PENDIENTE_PAGO", "Pago no verificable antes de confirmar")
                        return
                    r = post(client, f"{CONF}/confirmaciones", rid, {
                        "id_reserva": rid, "id_voucher": s["id_voucher"]})
                    if r.status_code in (200, 201) and r.json().get("estado") == "EMITIDO":
                        cambiar(rid, "CONFIRMADA")
                        return
                    # En este laboratorio el error forzado ocurre antes de emitir.
                    return compensar(client, s, f"Confirmacion: HTTP {r.status_code}")
                except (httpx.RequestError, ValueError) as exc:
                    # Una respuesta perdida podria esconder un voucher emitido.
                    try:
                        voucher = estado_remoto(client, f"{CONF}/confirmaciones/{s['id_voucher']}", rid)
                        if voucher == "EMITIDO":
                            cambiar(rid, "CONFIRMADA")
                            return
                    except httpx.RequestError:
                        pass
                    cambiar(rid, "PENDIENTE_CONFIRMACION", f"Emision incierta: {exc}")
                    return
    except Exception as exc:
        log.exception("evento=error_inesperado_saga reserva=%s", rid)
        cambiar(rid, "PENDIENTE_COMPENSACION" if s["estado"] in ("COMPENSANDO", "PENDIENTE_COMPENSACION") else "PENDIENTE_VERIFICACION", str(exc))

ESTADOS_FINALES = ("CONFIRMADA", "COMPENSADA", "FALLIDA")

def pendientes(antiguedad_s):
    limite = (datetime.now(timezone.utc) - timedelta(seconds=antiguedad_s)).isoformat()
    with db() as conn:
        filas = conn.execute(
            "SELECT id_reserva FROM sagas WHERE estado NOT IN (?, ?, ?) AND actualizado < ?",
            (*ESTADOS_FINALES, limite)).fetchall()
    return [f["id_reserva"] for f in filas]

def reconciliar_pendientes(antiguedad_s):
    for rid in pendientes(antiguedad_s):
        if ejecutar(rid):
            log.info("evento=reconciliacion_automatica reserva=%s estado=%s", rid, consultar(id_reserva=rid)["estado"])

def iniciar_reconciliador(intervalo_s):
    if intervalo_s <= 0:
        return None
    detener = threading.Event()

    def bucle():
        while not detener.wait(intervalo_s):
            try:
                reconciliar_pendientes(intervalo_s)
            except Exception:
                log.exception("evento=reconciliacion_error")

    threading.Thread(target=bucle, daemon=True, name="reconciliador").start()
    return detener

def ejecutar(id_reserva):
    with _guard:
        if id_reserva in _activas:
            return False
        _activas.add(id_reserva)
    try:
        s = consultar(id_reserva=id_reserva)
        if s:
            avanzar(s)
        return True
    finally:
        with _guard:
            _activas.remove(id_reserva)
