"""Integrante B: Pagos con estado persistente, HTTP real hacia la pasarela y resiliencia.

La operacion PENDIENTE/INCIERTO nunca se convierte en 'fallo seguro' por
un simple timeout: la pasarela podria cobrar DESPUES de que expiro la espera.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from src.common.correlacion import instalar
from .resiliencia import RechazoBulkhead, RechazoCircuito, Resiliencia

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pagos")
app = FastAPI(title="Servicio Pagos - resiliencia")
instalar(app, "pagos")
DB = os.getenv("PAGOS_DB", "pagos.db")
PASARELA = os.getenv("PASARELA_URL", "http://127.0.0.1:8004")
TIMEOUT = float(os.getenv("PASARELA_TIMEOUT", "1.0"))
resiliencia = Resiliencia()

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
    conn.execute("""CREATE TABLE IF NOT EXISTS pagos (
        id_pago TEXT PRIMARY KEY, id_reserva TEXT NOT NULL,
        monto_centavos INTEGER NOT NULL, estado TEXT NOT NULL
    )""")

class Pago(BaseModel):
    id_pago: str
    id_reserva: str
    monto: Decimal = Field(gt=0, max_digits=12, decimal_places=2)

def to_centavos(valor: Decimal):
    return int(valor * 100)

def mostrar(fila):
    r = dict(fila)
    r["monto"] = f'{Decimal(r["monto_centavos"]) / 100:.2f}'
    return r

def get_pago(id_pago):
    with db() as conn:
        return conn.execute("SELECT * FROM pagos WHERE id_pago=?", (id_pago,)).fetchone()

def estado(id_pago, nuevo):
    with db() as conn:
        conn.execute("UPDATE pagos SET estado=? WHERE id_pago=?", (nuevo, id_pago))
    log.info("evento=estado_pago pago=%s estado=%s", id_pago, nuevo)

def call_pasarela_post(pago, corr):
    """Solo se protege el cobro, que es la dependencia fragil del ejercicio."""
    prueba = resiliencia.entrar()
    exito = False
    timeout = False
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            respuesta = client.post(
                f"{PASARELA}/cobros", json={
                    "id_pago": pago.id_pago, "id_reserva": pago.id_reserva,
                    "monto_centavos": to_centavos(pago.monto),
                }, headers={"X-Correlation-ID": corr or pago.id_reserva})
        # Rechazo explicito NO_COBRADO permite compensacion segura.
        exito = respuesta.status_code in (200, 201)
        return respuesta
    except httpx.TimeoutException:
        timeout = True
        raise
    finally:
        resiliencia.salir(prueba, ok=exito, timeout=timeout)

@app.post("/pagos", status_code=201)
def cobrar(pago: Pago, x_correlation_id: str | None = Header(default=None)):
    cents = to_centavos(pago.monto)
    with db() as conn:
        anterior = conn.execute("SELECT * FROM pagos WHERE id_pago=?", (pago.id_pago,)).fetchone()
        if anterior:
            if anterior["id_reserva"] != pago.id_reserva or anterior["monto_centavos"] != cents:
                raise HTTPException(409, "Clave de idempotencia reutilizada con otros datos")
            # Sin nuevos efectos. Si una llamada anterior esta en curso, no duplicar.
            if anterior["estado"] != "EN_PROCESO":
                return mostrar(anterior)
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=202, content={**mostrar(anterior), "mensaje": "Cobro en proceso; consultar mas tarde"})
        conn.execute("INSERT INTO pagos VALUES (?, ?, ?, 'EN_PROCESO')", (pago.id_pago, pago.id_reserva, cents))
    try:
        respuesta = call_pasarela_post(pago, x_correlation_id)
        if respuesta.status_code in (200, 201):
            cuerpo = respuesta.json()
            nuevo = cuerpo.get("estado", "INCIERTO")
            if nuevo not in ("COBRADO", "REEMBOLSADO"):
                nuevo = "INCIERTO"
        elif respuesta.status_code == 503 and respuesta.json().get("detail", {}).get("estado") == "NO_COBRADO":
            nuevo = "RECHAZADO"
        else:
            nuevo = "INCIERTO"  # Error externo no garantiza ausencia de efecto.
    except (RechazoCircuito, RechazoBulkhead) as exc:
        nuevo = "RECHAZADO"  # Se rechazo ANTES de contactar a la pasarela.
        log.warning("evento=fallback pago=%s motivo=%s", pago.id_pago, str(exc))
    except httpx.ConnectError:
        nuevo = "RECHAZADO"  # No se establecio la conexion TCP.
        log.warning("evento=pasarela_inaccesible pago=%s", pago.id_pago)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        nuevo = "INCIERTO"  # Podria haberse hecho un cobro remoto.
        log.warning("evento=pago_incierto pago=%s motivo=%s", pago.id_pago, str(exc))
    estado(pago.id_pago, nuevo)
    resultado = mostrar(get_pago(pago.id_pago))
    if nuevo == "COBRADO":
        return resultado
    if nuevo in ("EN_PROCESO", "INCIERTO"):
        # 202 se informa con JSONResponse para que el orquestador no interprete aprobado.
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=202, content=resultado)
    if nuevo == "RECHAZADO":
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={**resultado, "fallback": "Pago no disponible, NO cobrado"})
    return resultado

@app.get("/pagos/{id_pago}")
def consultar_pago(id_pago: str):
    pago = get_pago(id_pago)
    if not pago:
        raise HTTPException(404, "Pago no encontrado")
    return mostrar(pago)

@app.get("/pagos/reserva/{id_reserva}")
def consultar_por_reserva(id_reserva: str):
    with db() as conn:
        pago = conn.execute("SELECT * FROM pagos WHERE id_reserva=? ORDER BY rowid DESC LIMIT 1", (id_reserva,)).fetchone()
    if not pago:
        raise HTTPException(404, "Pago no encontrado")
    return mostrar(pago)

@app.post("/pagos/{id_pago}/reconciliar")
def reconciliar(id_pago: str):
    actual = get_pago(id_pago)
    if not actual:
        raise HTTPException(404, "Pago no encontrado")
    if actual["estado"] in ("RECHAZADO", "REEMBOLSADO"):
        return mostrar(actual)
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(f"{PASARELA}/cobros/{id_pago}")
        if r.status_code == 200:
            nuevo = r.json().get("estado")
            if nuevo in ("COBRADO", "REEMBOLSADO"):
                estado(id_pago, nuevo)
        # 404 NO demuestra que un cobro lento no se ejecutara mas tarde.
    except httpx.RequestError as exc:
        log.warning("evento=consulta_pasarela_fallida pago=%s razon=%s", id_pago, exc)
    actual = get_pago(id_pago)
    if actual["estado"] in ("EN_PROCESO", "INCIERTO"):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=202, content=mostrar(actual))
    return mostrar(actual)

@app.post("/pagos/{id_pago}/reembolsar")
def reembolsar(id_pago: str, x_correlation_id: str | None = Header(default=None)):
    actual = get_pago(id_pago)
    if not actual:
        raise HTTPException(404, "Pago no encontrado")
    if actual["estado"] == "REEMBOLSADO":
        return mostrar(actual)
    if actual["estado"] != "COBRADO":
        raise HTTPException(409, f"Reembolso inseguro; estado {actual['estado']}")
    # Reembolsos se tratan fuera del CB de cobros para no bloquear compensaciones
    # criticas por circuito abierto. Conservan timeout e idempotencia en pasarela.
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            respuesta = client.post(f"{PASARELA}/cobros/{id_pago}/reembolsar",
                                    headers={"X-Correlation-ID": x_correlation_id or actual["id_reserva"]})
        respuesta.raise_for_status()
        if respuesta.json().get("estado") != "REEMBOLSADO":
            raise HTTPException(502, "La pasarela no confirmo reembolso")
    except httpx.RequestError as exc:
        log.error("evento=reembolso_pendiente pago=%s razon=%s", id_pago, exc)
        raise HTTPException(503, "Reembolso pendiente; reintentar la compensacion")
    estado(id_pago, "REEMBOLSADO")
    return mostrar(get_pago(id_pago))

@app.get("/metricas")
def metricas():
    return resiliencia.snapshot()

@app.post("/demo/reset-resiliencia")
def resetear():
    if resiliencia.snapshot()["bulkhead_en_uso"]:
        raise HTTPException(409, "No resetear con solicitudes en vuelo")
    resiliencia.reiniciar()
    return resiliencia.snapshot()

@app.get("/health")
def health():
    return {"status": "ok"}
