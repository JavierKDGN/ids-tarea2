"""
Responsabilidades:

- Reservar habitacion validando que no solapen fechas
- Compensacion: Liberar la habitacion
- Idempotencia: Si llega el mismo id, no duplica
"""

import os
import sqlite3
from datetime import date
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, model_validator

from src.common.correlacion import instalar
from src.common.errores import instalar_errores

app = FastAPI(title="Servicio-Habitaciones")
instalar(app, "habitaciones")
instalar_errores(app)
DB_NAME = os.getenv("HABITACIONES_DB", "habitaciones.db")

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
        CREATE TABLE IF NOT EXISTS reservas (
            id_reserva TEXT PRIMARY KEY,
            habitacion_id INTEGER NOT NULL,
            fecha_inicio TEXT NOT NULL,
            fecha_fin TEXT NOT NULL,
            estado TEXT NOT NULL -- 'RESERVADA' o 'LIBERADA'
        )
    """)
    conn.commit()
    conn.close()


init_db()

class ReservaRequest(BaseModel):
    id_reserva: str
    habitacion_id: int
    fecha_inicio: date # YYYY-MM-DD
    fecha_fin: date # YYYY-MM-DD

    @model_validator(mode="after")
    def validar_fechas(self):
        if self.fecha_inicio >= self.fecha_fin:
            raise ValueError("La fecha fin debe ser posterior al inicio")
        return self


@app.post("/reservas", status_code=201)
def reservar(req: ReservaRequest):

    # para insertar fechas en bd
    inicio_str = req.fecha_inicio.isoformat()
    fin_str = req.fecha_fin.isoformat()

    with get_db() as conn:
        # Idempotencia
        existente = conn.execute(
            "SELECT * FROM reservas WHERE id_reserva = ?", (req.id_reserva,)
        ).fetchone()

        if existente:
            # Si coincide la clave pero con otros parametros
            if (
                existente["habitacion_id"] != req.habitacion_id
                or existente["fecha_inicio"] != inicio_str
                or existente["fecha_fin"] != fin_str
            ):
                raise HTTPException(status_code=409, detail="Idempotency Key en uso con datos distintos")
            if existente["estado"] != "RESERVADA":
                raise HTTPException(status_code=409, detail="La reserva ya fue liberada y no puede reactivarse")
            return dict(existente)

        # Verificar fechas solapadas
        # [A,B] [C,D] se solapa si A < D y B < C 
        solapada = conn.execute("""
            SELECT * FROM reservas
            WHERE habitacion_id = ?
                AND estado = 'RESERVADA'
                AND fecha_inicio < ?
                AND fecha_fin > ?
        """, (req.habitacion_id, fin_str, inicio_str)
        ).fetchone()

        if solapada:
            raise HTTPException(status_code=409, detail="Habitacion no disponible en la fecha")

        # Guardar reserva
        conn.execute("""
            INSERT INTO reservas (id_reserva, habitacion_id, fecha_inicio, fecha_fin, estado)
            VALUES (?, ?, ?, ?, 'RESERVADA')
            """, (req.id_reserva, req.habitacion_id, inicio_str, fin_str,)
        )


    response = {
        "id_reserva": req.id_reserva,
        "habitacion_id": req.habitacion_id,
        "estado": 'RESERVADA'
    }

    return response

# COMPENSACION para SAGA
@app.post("/reservas/{id_reserva}/liberar")
def liberar_reserva(id_reserva: str):
    with get_db() as conn:

        reserva = conn.execute(
            "SELECT * FROM reservas WHERE id_reserva = ?", (id_reserva,)
        ).fetchone()

        if not reserva:
            raise HTTPException(status_code=404, detail="Reserva no encontrada")

        conn.execute(
            "UPDATE reservas SET estado = 'LIBERADA' WHERE id_reserva = ?", (id_reserva,)
        )

    response = {
        "id_reserva": id_reserva,
        "estado": 'LIBERADA',
        "mensaje": 'Compensacion Exitosa'
    }

    return response

@app.get("/reservas/{id_reserva}")
def obtener_reserva(id_reserva: str):
    with get_db(immediate=False) as conn:
        reserva = conn.execute(
            "SELECT * FROM reservas WHERE id_reserva = ?", (id_reserva,)
        ).fetchone()

        if not reserva:
            raise HTTPException(status_code=404, detail="Reserva no encontrada")

        return dict(reserva)

@app.get("/disponibilidad")
def disponibilidad(habitacion_id: int, fecha_inicio: date, fecha_fin: date):
    if fecha_inicio >= fecha_fin:
        raise HTTPException(status_code=422, detail="Intervalo de fechas invalido")
    with get_db(immediate=False) as conn:
        ocupado = conn.execute("""
            SELECT 1 FROM reservas WHERE habitacion_id = ? AND estado = 'RESERVADA'
            AND fecha_inicio < ? AND fecha_fin > ? LIMIT 1
        """, (habitacion_id, fecha_fin.isoformat(), fecha_inicio.isoformat())).fetchone()
    return {"habitacion_id": habitacion_id, "disponible": not bool(ocupado)}

@app.get("/health")
def health():
    return {"status": "ok"}
