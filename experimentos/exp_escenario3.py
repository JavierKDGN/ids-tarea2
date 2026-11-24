import csv
import json
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from utils import (ROOT, compose, gateway, guardar, habitacion_unica, interno, nuevo_id, percentil,
                   reiniciar_stack, reserva)

CONFIGURACIONES = {
    "A_con_patrones": {
        "descripcion": "Timeout 1 s + Circuit Breaker (umbral 2) + Bulkhead (2)",
        "env": {"RATE_LIMIT_PER_MINUTE": "5000", "RECONCILIACION_SEGUNDOS": "0", "PASARELA_TIMEOUT": "1.0",
                "CB_FAILURE_THRESHOLD": "2", "CB_RECOVERY_SECONDS": "4", "BULKHEAD_MAX_CONCURRENT": "2"},
    },
    "B_sin_breaker_ni_bulkhead": {
        "descripcion": "Solo timeout 1 s; Circuit Breaker y Bulkhead neutralizados",
        "env": {"RATE_LIMIT_PER_MINUTE": "5000", "RECONCILIACION_SEGUNDOS": "0", "PASARELA_TIMEOUT": "1.0",
                "CB_FAILURE_THRESHOLD": "100000", "CB_RECOVERY_SECONDS": "4", "BULKHEAD_MAX_CONCURRENT": "1000"},
    },
    "C_sin_ningun_patron": {
        "descripcion": "Sin Circuit Breaker, sin Bulkhead y con timeout de 30 s hacia la pasarela",
        "env": {"RATE_LIMIT_PER_MINUTE": "5000", "RECONCILIACION_SEGUNDOS": "0", "PASARELA_TIMEOUT": "30",
                "CB_FAILURE_THRESHOLD": "100000", "CB_RECOVERY_SECONDS": "4", "BULKHEAD_MAX_CONCURRENT": "1000"},
    },
    "D_solo_bulkhead": {
        "descripcion": "Solo Bulkhead de 2 con timeout de 30 s; Circuit Breaker neutralizado",
        "env": {"RATE_LIMIT_PER_MINUTE": "5000", "RECONCILIACION_SEGUNDOS": "0", "PASARELA_TIMEOUT": "30",
                "CB_FAILURE_THRESHOLD": "100000", "CB_RECOVERY_SECONDS": "4", "BULKHEAD_MAX_CONCURRENT": "2"},
    },
}

LATENCIA_PASARELA_S = 6
CONCURRENTES = 50
CAIDA_SECUENCIAL = 12

SONDA_PAGOS = """
import json, sys, time, urllib.request, urllib.error
fin = time.time() + float(sys.argv[1])
muestras = []
t0 = time.time()
while time.time() < fin:
    inicio = time.perf_counter()
    try:
        codigo = urllib.request.urlopen("http://127.0.0.1:8002/pagos/sonda-inexistente", timeout=15).status
    except urllib.error.HTTPError as e:
        codigo = e.code
    except Exception:
        codigo = 0
    muestras.append({"t_s": round(time.time() - t0, 3), "ms": round((time.perf_counter() - inicio) * 1000, 1), "http": codigo})
    time.sleep(0.25)
print(json.dumps(muestras))
"""


SCRIPT_ESTADOS = """
import json, sys, urllib.request, urllib.error
salida = {}
for id_pago in sys.argv[1:]:
    try:
        salida[id_pago] = json.loads(urllib.request.urlopen("http://127.0.0.1:8002/pagos/" + id_pago, timeout=10).read())["estado"]
    except urllib.error.HTTPError as e:
        salida[id_pago] = "HTTP_" + str(e.code)
    except Exception:
        salida[id_pago] = "SIN_RESPUESTA"
print(json.dumps(salida))
"""


def estados_pagos(ids):
    salida = compose("exec", "-T", "pagos", "python", "-c", SCRIPT_ESTADOS, *[f"pay-{i}" for i in ids])
    estados = json.loads(salida.stdout.strip().splitlines()[-1])
    conteo = {}
    for e in estados.values():
        conteo[e] = conteo.get(e, 0) + 1
    return conteo


def estados_saga(ids):
    conteo = {}
    for i in ids:
        r = gateway("GET", f"/api/reservas/{i}", timeout=20)
        e = r["datos"].get("estado") or f"HTTP_{r['http']}"
        conteo[e] = conteo.get(e, 0) + 1
    return conteo


class SondasGateway:
    def __init__(self, rid_resumen):
        self.rid = rid_resumen
        self.detener = threading.Event()
        self.muestras = {"disponibilidad_via_gateway": [], "resumen_bff_via_gateway": []}
        self.t0 = time.time()
        self.hilos = [
            threading.Thread(target=self._bucle, args=("disponibilidad_via_gateway",
                lambda: gateway("GET", "/api/habitaciones/disponibilidad?habitacion_id=1&fecha_inicio=2030-01-01&fecha_fin=2030-01-02", timeout=20))),
            threading.Thread(target=self._bucle, args=("resumen_bff_via_gateway",
                lambda: gateway("GET", f"/api/reservas/{self.rid}/resumen", timeout=20))),
        ]

    def _bucle(self, nombre, llamada):
        while not self.detener.is_set():
            try:
                r = llamada()
                ms, codigo = r["duracion_s"] * 1000, r["http"]
            except Exception:
                ms, codigo = 20000.0, 0
            self.muestras[nombre].append({"t_s": round(time.time() - self.t0, 3), "ms": round(ms, 1), "http": codigo})
            time.sleep(0.25)

    def iniciar(self):
        for h in self.hilos:
            h.start()

    def parar(self):
        self.detener.set()
        for h in self.hilos:
            h.join()
        return self.muestras


def resumen_latencias(valores):
    return {"n": len(valores), "p50": percentil(valores, 50), "p95": percentil(valores, 95),
            "max": round(max(valores), 3) if valores else None,
            "media": round(statistics.mean(valores), 3) if valores else None}


def ejecutar_reserva(prefijo, i):
    rid = nuevo_id(f"{prefijo}-{i}")
    inicio = time.time()
    r = reserva(rid, habitacion_unica(), nuevo_id(prefijo))
    return {"i": i, "id_reserva": rid, "t_inicio_s": inicio, "duracion_s": r["duracion_s"],
            "http": r["http"], "estado": r["datos"].get("estado") or r["datos"].get("code")}


def contar(filas):
    conteo = {}
    for f in filas:
        conteo[f["estado"]] = conteo.get(f["estado"], 0) + 1
    return conteo


def llamadas_a_pasarela_caida():
    salida = compose("logs", "pasarela", "--no-log-prefix")
    return sum(1 for l in (salida.stdout + salida.stderr).splitlines() if "evento=pasarela_caida" in l)


def correr_configuracion(nombre, cfg):
    print(f"\n=== {nombre}: {cfg['descripcion']}")
    reiniciar_stack(cfg["env"])
    resultado = {"configuracion": nombre, "descripcion": cfg["descripcion"], "env": cfg["env"]}
    filas, sondas = [], []

    base = [ejecutar_reserva(f"base-{nombre[:1]}", i) for i in range(5)]
    filas += [{**f, "fase": "base"} for f in base]
    rid_resumen = base[0]["id_reserva"]
    resultado["base"] = {"latencias": resumen_latencias([f["duracion_s"] for f in base]), "estados": contar(base),
                         "metricas": interno("pagos", "GET", "/metricas")["datos"]}
    print(f"  base: {resultado['base']['latencias']}")

    interno("pagos", "POST", "/demo/reset-resiliencia")
    interno("pasarela", "POST", "/chaos", {"latencia_segundos": LATENCIA_PASARELA_S, "forzar_error": False})
    duracion_fase = 3 + 3 * LATENCIA_PASARELA_S + 12
    sonda_pagos = subprocess.Popen(["docker", "compose", "exec", "-T", "pagos", "python", "-c", SONDA_PAGOS,
                                    str(duracion_fase)], cwd=ROOT, stdout=subprocess.PIPE, text=True, encoding="utf-8")
    sg = SondasGateway(rid_resumen)
    sg.iniciar()
    time.sleep(1.5)
    t_carga = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENTES) as pool:
        lentas = list(pool.map(lambda i: ejecutar_reserva(f"lenta-{nombre[:1]}", i), range(CONCURRENTES)))
    pared = round(time.time() - t_carga, 3)
    time.sleep(2)
    muestras_gw = sg.parar()
    muestras_pagos = json.loads(sonda_pagos.communicate()[0] or "[]")
    metricas_lenta = interno("pagos", "GET", "/metricas")["datos"]
    filas += [{**f, "fase": "lenta"} for f in lentas]
    sondas += [{"sonda": k, **m} for k, v in muestras_gw.items() for m in v]
    sondas += [{"sonda": "pagos_interno", **m} for m in muestras_pagos]
    resultado["lentitud_concurrente"] = {
        "latencia_pasarela_s": LATENCIA_PASARELA_S, "reservas_concurrentes": CONCURRENTES,
        "tiempo_total_s": pared, "latencias": resumen_latencias([f["duracion_s"] for f in lentas]),
        "estados": contar(lentas), "metricas": metricas_lenta,
        "sondas": {k: resumen_latencias([m["ms"] / 1000 for m in v]) for k, v in muestras_gw.items()}
                  | {"pagos_interno": resumen_latencias([m["ms"] / 1000 for m in muestras_pagos])},
    }
    print(f"  lenta: {resultado['lentitud_concurrente']['latencias']} estados={resultado['lentitud_concurrente']['estados']}")
    print(f"  sondas: {resultado['lentitud_concurrente']['sondas']}")

    time.sleep(LATENCIA_PASARELA_S + 4)
    ids_lentas = [f["id_reserva"] for f in lentas]
    resultado["lentitud_concurrente"]["pagos_tras_drenar"] = estados_pagos(ids_lentas)
    resultado["lentitud_concurrente"]["saga_tras_drenar"] = estados_saga(ids_lentas)
    print(f"  tras drenar: pagos={resultado['lentitud_concurrente']['pagos_tras_drenar']} saga={resultado['lentitud_concurrente']['saga_tras_drenar']}")
    interno("pagos", "POST", "/demo/reset-resiliencia")
    interno("pasarela", "POST", "/chaos", {"latencia_segundos": 0, "forzar_error": True})
    antes = llamadas_a_pasarela_caida()
    caida = []
    for i in range(CAIDA_SECUENCIAL):
        caida.append(ejecutar_reserva(f"caida-{nombre[:1]}", i))
    llegadas = llamadas_a_pasarela_caida() - antes
    metricas_caida = interno("pagos", "GET", "/metricas")["datos"]
    filas += [{**f, "fase": "caida"} for f in caida]
    resultado["caida"] = {
        "reservas_secuenciales": CAIDA_SECUENCIAL, "latencias": resumen_latencias([f["duracion_s"] for f in caida]),
        "estados": contar(caida), "llamadas_que_llegaron_a_la_pasarela": llegadas,
        "metricas": metricas_caida, "latencia_por_intento_s": [f["duracion_s"] for f in caida],
    }
    print(f"  caida: llegaron a la pasarela {llegadas}/{CAIDA_SECUENCIAL} breaker={metricas_caida['circuit_breaker']} {resultado['caida']['latencias']}")

    interno("pasarela", "POST", "/chaos", {"latencia_segundos": 0, "forzar_error": False})
    time.sleep(4.5)
    recuperada = ejecutar_reserva(f"recup-{nombre[:1]}", 0)
    resultado["recuperacion"] = {"intento": recuperada, "metricas": interno("pagos", "GET", "/metricas")["datos"]}
    filas.append({**recuperada, "fase": "recuperacion"})
    print(f"  recuperacion: {recuperada['estado']} breaker={resultado['recuperacion']['metricas']['circuit_breaker']}")

    guardar(f"escenario3_{nombre}", resultado)
    return resultado, filas, sondas


def escribir_csv(filas, sondas):
    ruta = ROOT / "experimentos"
    with open(ruta / "resultados_escenario3.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["configuracion", "fase", "i", "duracion_s", "http", "estado"])
        w.writeheader()
        for fila in filas:
            w.writerow({k: fila.get(k) for k in w.fieldnames})
    with open(ruta / "resultados_sondas.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["configuracion", "sonda", "t_s", "ms", "http"])
        w.writeheader()
        for s in sondas:
            w.writerow({k: s.get(k) for k in w.fieldnames})


def main():
    elegidas = sys.argv[1:] or list(CONFIGURACIONES)
    todas_filas, todas_sondas, resumen = [], [], {}
    for nombre in elegidas:
        resultado, filas, sondas = correr_configuracion(nombre, CONFIGURACIONES[nombre])
        resumen[nombre] = resultado
        todas_filas += [{**f, "configuracion": nombre} for f in filas]
        todas_sondas += [{**s, "configuracion": nombre} for s in sondas]
    escribir_csv(todas_filas, todas_sondas)
    guardar("escenario3_resumen", resumen)
    reiniciar_stack()
    print("\nListo. CSV en experimentos/ y JSON en evidence/docker/")


if __name__ == "__main__":
    main()
