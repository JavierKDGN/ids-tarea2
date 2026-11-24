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


def estados_breaker():
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    fig, ax = plt.subplots(figsize=(8.4, 3.9))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 4.6)
    ax.axis("off")

    def caja(x, y, w, h, texto, fc, ec, tam=9, bold=False):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.05,rounding_size=0.15", fc=fc, ec=ec, lw=1.3))
        ax.text(x + w / 2, y + h / 2, texto, ha="center", va="center", fontsize=tam, fontweight="bold" if bold else "normal")

    def flecha(p0, p1, texto, tx, ty, color="#1f4e79", rad=0.0):
        ax.add_patch(FancyArrowPatch(p0, p1, arrowstyle="-|>", mutation_scale=12, color=color, lw=1.3,
                                     connectionstyle=f"arc3,rad={rad}"))
        ax.text(tx, ty, texto, ha="center", va="center", fontsize=7.5, color=color)

    caja(0.3, 2.2, 2.2, 0.9, "CLOSED\nllama a la pasarela", "#e6f4ea", "#2a9d8f", bold=True)
    caja(7.5, 2.2, 2.2, 0.9, "OPEN\nfail-fast con fallback", "#fdecea", "#b3261e", bold=True)
    caja(3.9, 0.3, 2.2, 0.9, "HALF_OPEN\nuna sola prueba", "#fff4e0", "#b45f06", bold=True)
    caja(0.3, 3.8, 9.4, 0.6, "Antes de cada llamada, el Bulkhead limita las simultáneas: si está lleno, rechazo inmediato (RECHAZADO)", "#eef3f8", "#1f4e79", tam=7.5)

    flecha((2.55, 2.65), (7.45, 2.65), "2 fallos seguidos (timeout o 503 NO_COBRADO)", 5.0, 2.85)
    flecha((8.6, 2.15), (6.15, 0.85), "pasan 4 s", 8.4, 1.1, color="#b3261e", rad=-0.15)
    flecha((3.85, 0.85), (1.4, 2.15), "la prueba tiene éxito", 1.5, 1.05, color="#2a9d8f", rad=-0.15)
    flecha((5.0, 1.25), (8.2, 2.15), "la prueba falla", 6.5, 2.0, color="#b3261e", rad=0.15)

    SALIDA.mkdir(parents=True, exist_ok=True)
    fig.savefig(SALIDA / "estados_breaker.png", dpi=170, bbox_inches="tight")
    plt.close(fig)


def main():
    dibujar(FELIZ, "Flujo feliz: la reserva se confirma (201)", "flujo_feliz.png")
    dibujar(COMPENSACION, "Falla del último paso: compensación en orden inverso (502)", "flujo_compensacion.png")
    estados_breaker()
    print("Diagramas en", SALIDA)


if __name__ == "__main__":
    main()
