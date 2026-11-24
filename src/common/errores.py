import logging
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("errores")

PATRON_CODIGO = re.compile(r"^([A-Z][A-Z_]{3,}): (.*)$", re.S)

CODIGOS_409 = (
    ("idempotenc", "IDEMPOTENCY_CONFLICT"),
    ("otra solicitud", "IDEMPOTENCY_CONFLICT"),
    ("otra reserva", "IDEMPOTENCY_CONFLICT"),
    ("ya fue liberada", "RESERVA_YA_LIBERADA"),
    ("no disponible", "HABITACION_NO_DISPONIBLE"),
    ("ya fue anulado", "VOUCHER_YA_ANULADO"),
    ("reembolso inseguro", "REEMBOLSO_INSEGURO"),
    ("no resetear", "RESET_NO_PERMITIDO"),
)

CODIGOS_TEXTO = (
    ("reembolso pendiente", "REEMBOLSO_PENDIENTE"),
    ("confirmacion no disponible", "CONFIRMACION_NO_DISPONIBLE"),
)


def clasificar(status, mensaje):
    claro = mensaje.lower()
    coincide = PATRON_CODIGO.match(mensaje)
    if coincide:
        categoria = "no_disponible" if status >= 500 else "conflicto"
        return categoria, coincide.group(1), coincide.group(2)
    if status == 404:
        return "no_encontrado", "RECURSO_NO_ENCONTRADO", mensaje
    if status == 422:
        return "validacion", "VALIDACION_FALLIDA", mensaje
    if status == 409:
        for fragmento, codigo in CODIGOS_409:
            if fragmento in claro:
                return "conflicto", codigo, mensaje
        return "conflicto", "CONFLICTO", mensaje
    for fragmento, codigo in CODIGOS_TEXTO:
        if fragmento in claro:
            return ("no_disponible" if status == 503 else "error_interno"), codigo, mensaje
    if status == 503:
        return "no_disponible", "SERVICIO_NO_DISPONIBLE", mensaje
    return "error_interno", "ERROR_INTERNO", mensaje


def respuesta(status, mensaje):
    error, codigo, texto = clasificar(status, mensaje)
    return JSONResponse(status_code=status, content={"error": error, "code": codigo, "message": texto})


def instalar_errores(app: FastAPI):
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        mensaje = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return respuesta(exc.status_code, mensaje)

    @app.exception_handler(RequestValidationError)
    async def validacion(request: Request, exc: RequestValidationError):
        primero = exc.errors()[0] if exc.errors() else {}
        campo = ".".join(str(p) for p in primero.get("loc", ()) if p != "body")
        mensaje = f"{campo}: {primero.get('msg', 'solicitud invalida')}" if campo else primero.get("msg", "solicitud invalida")
        return respuesta(422, mensaje)

    @app.exception_handler(Exception)
    async def inesperado(request: Request, exc: Exception):
        log.exception("evento=error_inesperado ruta=%s", request.url.path)
        return respuesta(500, "Error interno")
