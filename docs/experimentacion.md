# Experimentación: fallas y patrones de resiliencia

Todo se probó sobre el sistema real en `docker compose`, entrando por el gateway (`localhost:8080`). Las cifras salen de `evidence/docker/` y `experimentos/`.

## Cómo reproducir

```
docker compose up -d --build
pip install -r experimentos/requirements.txt
python experimentos/exp_feliz_compensacion.py
python experimentos/exp_escenario3.py
python experimentos/graficar_escenario3.py
```

`exp_escenario3.py` reinicia el sistema tres veces y tarda unos 10 minutos. Sube `RATE_LIMIT_PER_MINUTE` a 5000 para que el gateway no distorsione la carga.

## Componentes

| Componente | Papel en los experimentos |
|---|---|
| Gateway (8080, único publicado) | Entrada de todas las peticiones: token Bearer (401), rate limit (429), enrutamiento y timeouts (504/503). Sin lógica de negocio. |
| BFF (8005) | Resumen de reserva en una sola respuesta (saga, habitaciones, confirmaciones). Solo lectura y no depende de Pagos. Degrada con `degradado: true`. |
| Saga (8000) | Orquesta reservar, cobrar y confirmar; compensa en orden inverso. Ante un pago incierto queda `PENDIENTE_PAGO` y concilia después. |
| Habitaciones (8001) | Reserva y libera; impide fechas superpuestas. Su estado final prueba la consistencia. |
| Pagos (8002) | Dependencia frágil. Timeout 1 s, Circuit Breaker (abre tras 2 fallos, prueba a los 4 s), Bulkhead (2 simultáneas) y fallback "NO cobrado". Expone `/metricas`, con contadores y latencia de la pasarela. |
| Pasarela simulada (8004) | `POST /chaos` provoca latencia o error sin tocar código. Con latencia cobra igual después de dormir. |
| Confirmaciones (8003) | `POST /chaos` hace fallar la emisión del voucher: es la falla del último paso. |
| Correlación (`src/common`) | Un `X-Correlation-ID` permite seguir una reserva en los logs de todos los contenedores. |

Cada servicio tiene su propia SQLite. Lo que no se publica (`/chaos`, `/metricas`) se invoca con `docker compose exec`.

## Escenarios 1 y 2

- **Camino feliz:** 201 `CONFIRMADA`, reintento idempotente 200, habitación `RESERVADA`, pago `COBRADO`, voucher `EMITIDO`.
- **Falla del último paso:** Confirmaciones responde 500 y la saga compensa en este orden: reembolso, liberación de la habitación, `COMPENSADA` (502). Queda pago `REEMBOLSADO`, habitación `LIBERADA` y sin voucher. El script comprueba el orden en el log.

Evidencia: `evidence/docker/escenario1_feliz` y `escenario2_compensacion` (`.json` y `.log`).

## Escenario 3: pasarela lenta y caída

Tres configuraciones de Pagos, cambiadas solo con variables de entorno:

| | Timeout | Circuit Breaker | Bulkhead |
|---|---|---|---|
| A: con patrones | 1 s | umbral 2 | 2 |
| B: sin breaker ni bulkhead | 1 s | neutralizado | neutralizado |
| C: sin ningún patrón | 30 s | neutralizado | neutralizado |

Fases: 50 reservas simultáneas con la pasarela a 6 s, luego 12 reservas seguidas con la pasarela caída y una recuperación. Se usaron 50 reservas porque Pagos atiende en un pool de 40 hilos.

| Resultado | A | B | C |
|---|---|---|---|
| Pagos tras la lentitud | 47 `RECHAZADO`, 3 `INCIERTO` | 50 `INCIERTO` | 50 `COBRADO` |
| Reservas sin resolver | 3 | 50 | 50 |
| Llamadas que llegaron a la pasarela | 3 de 50 | 50 | 50 |
| Latencia de Pagos, máximo | 120 ms | 78 ms | 3631 ms |
| Con pasarela caída, llamadas que la golpearon | 3 de 12 | 12 | 12 |

**Interpretación**
- **A:** el breaker y el bulkhead rechazaron 47 llamadas al instante (44 y 3) y la saga las compensó limpiamente.
- **B:** las 50 llamadas esperaron el timeout y todas las reservas quedaron por conciliar.
- **C:** la pasarela terminó los 50 cobros después de que la saga se rindió a los 2,5 s. Resultado: 50 cobros sin reserva confirmada, y Pagos sin responder durante 3,6 s.
- **Caída:** con el breaker abierto se dejó de golpear a la pasarela caída (3 llamadas en vez de 12).

Gráficos: `experimentos/grafico_estados_pagos.png`, `grafico_pasarela_caida.png` y `grafico_sonda_pagos.png`.

## Límites

- **Latencia:** no distingue las configuraciones. Con 30 reservas simultáneas sin fallas ya tarda unos 5 s, por la capacidad del propio sistema (un proceso por servicio y SQLite). La evidencia es el estado de los pagos y las llamadas a la pasarela.
- **Breaker:** con una pasarela que falla al instante no baja la latencia; evita llamadas.
- **Bulkhead:** con capacidad 2 y la pasarela sana, 3 reservas simultáneas dieron 1 `COMPENSADA` porque el bulkhead rechazó una llamada legítima. Por eso el valor por defecto del sistema pasó a 10 (6 rondas de 8 reservas simultáneas: 0 rechazos). La configuración A del escenario 3 fija la capacidad en 2 de forma explícita.
- **Reconciliación y reintentos:** los experimentos fijan `RECONCILIACION_SEGUNDOS=0` y no usan reintentos, para medir los patrones sin que la saga compense sola los cobros tardíos. Con el valor por defecto (20 s), esas reservas se compensan solas: se probó con un cobro tardío, que quedó `COMPENSADA` con el pago reembolsado en unos 18 s.
- **Repeticiones:** una sola ejecución por configuración, en Docker Desktop sin límites de recursos.

## Anexo: pruebas manuales

Servicio caído (el BFF degrada) y 401/429 se probaron a mano y no están en `evidence/`. Se ejecutaron con `curl` contra el gateway y no se repitieron, así que son observaciones y no resultados reproducibles por script.

| Prueba | Cómo | Resultado observado |
|---|---|---|
| Sin token y token inválido | `curl` sin `Authorization` y con `Bearer mal` | 401 en ambos casos |
| Rate limit | `RATE_LIMIT_PER_MINUTE=8`, ráfaga de peticiones con token | 429 con `Retry-After` desde la tercera petición seguida (la cuenta incluía las anteriores) |
| Ruta no publicada | `GET /api/pagos/x` | 404 |
| Puertos internos | `curl` a 8001 a 8004 desde el host | sin respuesta |
| Pagos detenido | `docker compose stop pagos` y `GET /api/reservas/{id}/resumen` | 200, sin degradar |
| Confirmaciones detenido | `docker compose stop confirmaciones` y resumen | 200 con `degradado: true` y `confirmacion: null`, en unos 2 s (el timeout del BFF) |
| Saga detenida | `docker compose stop saga` y resumen | 503 `SAGA_NO_DISPONIBLE` |
| Reserva inexistente | resumen de un id que no existe | 404 |

