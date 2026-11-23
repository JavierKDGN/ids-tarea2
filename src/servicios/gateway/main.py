"""API Gateway: unica puerta de entrada. NO tiene logica de negocio.

Solo hace cuatro cosas (ver contrato-api-v1/contrato/gateway.yaml):
  1. autentica (Bearer token),
  2. limita por cliente (rate limiting, 429),
  3. enruta al servicio interno que corresponde,
  4. aplica timeout y propaga X-Correlation-ID.
Los cuerpos y codigos de negocio del servicio destino se reenvian sin modificar.
"""
import hmac
import logging
import math
import os
import re
import time
import uuid
from collections import deque

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("gateway")
app = FastAPI(title="API-Gateway")

SAGA_URL = os.getenv("SAGA_URL", "http://127.0.0.1:8000")
BFF_URL = os.getenv("BFF_URL", "http://127.0.0.1:8005")
HABITACIONES_URL = os.getenv("HABITACIONES_URL", "http://127.0.0.1:8001")
LIMITE = max(1, int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")))
VENTANA_S = 60.0
TIMEOUT_ESCRITURA = float(os.getenv("GATEWAY_TIMEOUT_POST", "10"))
TIMEOUT_LECTURA = float(os.getenv("GATEWAY_TIMEOUT_GET", "4"))


def cargar_tokens():
    """GATEWAY_TOKENS='cliente:token,cliente2:token2' -> {token: cliente}."""
    crudo = os.getenv("GATEWAY_TOKENS", "demo:demo-token")
    tokens = {}
    for par in crudo.split(","):
        cliente, _, token = par.strip().partition(":")
        if cliente and token:
            tokens[token] = cliente
    return tokens


TOKENS = cargar_tokens()

# (metodo, patron del path publico, base del destino, path interno, timeout)
RUTAS = [
    ("POST", r"/api/reservas", SAGA_URL, "/reservas", TIMEOUT_ESCRITURA),
    ("GET", r"/api/reservas/(?P<id>[^/]+)", SAGA_URL, "/reservas/{id}", TIMEOUT_LECTURA),
    ("POST", r"/api/reservas/(?P<id>[^/]+)/reconciliar", SAGA_URL, "/reservas/{id}/reconciliar", TIMEOUT_ESCRITURA),
    ("GET", r"/api/reservas/(?P<id>[^/]+)/resumen", BFF_URL, "/api/reservas/{id}/resumen", TIMEOUT_LECTURA),
    ("GET", r"/api/habitaciones/disponibilidad", HABITACIONES_URL, "/disponibilidad", TIMEOUT_LECTURA),
]
RUTAS = [(m, re.compile(p), base, destino, t) for m, p, base, destino, t in RUTAS]

HEADERS_REENVIADOS = ("content-type", "idempotency-key")
_ventanas: dict[str, deque] = {}


def error(status, code, error_, mensaje, headers=None):
    return JSONResponse(status_code=status, headers=headers,
                        content={"error": error_, "code": code, "message": mensaje})


def cliente_de(request: Request):
    """Devuelve (clave_de_cliente, autenticado). Sin token valido la clave es la IP."""
    auth = request.headers.get("authorization", "")
    esquema, _, token = auth.partition(" ")
    if esquema.lower() == "bearer" and token:
        for valido, cliente in TOKENS.items():
            if hmac.compare_digest(token, valido):
                return cliente, True
    return f"ip:{request.client.host if request.client else 'desconocida'}", False


def consumir_cupo(clave):
    """Ventana deslizante de 60 s. Devuelve (permitido, restantes, segundos_para_reintentar)."""
    ahora = time.monotonic()
    ventana = _ventanas.setdefault(clave, deque())
    while ventana and ahora - ventana[0] >= VENTANA_S:
        ventana.popleft()
    if len(ventana) >= LIMITE:
        return False, 0, max(1, math.ceil(VENTANA_S - (ahora - ventana[0])))
    ventana.append(ahora)
    return True, LIMITE - len(ventana), 0


@app.get("/health")
def health():
    return {"status": "ok"}


@app.api_route("/{ruta:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(ruta: str, request: Request):
    path = "/" + ruta
    corr = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    base_headers = {"X-Correlation-ID": corr}

    if not path.startswith("/api/"):
        return error(404, "RECURSO_NO_ENCONTRADO", "no_encontrado", "Ruta no publicada", base_headers)

    clave, autenticado = cliente_de(request)
    permitido, restantes, reintentar = consumir_cupo(clave)
    base_headers.update({"X-RateLimit-Limit": str(LIMITE), "X-RateLimit-Remaining": str(restantes)})
    if not permitido:
        log.warning("evento=rate_limit cliente=%s corr=%s", clave, corr)
        return error(429, "LIMITE_EXCEDIDO", "limite_excedido",
                     "Demasiadas solicitudes; reintente mas tarde",
                     {**base_headers, "Retry-After": str(reintentar)})
    if not autenticado:
        log.warning("evento=no_autorizado cliente=%s corr=%s", clave, corr)
        return error(401, "NO_AUTORIZADO", "no_autorizado", "Token ausente o invalido",
                     {**base_headers, "WWW-Authenticate": "Bearer"})

    for metodo, patron, base, destino, timeout in RUTAS:
        m = patron.fullmatch(path)
        if m and request.method == metodo:
            url = base + destino.format(**m.groupdict())
            break
    else:
        return error(404, "RECURSO_NO_ENCONTRADO", "no_encontrado", "Ruta no publicada", base_headers)

    headers = {k: v for k, v in request.headers.items() if k.lower() in HEADERS_REENVIADOS}
    headers["X-Correlation-ID"] = corr
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(request.method, url, params=request.query_params,
                                     content=await request.body(), headers=headers)
    except httpx.TimeoutException:
        log.warning("evento=upstream_timeout cliente=%s destino=%s corr=%s", clave, url, corr)
        return error(504, "UPSTREAM_TIMEOUT", "timeout", "El servicio no respondio a tiempo", base_headers)
    except httpx.RequestError:
        log.warning("evento=upstream_no_disponible cliente=%s destino=%s corr=%s", clave, url, corr)
        return error(503, "UPSTREAM_NO_DISPONIBLE", "no_disponible", "Servicio no disponible", base_headers)

    log.info("evento=proxy cliente=%s %s %s -> %s corr=%s", clave, request.method, path, r.status_code, corr)
    respuesta = Response(content=r.content, status_code=r.status_code,
                         media_type=r.headers.get("content-type"))
    for k, v in base_headers.items():
        respuesta.headers[k] = v
    return respuesta
