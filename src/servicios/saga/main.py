"""API publica temporal del orquestador (sin Gateway/BFF aun)."""
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from src.common.correlacion import instalar
from src.common.errores import instalar_errores
from .logic import ContratoInvalido, conteo_por_estado, consultar, ejecutar, iniciar_reconciliador, registrar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
INTERVALO_RECONCILIACION_S = float(os.getenv("RECONCILIACION_SEGUNDOS", "0"))


@asynccontextmanager
async def ciclo_de_vida(app):
    detener = iniciar_reconciliador(INTERVALO_RECONCILIACION_S)
    yield
    if detener:
        detener.set()


app = FastAPI(title="Saga-orquestada", lifespan=ciclo_de_vida)
instalar(app, "saga")
instalar_errores(app)

class Reserva(BaseModel):
    habitacion_id: int = Field(gt=0)
    fecha_inicio: date
    fecha_fin: date
    monto: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    id_reserva: str | None = None

    @model_validator(mode="after")
    def validar(self):
        if self.fecha_inicio >= self.fecha_fin:
            raise ValueError("fecha_fin debe ser posterior a fecha_inicio")
        if self.id_reserva and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.id_reserva):
            raise ValueError("id_reserva debe contener solo letras, numeros, guion o guion bajo")
        return self

def respuesta(s, creado=False, consulta=False):
    estados = {"CONFIRMADA": 201 if creado else 200, "COMPENSADA": 502,
               "FALLIDA": 409}
    http = 200 if consulta else estados.get(s["estado"], 202)
    return JSONResponse(status_code=http, content={
        "id_reserva": s["id_reserva"], "id_pago": s["id_pago"],
        "id_voucher": s["id_voucher"], "estado": s["estado"],
        "error": s["error"], "actualizado": s["actualizado"]})

@app.post("/reservas")
def reservar(req: Reserva, idempotency_key: str | None = Header(default=None)):
    if idempotency_key and (len(idempotency_key) > 128 or not idempotency_key.strip()):
        raise HTTPException(422, "Idempotency-Key invalida")
    try:
        s, creado = registrar(req, idempotency_key)
    except ContratoInvalido as exc:
        raise HTTPException(409, str(exc))
    ejecutar(s["id_reserva"])
    return respuesta(consultar(id_reserva=s["id_reserva"]), creado)

@app.get("/reservas/{id_reserva}")
def obtener(id_reserva: str):
    s = consultar(id_reserva=id_reserva)
    if not s:
        raise HTTPException(404, "Reserva no encontrada")
    return respuesta(s, consulta=True)

@app.post("/reservas/{id_reserva}/reconciliar")
def reconciliar(id_reserva: str):
    s = consultar(id_reserva=id_reserva)
    if not s:
        raise HTTPException(404, "Reserva no encontrada")
    ejecutar(id_reserva)
    return respuesta(consultar(id_reserva=id_reserva))

@app.get("/metricas")
def metricas():
    conteo = conteo_por_estado()
    return {"total_reservas": sum(conteo.values()), "reservas_por_estado": conteo}

@app.get("/health")
def health():
    return {"status": "ok"}
