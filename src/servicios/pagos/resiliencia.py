"""Circuit Breaker y Bulkhead ejecutados ALREDEDOR de la pasarela HTTP.

Estado y metricas en memoria
"""
import logging
import os
import threading
import time

log = logging.getLogger("pagos.resiliencia")

class RechazoCircuito(Exception):
    pass

class RechazoBulkhead(Exception):
    pass

class Resiliencia:
    def __init__(self):
        self.umbral = max(1, int(os.getenv("CB_FAILURE_THRESHOLD", "2")))
        self.recuperacion_s = float(os.getenv("CB_RECOVERY_SECONDS", "4"))
        self.capacidad = max(1, int(os.getenv("BULKHEAD_MAX_CONCURRENT", "10")))
        self._lock = threading.Lock()
        self._semaforo = threading.BoundedSemaphore(self.capacidad)
        self.reiniciar()

    def reiniciar(self):
        with self._lock:
            self.estado = "CLOSED"
            self.fallos_consecutivos = 0
            self.abierto_desde = 0.0
            self.probe_en_curso = False
            self.en_vuelo = 0
            self.contadores = {
                "llamadas_pasarela": 0, "exitos": 0, "fallos": 0,
                "timeouts": 0, "rechazos_circuito": 0, "rechazos_bulkhead": 0,
                "fallbacks": 0, "transiciones_open": 0, "reintentos": 0,
            }

    def snapshot(self):
        with self._lock:
            return {"circuit_breaker": self.estado,
                    "fallos_consecutivos": self.fallos_consecutivos,
                    "bulkhead_en_uso": self.en_vuelo,
                    "bulkhead_capacidad": self.capacidad,
                    "umbral": self.umbral,
                    **self.contadores}

    def sumar(self, clave):
        with self._lock:
            self.contadores[clave] += 1

    def entrar(self):
        """Devuelve si la llamada es una unica prueba HALF_OPEN."""
        with self._lock:
            ahora = time.monotonic()
            if self.estado == "OPEN":
                if ahora - self.abierto_desde >= self.recuperacion_s:
                    self.estado = "HALF_OPEN"
                    log.warning("evento=circuit_breaker estado=HALF_OPEN")
                    self.probe_en_curso = False
                else:
                    self.contadores["rechazos_circuito"] += 1
                    self.contadores["fallbacks"] += 1
                    raise RechazoCircuito("Circuit Breaker OPEN: fallo rapido")
            if self.estado == "HALF_OPEN":
                if self.probe_en_curso:
                    self.contadores["rechazos_circuito"] += 1
                    self.contadores["fallbacks"] += 1
                    raise RechazoCircuito("Circuit Breaker HALF_OPEN ocupado")
                self.probe_en_curso = True
                es_prueba = True
            else:
                es_prueba = False
        if not self._semaforo.acquire(blocking=False):
            with self._lock:
                if es_prueba:
                    self.probe_en_curso = False
                log.warning("evento=bulkhead_rechazo capacidad=%s", self.capacidad)
                self.contadores["rechazos_bulkhead"] += 1
                self.contadores["fallbacks"] += 1
            raise RechazoBulkhead("Bulkhead lleno: concurrencia aislada")
        with self._lock:
            self.en_vuelo += 1
            self.contadores["llamadas_pasarela"] += 1
        return es_prueba

    def salir(self, es_prueba, ok, timeout=False):
        with self._lock:
            self.en_vuelo -= 1
            if timeout:
                self.contadores["timeouts"] += 1
            if ok:
                self.contadores["exitos"] += 1
                self.fallos_consecutivos = 0
                if self.estado == "HALF_OPEN":
                    self.estado = "CLOSED"
                    log.info("evento=circuit_breaker estado=CLOSED")
            else:
                self.contadores["fallos"] += 1
                self.fallos_consecutivos += 1
                if self.estado == "HALF_OPEN" or self.fallos_consecutivos >= self.umbral:
                    self.estado = "OPEN"
                    self.abierto_desde = time.monotonic()
                    self.contadores["transiciones_open"] += 1
                    log.warning("evento=circuit_breaker estado=OPEN fallos=%s", self.fallos_consecutivos)
            if es_prueba:
                self.probe_en_curso = False
        self._semaforo.release()
