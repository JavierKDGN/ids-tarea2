import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bff")
app = FastAPI(title="BFF-web")

SAGA_URL = os.getenv("SAGA_URL", "http://127.0.0.1:8000")
HABITACIONES_URL = os.getenv("HABITACIONES_URL", "http://127.0.0.1:8001")
CONFIRMACION_URL = os.getenv("CONFIRMACION_URL", "http://127.0.0.1:8003")
TIMEOUT = float(os.getenv("BFF_TIMEOUT", "2.0"))

ESTADOS_SAGA = (200, 202, 409, 502)

MENSAJES = {
    "CONFIRMADA": "Reserva confirmada",
    "COMPENSADA": "La reserva no pudo completarse y fue revertida",
    "FALLIDA": "La reserva no pudo realizarse",
}
MENSAJE_EN_CURSO = "La reserva esta en proceso, consulte nuevamente en unos instantes"


async def consultar(client, url, corr, estados_ok=(200,)):
    try:
        r = await client.get(url, headers={"X-Correlation-ID": corr})
    except httpx.HTTPError:
        return "NO_DISPONIBLE", None
    if r.status_code in estados_ok:
        try:
            return "OK", r.json()
        except ValueError:
            return "NO_DISPONIBLE", None
    if r.status_code == 404:
        return "NO_EXISTE", None
    return "NO_DISPONIBLE", None


def vista_habitacion(h):
    if not h:
        return None
    return {"habitacion_id": h["habitacion_id"], "fecha_inicio": h["fecha_inicio"],
            "fecha_fin": h["fecha_fin"], "estado": h["estado"]}


def vista_confirmacion(c):
    if not c:
        return None
    return {"id_voucher": c["id_voucher"], "estado": c["estado"],
            "fecha_emision": c.get("fecha_emision")}


@app.get("/api/reservas/{id_reserva}/resumen")
async def resumen(id_reserva: str, x_correlation_id: str | None = Header(default=None)):
    corr = x_correlation_id or uuid.uuid4().hex
    headers = {"X-Correlation-ID": corr}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        (f_saga, saga), (f_hab, hab), (f_conf, conf) = await asyncio.gather(
            consultar(client, f"{SAGA_URL}/reservas/{id_reserva}", corr, ESTADOS_SAGA),
            consultar(client, f"{HABITACIONES_URL}/reservas/{id_reserva}", corr),
            consultar(client, f"{CONFIRMACION_URL}/confirmaciones/reserva/{id_reserva}", corr),
        )

    if f_saga == "NO_EXISTE":
        log.info("evento=resumen_no_encontrado reserva=%s corr=%s", id_reserva, corr)
        return JSONResponse(status_code=404, headers=headers, content={
            "error": "no_encontrado", "code": "RECURSO_NO_ENCONTRADO", "message": "Reserva no encontrada"})
    if f_saga != "OK":
        log.warning("evento=resumen_saga_no_disponible reserva=%s corr=%s", id_reserva, corr)
        return JSONResponse(status_code=503, headers=headers, content={
            "error": "no_disponible", "code": "SAGA_NO_DISPONIBLE",
            "message": "No se pudo consultar el estado de la reserva"})

    fuentes = {"saga": f_saga, "habitacion": f_hab, "confirmacion": f_conf}
    degradado = "NO_DISPONIBLE" in fuentes.values()
    log.info("evento=resumen reserva=%s estado=%s degradado=%s corr=%s",
             id_reserva, saga["estado"], degradado, corr)
    return JSONResponse(headers=headers, content={
        "id_reserva": id_reserva,
        "mensaje": MENSAJES.get(saga["estado"], MENSAJE_EN_CURSO),
        "degradado": degradado,
        "saga": {"estado": saga["estado"], "error": saga.get("error"), "actualizado": saga["actualizado"]},
        "habitacion": vista_habitacion(hab),
        "confirmacion": vista_confirmacion(conf),
        "fuentes": fuentes,
        "generado": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/health")
def health():
    return {"status": "ok"}
