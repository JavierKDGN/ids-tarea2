import json
import sys

from utils import ROOT, compose, guardar, interno, nuevo_id, reiniciar_stack

LLAMADAS = 6

SONDA = """
import json, sys, time, urllib.request, urllib.error
prefijo = sys.argv[1]
salida = []
for i in range(int(sys.argv[2])):
    cuerpo = json.dumps({"id_pago": f"pay-{prefijo}-{i}", "id_reserva": f"{prefijo}-{i}", "monto": "80.00"}).encode()
    req = urllib.request.Request("http://127.0.0.1:8002/pagos", data=cuerpo, method="POST", headers={"Content-Type": "application/json"})
    inicio = time.perf_counter()
    try:
        r = urllib.request.urlopen(req, timeout=30)
        codigo, texto = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        codigo, texto = e.code, e.read().decode()
    ms = round((time.perf_counter() - inicio) * 1000, 1)
    datos = json.loads(texto)
    salida.append({"llamada": i + 1, "ms": ms, "http": codigo, "estado": datos.get("estado"), "fallback": datos.get("fallback")})
print(json.dumps(salida))
"""


def main():
    env = {"RATE_LIMIT_PER_MINUTE": "5000", "RECONCILIACION_SEGUNDOS": "0", "PASARELA_TIMEOUT": "1.0",
           "CB_FAILURE_THRESHOLD": "2", "CB_RECOVERY_SECONDS": "60", "BULKHEAD_MAX_CONCURRENT": "10"}
    reiniciar_stack(env)
    interno("pasarela", "POST", "/chaos", {"latencia_segundos": 6, "forzar_error": False})
    salida = compose("exec", "-T", "pagos", "python", "-c", SONDA, nuevo_id("ff"), str(LLAMADAS))
    if salida.returncode != 0:
        print(salida.stderr)
        sys.exit(1)
    llamadas = json.loads(salida.stdout.strip().splitlines()[-1])
    metricas = interno("pagos", "GET", "/metricas")["datos"]
    resultado = {"descripcion": "Llamadas secuenciales a POST /pagos con la pasarela respondiendo en 6 s y timeout de 1 s; el Circuit Breaker abre tras 2 fallos",
                 "llamadas": llamadas, "metricas": metricas}
    guardar("escenario3_failfast", resultado)
    for c in llamadas:
        print(c)
    print("breaker:", metricas["circuit_breaker"], "rechazos_circuito:", metricas["rechazos_circuito"])
    reiniciar_stack()


if __name__ == "__main__":
    main()
