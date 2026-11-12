"""
    Operacion SAGA
    1. Llamar a habitaciones
    2. Llamar a Pagos (Timeout + Bulkhead + Circuit Breaker)
    3. Confirmacion

    BFF todo en una request
"""

import os
import uuid
import logging
import httpx
from datetime import date
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from .saga import ejecutar_compensacion

# Config logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("BFF-Orquestador")

app = FastAPI(title="Servicio-BFF")

# URLs entorno
HABITACIONES_URL = os.getenv("HABITACIONES_URL", "http://localhost:8001")
PAGOS_URL = os.getenv("PAGOS_URL", "http://localhost:8002")
CONFIRMACION_URL = os.getenv("CONFIRMACION_URL", "http://localhost:8003")

class SolicitarReservaRequest(BaseModel):
    habitacion_id: int
    fecha_inicio: date
    fecha_fin: date
    monto: float = Field(gt=0)
    id_reserva: str | None = None

@app.post("/api/reservar", status_code=201)
def reservar(req: SolicitarReservaRequest):
    # TODO: FUNCION EJECUTAR COMPENSACION 
    # ID unico de reserva para todo SAGA, si no existe se crea
    id_transaccion = req.id_reserva or f"res-{uuid.uuid4().hex[:8]}"
    id_pago = f"pay-{id_transaccion}"
    id_voucher = f"voucher-{id_transaccion}"

    flag_habitacion_reservada = False
    flag_pago_realizado = False

    with httpx.Client(timeout=5.0) as client:

        # --------------------------------------
        # 1: Reservar Habitacion
        # --------------------------------------
        try:
            response_hab = client.post(
                f"{HABITACIONES_URL}/reservas",
                json = {
                    "id_reserva": id_transaccion,
                    "habitacion_id": req.habitacion_id,
                    "fecha_inicio": req.fecha_inicio,
                    "fecha_fin": req.fecha_fin
                }
            )

            if response_hab.status_code not in (200, 201):
                raise HTTPException(status_code=response_hab.status_code, detail=f"Fallo en habitaciones: {response_hab.text}")

            flag_habitacion_reservada = True
        
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"No se pudo reservar la habitacion")

        # --------------------------------------
        # 2: Cobrar pago
        # --------------------------------------
        try:
            response_pago = client.post(
                f"{PAGOS_URL}/pagos",
                json = {
                    "id_reserva": id_transaccion,
                    "id_pago": id_pago,
                    "monto": req.monto
                }
            )

            if response_pago.status_code not in (200, 201):
                ejecutar_compensacion()
                raise HTTPException(status_code=response_pago.status_code, detail=f"Fallo en pago")

            flag_pago_realizado= True

        except HTTPException:
            raise
        except Exception as e:
            # Error distinto al anterior
            ejecutar_compensacion()
            raise HTTPException(status_code=500, detail=f"Error en servicio de pagos")

        # --------------------------------------
        # 3: Emitir confirmacion
        # --------------------------------------
        try:
            response_conf = client.post(
                f"{CONFIRMACION_URL}/confirmaciones",
                json = {
                    "id_reserva": id_transaccion,
                    "id_voucher": id_voucher
                }
            )

            if response_conf.status_code not in (200, 201):
                ejecutar_compensacion()
                raise HTTPException(status_code=response_conf.status_code, detail=f"Fallo en confirmacion")

            flag_pago_realizado= True

        except HTTPException:
            raise
        except Exception as e:
            # Error distinto al anterior
            ejecutar_compensacion()
            raise HTTPException(status_code=response_conf.status_code, detail=f"Fallo en confirmacion. Se revirtieron los cargos.")

        response = {
            "mensaje": "Reserva completada exitosamente",
            "id_reserva": id_transaccion,
            "id_pago": id_pago,
            "id_voucher": id_voucher,
            "estado": "CONFIRMADA"
        }

        return response 


