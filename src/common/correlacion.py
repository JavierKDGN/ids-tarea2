import logging
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

corr_actual: ContextVar[str | None] = ContextVar("corr_actual", default=None)


def instalar(app: FastAPI, servicio: str):
    log = logging.getLogger(f"{servicio}.http")

    @app.middleware("http")
    async def correlacion(request: Request, call_next):
        corr = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        token = corr_actual.set(corr)
        inicio = time.perf_counter()
        try:
            respuesta = await call_next(request)
        finally:
            corr_actual.reset(token)
        respuesta.headers["X-Correlation-ID"] = corr
        if request.url.path != "/health":
            log.info("evento=request servicio=%s metodo=%s ruta=%s estado=%s ms=%.0f corr=%s",
                     servicio, request.method, request.url.path, respuesta.status_code,
                     (time.perf_counter() - inicio) * 1000, corr)
        return respuesta
