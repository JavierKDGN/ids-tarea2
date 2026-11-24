import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8080")
TOKEN = os.getenv("GATEWAY_TOKEN", "demo-token")
PUERTOS = {"saga": 8000, "habitaciones": 8001, "pagos": 8002, "confirmaciones": 8003, "pasarela": 8004}

SCRIPT_INTERNO = """
import json, sys, urllib.request, urllib.error
metodo, url, cuerpo = sys.argv[1], sys.argv[2], sys.argv[3]
datos = cuerpo.encode() if cuerpo else None
req = urllib.request.Request(url, data=datos, method=metodo, headers={"Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=20)
    codigo, texto = r.status, r.read().decode()
except urllib.error.HTTPError as e:
    codigo, texto = e.code, e.read().decode()
print(json.dumps({"http": codigo, "datos": json.loads(texto) if texto else None}))
"""


def compose(*args, env=None):
    entorno = {**os.environ, **env} if env else None
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", check=False, env=entorno)


def reiniciar_stack(env=None, espera_s=120):
    compose("down", "-v")
    salida = compose("up", "-d", "--build", env=env)
    if salida.returncode != 0:
        raise RuntimeError(salida.stderr)
    limite = time.time() + espera_s
    while time.time() < limite:
        if verificar_gateway():
            return
        time.sleep(1)
    raise RuntimeError("el gateway no quedo disponible")


def interno(servicio, metodo, ruta, cuerpo=None):
    url = f"http://127.0.0.1:{PUERTOS[servicio]}{ruta}"
    texto = json.dumps(cuerpo) if cuerpo is not None else ""
    salida = compose("exec", "-T", servicio, "python", "-c", SCRIPT_INTERNO, metodo, url, texto)
    if salida.returncode != 0:
        raise RuntimeError(f"fallo exec en {servicio}: {salida.stderr.strip()}")
    return json.loads(salida.stdout.strip().splitlines()[-1])


def gateway(metodo, ruta, cuerpo=None, corr=None, idempotency=None, timeout=15, token=TOKEN):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if corr:
        headers["X-Correlation-ID"] = corr
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    inicio = time.perf_counter()
    r = httpx.request(metodo, GATEWAY + ruta, json=cuerpo, headers=headers, timeout=timeout)
    duracion = round(time.perf_counter() - inicio, 3)
    try:
        datos = r.json()
    except ValueError:
        datos = {"respuesta": r.text}
    return {"http": r.status_code, "duracion_s": duracion, "datos": datos}


def reserva(rid, habitacion, corr, desde="2027-03-01", hasta="2027-03-04", monto="80.00"):
    cuerpo = {"id_reserva": rid, "habitacion_id": habitacion, "fecha_inicio": desde,
              "fecha_fin": hasta, "monto": monto}
    return gateway("POST", "/api/reservas", cuerpo, corr=corr, idempotency=rid)


def limpiar_fallas():
    interno("confirmaciones", "POST", "/chaos", {"forzar_error": False})
    interno("pasarela", "POST", "/chaos", {"forzar_error": False, "latencia_segundos": 0})
    interno("pagos", "POST", "/demo/reset-resiliencia")


def logs_por_corr(corr):
    salida = compose("logs", "--no-log-prefix")
    lineas = [l for l in (salida.stdout + salida.stderr).splitlines() if corr in l]
    return sorted(lineas)


def nuevo_id(prefijo):
    return f"{prefijo}-{uuid.uuid4().hex[:8]}"


def habitacion_unica():
    return 20000 + int(uuid.uuid4().hex[:6], 16) % 900000


def guardar(nombre, datos, lineas=None):
    carpeta = ROOT / "evidence" / "docker"
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / f"{nombre}.json").write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    if lineas is not None:
        (carpeta / f"{nombre}.log").write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return carpeta


def verificar_gateway():
    try:
        return httpx.get(GATEWAY + "/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def percentil(valores, p):
    if not valores:
        return None
    ordenados = sorted(valores)
    k = (len(ordenados) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(ordenados) - 1)
    return round(ordenados[f] + (ordenados[c] - ordenados[f]) * (k - f), 3)
