import sys

from utils import (gateway, guardar, habitacion_unica, interno, limpiar_fallas, logs_por_corr,
                   nuevo_id, reserva, verificar_gateway)


def estados(rid):
    return {
        "habitacion": interno("habitaciones", "GET", f"/reservas/{rid}"),
        "pago": interno("pagos", "GET", f"/pagos/pay-{rid}"),
        "confirmacion": interno("confirmaciones", "GET", f"/confirmaciones/voucher-{rid}"),
    }


def escenario_feliz():
    rid, corr, hab = nuevo_id("feliz"), nuevo_id("esc1"), habitacion_unica()
    respuesta = reserva(rid, hab, corr)
    reintento = reserva(rid, hab, nuevo_id("esc1-reintento"))
    resumen = gateway("GET", f"/api/reservas/{rid}/resumen", corr=nuevo_id("esc1-resumen"))
    final = estados(rid)
    lineas = logs_por_corr(corr)
    resultado = {"id_reserva": rid, "correlation_id": corr, "respuesta": respuesta,
                 "reintento_idempotente": reintento, "resumen_bff": resumen, "estados": final}
    guardar("escenario1_feliz", resultado, lineas)
    ok = (respuesta["http"] == 201 and respuesta["datos"]["estado"] == "CONFIRMADA"
          and reintento["http"] == 200
          and final["habitacion"]["datos"]["estado"] == "RESERVADA"
          and final["pago"]["datos"]["estado"] == "COBRADO"
          and final["confirmacion"]["datos"]["estado"] == "EMITIDO")
    return ok, resultado, lineas


def escenario_compensacion():
    rid, corr, hab = nuevo_id("comp"), nuevo_id("esc2"), habitacion_unica()
    interno("confirmaciones", "POST", "/chaos", {"forzar_error": True})
    try:
        respuesta = reserva(rid, hab, corr)
    finally:
        interno("confirmaciones", "POST", "/chaos", {"forzar_error": False})
    resumen = gateway("GET", f"/api/reservas/{rid}/resumen", corr=nuevo_id("esc2-resumen"))
    final = estados(rid)
    lineas = logs_por_corr(corr)
    texto = "\n".join(lineas)
    pos_reembolso = texto.find(f"ruta=/pagos/pay-{rid}/reembolsar")
    pos_liberar = texto.find(f"ruta=/reservas/{rid}/liberar")
    orden_ok = 0 <= pos_reembolso < pos_liberar
    resultado = {"id_reserva": rid, "correlation_id": corr, "respuesta": respuesta,
                 "resumen_bff": resumen, "estados": final, "reembolso_antes_de_liberar": orden_ok}
    guardar("escenario2_compensacion", resultado, lineas)
    ok = (respuesta["http"] == 502 and respuesta["datos"]["estado"] == "COMPENSADA"
          and final["pago"]["datos"]["estado"] == "REEMBOLSADO"
          and final["habitacion"]["datos"]["estado"] == "LIBERADA"
          and final["confirmacion"]["http"] == 404 and orden_ok)
    return ok, resultado, lineas


def main():
    if not verificar_gateway():
        print("El gateway no responde. Levantar con: docker compose up -d --build")
        sys.exit(1)
    limpiar_fallas()
    fallos = 0
    for nombre, fn in (("Escenario 1 - camino feliz", escenario_feliz),
                       ("Escenario 2 - falla del ultimo paso y compensacion", escenario_compensacion)):
        ok, resultado, lineas = fn()
        print(f"{nombre}: {'OK' if ok else 'FALLO'}  (corr={resultado['correlation_id']}, {len(lineas)} lineas de log)")
        fallos += 0 if ok else 1
    limpiar_fallas()
    sys.exit(1 if fallos else 0)


if __name__ == "__main__":
    main()
