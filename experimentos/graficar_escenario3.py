import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

AQUI = Path(__file__).resolve().parent
RESUMEN = AQUI.parent / "evidence" / "docker" / "escenario3_resumen.json"
ETIQUETAS = {"A_con_patrones": "A: con patrones", "B_sin_breaker_ni_bulkhead": "B: sin breaker ni bulkhead",
             "C_sin_ningun_patron": "C: sin breaker, bulkhead ni timeout corto"}
COLORES = {"A_con_patrones": "#2a9d8f", "B_sin_breaker_ni_bulkhead": "#e9c46a", "C_sin_ningun_patron": "#e76f51"}
ESTADOS = ["RECHAZADO", "INCIERTO", "COBRADO"]
COLOR_ESTADO = {"RECHAZADO": "#2a9d8f", "INCIERTO": "#e9c46a", "COBRADO": "#e76f51"}


def grafico_estados(resumen):
    fig, ax = plt.subplots(figsize=(8, 4.5))
    nombres = list(resumen)
    base = [0] * len(nombres)
    for estado in ESTADOS:
        valores = [resumen[n]["lentitud_concurrente"]["pagos_tras_drenar"].get(estado, 0) for n in nombres]
        ax.bar([ETIQUETAS[n] for n in nombres], valores, bottom=base, label=estado, color=COLOR_ESTADO[estado])
        base = [b + v for b, v in zip(base, valores)]
    ax.set_ylabel("pagos de las 50 reservas simultaneas")
    ax.set_title("Estado de los pagos tras la lentitud de la pasarela (6 s)")
    ax.legend(title="estado del pago")
    fig.tight_layout()
    fig.savefig(AQUI / "grafico_estados_pagos.png", dpi=150)
    plt.close(fig)


def grafico_pasarela_caida(resumen):
    fig, ax = plt.subplots(figsize=(8, 4.5))
    nombres = list(resumen)
    valores = [resumen[n]["caida"]["llamadas_que_llegaron_a_la_pasarela"] for n in nombres]
    barras = ax.bar([ETIQUETAS[n] for n in nombres], valores, color=[COLORES[n] for n in nombres])
    ax.bar_label(barras)
    ax.set_ylabel("llamadas que llegaron a la pasarela caida")
    ax.set_title("12 reservas con la pasarela caida: cuantas llegaron a golpearla")
    ax.set_ylim(0, 14)
    fig.tight_layout()
    fig.savefig(AQUI / "grafico_pasarela_caida.png", dpi=150)
    plt.close(fig)


def grafico_sonda_pagos():
    series = {}
    with open(AQUI / "resultados_sondas.csv", encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            if fila["sonda"] == "pagos_interno":
                series.setdefault(fila["configuracion"], []).append((float(fila["t_s"]), float(fila["ms"])))
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for nombre, puntos in series.items():
        ax.plot([p[0] for p in puntos], [p[1] for p in puntos], label=ETIQUETAS[nombre], color=COLORES[nombre], linewidth=1.4)
    ax.set_yscale("log")
    ax.set_xlabel("segundos desde el inicio de la sonda")
    ax.set_ylabel("latencia de GET /pagos/x en ms (escala log)")
    ax.set_title("Pagos sigue atendiendo mientras la pasarela esta lenta?")
    ax.legend()
    fig.tight_layout()
    fig.savefig(AQUI / "grafico_sonda_pagos.png", dpi=150)
    plt.close(fig)


def main():
    resumen = json.loads(RESUMEN.read_text(encoding="utf-8"))
    grafico_estados(resumen)
    grafico_pasarela_caida(resumen)
    grafico_sonda_pagos()
    print("Graficos generados en", AQUI)


if __name__ == "__main__":
    main()
