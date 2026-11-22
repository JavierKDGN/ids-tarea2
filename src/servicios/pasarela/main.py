"""Pasarela EXTERNA simulada  (se ejecuta en puerto 8004).

Almacena el estado de la transacción desde la perspectiva del proveedor externo

La demora ocurre ANTES de registrar el cobro, para poder reproducir el caso
peligroso: el cliente agota su timeout pero el servidor cobra despues.
"""
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("pasarela")
app = FastAPI(title="Pasarela-externa")
DB = os.getenv("PASARELA_DB", "pasarela.db")
lock = threading.Lock()
chaos = {"latencia_segundos": 0.0, "forzar_error": False}

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
    conn.execute("""CREATE TABLE IF NOT EXISTS cobros (
        id_pago TEXT PRIMARY KEY, id_reserva TEXT NOT NULL,
        monto_centavos INTEGER NOT NULL, estado TEXT NOT NULL
    )""")

class Cobro(BaseModel):
    id_pago: str
    id_reserva: str
    monto_centavos: int = Field(gt=0)

class Caos(BaseModel):
    latencia_segundos: float = Field(default=0, ge=0, le=30)
    forzar_error: bool = False

@app.post("/chaos")
def configurar(c: Caos):
    with lock:
        chaos.update(c.model_dump())
        resultado = dict(chaos)
    logger.info("evento=chaos configuracion=%s", resultado)
    return resultado

@app.post("/cobros", status_code=201)
def cobrar(c: Cobro, x_correlation_id: str | None = Header(default=None)):
    with lock:
        delay, error = chaos["latencia_segundos"], chaos["forzar_error"]
    if delay:
        time.sleep(delay)
    if error:
        logger.warning("evento=pasarela_caida pago=%s corr=%s", c.id_pago, x_correlation_id)
        # Respuesta explicita: no se efectuo el cobro.
        raise HTTPException(status_code=503, detail={"estado": "NO_COBRADO", "razon": "Pasarela caida (simulada)"})
    with db() as conn:
        antiguo = conn.execute("SELECT * FROM cobros WHERE id_pago=?", (c.id_pago,)).fetchone()
        if antiguo:
            if antiguo["id_reserva"] != c.id_reserva or antiguo["monto_centavos"] != c.monto_centavos:
                raise HTTPException(status_code=409, detail="Clave usada con otros datos")
            return dict(antiguo)
        conn.execute("INSERT INTO cobros VALUES (?, ?, ?, 'COBRADO')", (c.id_pago, c.id_reserva, c.monto_centavos))
    logger.info("evento=cobro_realizado pago=%s reserva=%s corr=%s", c.id_pago, c.id_reserva, x_correlation_id)
    return {"id_pago": c.id_pago, "id_reserva": c.id_reserva, "monto_centavos": c.monto_centavos, "estado": "COBRADO"}

@app.get("/cobros/{id_pago}")
def consultar(id_pago: str):
    with db() as conn:
        registro = conn.execute("SELECT * FROM cobros WHERE id_pago=?", (id_pago,)).fetchone()
        if not registro:
            raise HTTPException(status_code=404, detail="Cobro aun no encontrado (NO prueba ausencia definitiva)")
        return dict(registro)

@app.post("/cobros/{id_pago}/reembolsar")
def reembolsar(id_pago: str, x_correlation_id: str | None = Header(default=None)):
    with db() as conn:
        registro = conn.execute("SELECT * FROM cobros WHERE id_pago=?", (id_pago,)).fetchone()
        if not registro:
            raise HTTPException(status_code=404, detail="Cobro no encontrado")
        conn.execute("UPDATE cobros SET estado='REEMBOLSADO' WHERE id_pago=?", (id_pago,))
    logger.info("evento=reembolso_realizado pago=%s corr=%s", id_pago, x_correlation_id)
    return {"id_pago": id_pago, "estado": "REEMBOLSADO"}

@app.get("/health")
def health():
    return {"status": "ok"}
