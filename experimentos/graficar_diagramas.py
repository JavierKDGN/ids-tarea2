from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SALIDA = Path(__file__).resolve().parents[1] / "docs" / "figuras"
ACTORES = ["Cliente", "Gateway", "Saga", "Habitaciones", "Pagos y pasarela", "Confirmaciones"]
X = {a: i * 1.9 for i, a in enumerate(ACTORES)}

FELIZ = [
    ("Cliente", "Gateway", "POST /api/reservas", "ok"),
    ("Gateway", "Saga", "token y límite OK", "ok"),
    ("Saga", "Habitaciones", "1. reservar", "ok"),
    ("Habitaciones", "Saga", "RESERVADA", "ret"),
    ("Saga", "Pagos y pasarela", "2. cobrar garantía", "ok"),
    ("Pagos y pasarela", "Saga", "COBRADO", "ret"),
    ("Saga", "Confirmaciones", "3. emitir voucher", "ok"),
    ("Confirmaciones", "Saga", "EMITIDO", "ret"),
    ("Saga", "Cliente", "201 CONFIRMADA", "ret"),
]

COMPENSACION = [
    ("Cliente", "Gateway", "POST /api/reservas", "ok"),
    ("Gateway", "Saga", "token y límite OK", "ok"),
    ("Saga", "Habitaciones", "1. reservar", "ok"),
    ("Habitaciones", "Saga", "RESERVADA", "ret"),
    ("Saga", "Pagos y pasarela", "2. cobrar garantía", "ok"),
    ("Pagos y pasarela", "Saga", "COBRADO", "ret"),
    ("Saga", "Confirmaciones", "3. emitir voucher", "ok"),
    ("Confirmaciones", "Saga", "500 (falla forzada)", "err"),
    ("Saga", "Pagos y pasarela", "compensar 2: reembolsar (primero)", "comp"),
    ("Pagos y pasarela", "Saga", "REEMBOLSADO", "ret"),
    ("Saga", "Habitaciones", "compensar 1: liberar (después)", "comp"),
    ("Habitaciones", "Saga", "LIBERADA", "ret"),
    ("Saga", "Cliente", "502 COMPENSADA", "err"),
]

ESTILOS = {
    "ok": dict(color="#1f4e79", linestyle="-"),
    "ret": dict(color="#555555", linestyle="--"),
    "err": dict(color="#b3261e", linestyle="-"),
    "comp": dict(color="#b45f06", linestyle="-"),
}


def dibujar(mensajes, titulo, archivo):
    paso = 0.62
    alto = paso * (len(mensajes) + 1.6)
    fig, ax = plt.subplots(figsize=(9.2, alto * 0.62 + 1.0))
    for a, x in X.items():
        ax.text(x, 0.45, a, ha="center", va="bottom", fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc="#eef3f8", ec="#1f4e79"))
        ax.plot([x, x], [0.4, -paso * (len(mensajes) + 0.4)], color="#9aa5b1", linewidth=1, zorder=0)
    for i, (origen, destino, texto, tipo) in enumerate(mensajes):
        y = -paso * (i + 0.8)
        x0, x1 = X[origen], X[destino]
        estilo = ESTILOS[tipo]
        ax.annotate("", xy=(x1, y), xytext=(x0, y),
                    arrowprops=dict(arrowstyle="-|>", color=estilo["color"], linestyle=estilo["linestyle"], lw=1.3))
        ax.text((x0 + x1) / 2, y + 0.07, texto, ha="center", va="bottom", fontsize=8, color=estilo["color"])
    ax.set_title(titulo, fontsize=11, loc="left")
    ax.set_xlim(-1.0, X["Confirmaciones"] + 1.0)
    ax.set_ylim(-paso * (len(mensajes) + 0.6), 1.0)
    ax.axis("off")
    fig.tight_layout()
    SALIDA.mkdir(parents=True, exist_ok=True)
    fig.savefig(SALIDA / archivo, dpi=170)
    plt.close(fig)


def main():
    dibujar(FELIZ, "Flujo feliz: la reserva se confirma (201)", "flujo_feliz.png")
    dibujar(COMPENSACION, "Falla del último paso: compensación en orden inverso (502)", "flujo_compensacion.png")
    print("Diagramas en", SALIDA)


if __name__ == "__main__":
    main()
