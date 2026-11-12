"""
Responsabilidades:

- Cobrar la garantia
- Compensacion: Reembolsar
- Control de falla: endpoint para simular latencia/errores (ABET 6)
"""

import sqlite3
import time
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Servicio-Pagos")
DB_NAME = "pagos.db"

caos_config = {
    "latencia_segundos": 0.0,
    "forzar_error": False
}

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row

    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pagos (
                id_pago TEXT PRIMARY KEY,
                id_reserva TEXT NOT NULL,
                monto REAL NOT NULL,
                estado TEXT NOT NULL -- 'COBRADO' o 'REEMBOLSADO'
            )
        """)
        conn.commit()


init_db()

class PagoRequest(BaseModel):
    id_pago: str
    id_reserva: str
    monto: float

class CaosRequest(BaseModel):
    latencia_segundos: float = 0.0
    forzar_error: bool = False

@app.post("/chaos")
def configurar_caos(req: CaosRequest):
    caos_config["latencia_segundos"] = req.latencia_segundos
    caos_config["forzar_error"] = req.forzar_error

    response = {
        "mensaje": "Configuracion de fallas actualizada",
        "estado_actual": caos_config
    }

    return response

@app.post("/pagos", status_code=201)
def cobrar(req: PagoRequest):

    # Simular caos o fallas
    if caos_config["latencia_segundos"] > 0:
        time.sleep(caos_config["latencia_segundos"])

    if caos_config["forzar_error"]:
        raise HTTPException(status_code=500, detail="Pasarela de pagos no disponible (Error forzado)")

    with get_db() as conn:
        # Idempotencia
        existente = conn.execute(
            "SELECT * FROM pagos WHERE id_pago = ?", (req.id_pago,)
        ).fetchone()

        if existente:
            return dict(existente)

        # Guardar pago
        conn.execute("""
            INSERT INTO pagos (id_pago, id_reserva, monto, estado)
            VALUES (?, ?, ?, 'COBRADO')
            """, (req.id_pago, req.id_reserva, req.monto)
        )

        conn.commit()

    response = {
        "id_pago": req.id_pago,
        "id_reserva": req.id_reserva,
        "monto": req.monto,
        "estado": 'COBRADO'
    }

    return response

# COMPENSACION para SAGA
@app.post("pagos/{id_pago}/reembolsar")
def reembolsar(id_pago: str):
    with get_db() as conn:

        pago = conn.execute(
            "SELECT * FROM pagos WHERE id_pago = ?", (id_pago,)
        ).fetchone()

        if not pago:
            raise HTTPException(status_code=404, detail="pago no encontrado")

        conn.execute(
            "UPDATE pagos SET estado = 'REEMBOLSADO' WHERE id_pago = ?", (id_pago,)
        )

        conn.commit()

    response = {
        "id_pago": id_pago,
        "estado": 'REEMBOLSADO',
        "mensaje": 'Garantia reembolsada'
    }

    return response

@app.get("/pagos/{id_pago}")
def obtener_pago(id_pago: str):
    with get_db() as conn:
        pago = conn.execute(
            "SELECT * FROM pagos WHERE id_pago = ?", (id_pago,)
        ).fetchone()

        if not pago:
            raise HTTPException(status_code=404, detail="pago no encontrado")

        return dict(pago)
