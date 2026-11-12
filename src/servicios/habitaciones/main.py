"""
Responsabilidades:

- Reservar habitacion validando que no solapen fechas
- Compensacion: Liberar la habitacion
- Idempotencia: Si llega el mismo id, no duplica
"""

import sqlite3
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Servicio-Habitaciones")
DB_NAME = "habitaciones.db"

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
            CREATE TABLE IF NOT EXISTS reservas (
                id_reserva TEXT PRIMARY KEY,
                habitacion_id INTEGER NOT NULL,
                fecha_inicio TEXT NOT NULL,
                fecha_fin TEXT NOT NULL,
                estado TEXT NOT NULL -- 'RESERVADA' o 'LIBERADA'
            )
        """)
        conn.commit()


init_db()

class ReservaRequest(BaseModel):
    id_reserva: str
    habitacion_id: int
    fecha_inicio: str # YYYY-MM-DD
    fecha_fin: str # YYYY-MM-DD


@app.post("/reservas", status_code=201)
def reservar(req: ReservaRequest):
    with get_db() as conn:
        # Idempotencia
        existente = conn.execute(
            "SELECT * FROM reservas WHERE id_reserva = ?", (req.id_reserva,)
        ).fetchone()

        if existente:
            return dict(existente)

        # Verificar fechas solapadas
        # [A,B] [C,D] se solapa si A < D y B < C 
        solapada = conn.execute("""
            SELECT * FROM reservas
            WHERE habitacion_id = ?
                AND estado = 'RESERVADA'
                AND fecha_inicio < ?
                AND fecha_fin > ?
        """, (req.habitacion_id, req.fecha_fin, req.fecha_inicio)
        ).fetchone()

        if solapada:
            raise HTTPException(status_code=409, detail="Habitacion no disponible en la fecha")

        # Guardar reserva
        conn.execute("""
            INSERT INTO reservas (id_reserva, habitacion_id, fecha_inicio, fecha_fin, estado)
            VALUES (?, ?, ?, ?, 'RESERVADA')
            """, (req.id_reserva, req.habitacion_id, req.fecha_inicio, req.fecha_fin,)
        )

        conn.commit()

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

        conn.commit()

    response = {
        "id_reserva": id_reserva,
        "estado": 'LIBERADA',
        "mensaje": 'Compensacion Exitosa'
    }

    return response

@app.get("/reservas/{id_reserva}")
def obtener_reserva(id_reserva: str):
    with get_db() as conn:
        reserva = conn.execute(
            "SELECT * FROM reservas WHERE id_reserva = ?", (id_reserva,)
        ).fetchone()

        if not reserva:
            raise HTTPException(status_code=404, detail="Reserva no encontrada")

        return dict(reserva)
