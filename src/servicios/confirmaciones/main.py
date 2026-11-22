"""
Responsabilidades:

- Emitir el voucher al final
- Compensacion: Anular el voucher
- Control de falla: Hacer fallar para probar SAGA
"""

import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Servicio-Confirmacion")
DB_NAME = os.getenv("CONFIRMACIONES_DB", "confirmaciones.db")

caos_config = {
    "forzar_error": False
}


@contextmanager
def get_db(immediate: bool = True):
    conn = sqlite3.connect(
        DB_NAME,
        timeout=10,
        isolation_level=None
    )
    conn.row_factory = sqlite3.Row

    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vouchers (
            id_voucher TEXT PRIMARY KEY,
            id_reserva TEXT NOT NULL,
            estado TEXT NOT NULL, -- 'EMITIDO' o 'ANULADO'
            fecha_emision TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

class ConfirmacionRequest(BaseModel):
    id_voucher: str
    id_reserva: str

class CaosRequest(BaseModel):
    forzar_error: bool = False

@app.post("/chaos")
def configurar_caos(req: CaosRequest):
    caos_config["forzar_error"] = req.forzar_error

    response = {
        "mensaje": "Configuracion de fallas actualizada",
        "estado_actual": caos_config
    }

    return response

@app.post("/confirmaciones", status_code=201)
def emitir_confirmacion(req: ConfirmacionRequest):

    if caos_config["forzar_error"]:
        raise HTTPException(status_code=500, detail="Confirmacion no disponible (Error forzado)")

    with get_db() as conn:
        # Idempotencia
        existente = conn.execute(
            "SELECT * FROM vouchers WHERE id_voucher = ?", (req.id_voucher,)
        ).fetchone()

        if existente:
            if existente["id_reserva"] != req.id_reserva:
                raise HTTPException(status_code=409, detail="Idempotency Key en uso con datos distintos")
            if existente["estado"] != "EMITIDO":
                raise HTTPException(status_code=409, detail="El voucher ya fue anulado y no puede reemitirse")
            return dict(existente)

        fecha_actual = datetime.now(timezone.utc).isoformat()

        # Guardar voucher
        conn.execute("""
            INSERT INTO vouchers (id_voucher, id_reserva, estado, fecha_emision)
            VALUES (?, ?, 'EMITIDO', ?)
            """, (req.id_voucher, req.id_reserva, fecha_actual)
        )

    response = {
        "id_voucher": req.id_voucher,
        "id_reserva": req.id_reserva,
        "estado": 'EMITIDO'
    }

    return response

# COMPENSACION para SAGA
@app.post("/confirmaciones/{id_voucher}/anular")
def anular_confirmacion(id_voucher: str):
    with get_db() as conn:

        voucher = conn.execute(
            "SELECT * FROM vouchers WHERE id_voucher = ?", (id_voucher,)
        ).fetchone()

        if not voucher:
            raise HTTPException(status_code=404, detail="voucher no encontrado")

        conn.execute(
            "UPDATE vouchers SET estado = 'ANULADO' WHERE id_voucher = ?", (id_voucher,)
        )


    response = {
        "id_voucher": id_voucher,
        "estado": 'ANULADO',
        "mensaje": 'Voucher anulado'
    }

    return response

@app.get("/confirmaciones/{id_voucher}")
def obtener_voucher(id_voucher: str):
    with get_db(immediate=False) as conn:
        voucher = conn.execute(
            "SELECT * FROM vouchers WHERE id_voucher = ?", (id_voucher,)
        ).fetchone()

        if not voucher:
            raise HTTPException(status_code=404, detail="voucher no encontrado")

        return dict(voucher)

@app.get("/confirmaciones/reserva/{id_reserva}")
def obtener_por_reserva(id_reserva: str):
    with get_db(immediate=False) as conn:
        voucher = conn.execute("SELECT * FROM vouchers WHERE id_reserva = ? ORDER BY fecha_emision DESC LIMIT 1", (id_reserva,)).fetchone()
        if not voucher:
            raise HTTPException(status_code=404, detail="Confirmacion no encontrada")
        return dict(voucher)

@app.get("/health")
def health():
    return {"status": "ok"}
