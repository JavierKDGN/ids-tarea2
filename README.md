# ids-tarea2

Reserva de una habitación de hotel (Forma H) como sistema distribuido: API Gateway, BFF, Saga orquestada y resiliencia (timeout, Circuit Breaker y Bulkhead) sobre el servicio de Pagos.

## Arquitectura

```
Cliente -> Gateway :8080 -> BFF :8005 (resumen, solo lectura)
                  |
                  +------> Saga :8000 -> Habitaciones :8001
                  |                  -> Pagos :8002 -> Pasarela :8004 (externa simulada)
                  |                  -> Confirmaciones :8003
                  +------> Habitaciones (disponibilidad)
```

- Solo el gateway publica un puerto al host. Todo lo demás vive en la red interna de Docker.
- Cada servicio tiene su propia base SQLite en su propio volumen.
- La reserva es una Saga: reservar habitación, cobrar la garantía, emitir la confirmación. Si un paso falla, compensa en orden inverso (anular voucher, reembolsar, liberar).
- Timeout, Circuit Breaker y Bulkhead protegen la llamada de Pagos a la pasarela, que es la dependencia frágil.

| Carpeta o archivo                                        | Contenido                                                                    |
| -------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `src/servicios/`                                       | Gateway, BFF, saga, habitaciones, pagos, confirmaciones y pasarela (FastAPI) |
| `src/common/correlacion.py`                            | Middleware de `X-Correlation-ID` y log por request                         |
| `contrato-api-v1/contrato/`                            | Contrato OpenAPI de cada componente y sus decisiones                         |
| `docker-compose.yml`, `Dockerfile`, `.env.example` | Despliegue                                                                   |
| `tests/`                                               | Tests de integración                                                        |
| `experimentos/`, `evidence/docker/`                  | Scripts, CSV, gráficos y logs de los escenarios                             |
| `docs/experimentacion.md`                              | Qué se probó y qué significa el resultado                                 |

## Setup

Requisitos: Docker y Docker Compose.

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

Los 7 contenedores deben quedar `healthy`. El gateway queda en `http://localhost:8080` (cambiar con `GW_HOST_PORT` en `.env` si el puerto está ocupado). Los datos persisten en volúmenes; `docker compose down -v` los borra.

Variables principales (`.env.example`):

| Variable                    | Qué controla                                         | Valor por defecto   |
| --------------------------- | ----------------------------------------------------- | ------------------- |
| `GATEWAY_TOKENS`          | Tokens válidos, `cliente:token` separados por coma | `demo:demo-token` |
| `RATE_LIMIT_PER_MINUTE`   | Límite por cliente en el gateway                     | 60                  |
| `PASARELA_TIMEOUT`        | Timeout de Pagos hacia la pasarela (s)                | 1.0                 |
| `CB_FAILURE_THRESHOLD`    | Fallos seguidos para abrir el Circuit Breaker         | 2                   |
| `CB_RECOVERY_SECONDS`     | Espera antes de probar de nuevo (s)                   | 4                   |
| `BULKHEAD_MAX_CONCURRENT` | Llamadas simultáneas a la pasarela                   | 2                   |
| `SERVICES_TIMEOUT`        | Timeout de la saga hacia cada servicio (s)            | 2.5                 |
| `BFF_TIMEOUT`             | Espera del BFF por cada fuente (s)                    | 2.0                 |

## Uso

Todas las rutas públicas exigen `Authorization: Bearer demo-token`.

Reservar (bash):

```bash
curl -X POST http://localhost:8080/api/reservas \
  -H "Authorization: Bearer demo-token" -H "Content-Type: application/json" \
  -H "Idempotency-Key: reserva-001" -H "X-Correlation-ID: reserva-001" \
  -d '{"habitacion_id":101,"fecha_inicio":"2027-05-01","fecha_fin":"2027-05-03","monto":"125.50","id_reserva":"reserva-001"}'
```

Reservar (PowerShell; `curl` ahí es un alias de `Invoke-WebRequest`):

```powershell
$h = @{ Authorization = 'Bearer demo-token'; 'Idempotency-Key' = 'reserva-001'; 'X-Correlation-ID' = 'reserva-001' }
Invoke-RestMethod -Method Post -Uri http://localhost:8080/api/reservas -Headers $h -ContentType 'application/json' -Body '{"habitacion_id":101,"fecha_inicio":"2027-05-01","fecha_fin":"2027-05-03","monto":"125.50","id_reserva":"reserva-001"}'
```

Otras rutas (mismo header de autorización):

| Ruta                                                                                                    | Para qué                                                  |
| ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `GET /api/reservas/{id}`                                                                              | Estado de la saga                                          |
| `GET /api/reservas/{id}/resumen`                                                                      | Vista agregada del BFF (saga, habitación y confirmación) |
| `POST /api/reservas/{id}/reconciliar`                                                                 | Reanudar una reserva pendiente                             |
| `GET /api/habitaciones/disponibilidad?habitacion_id=101&fecha_inicio=2027-05-01&fecha_fin=2027-05-03` | Disponibilidad                                             |

Códigos de la reserva: 201 `CONFIRMADA`, 200 si es un reintento idempotente, 502 `COMPENSADA`, 409 `FALLIDA` o conflicto, 202 si queda pendiente. El gateway responde 401 sin token, 429 si se excede el límite, 503 si un servicio no está disponible y 504 si vence el timeout.

### Probar las políticas del gateway

Sin token o con un token inválido responde 401:

```bash
curl -i http://localhost:8080/api/reservas/x
curl -i -H "Authorization: Bearer malo" http://localhost:8080/api/reservas/x
```

Rate limit (60 por minuto por cliente): una ráfaga de 70 peticiones devuelve 60 respuestas normales (404, porque `x` no existe) y luego 429 con `Retry-After`. Esperar un minuto para que la ventana se vacíe.

```bash
for i in $(seq 1 70); do curl -s -o /dev/null -w "%{http_code} " -H "Authorization: Bearer demo-token" http://localhost:8080/api/reservas/x; done
```

```powershell
$h = @{ Authorization = 'Bearer demo-token' }
1..70 | ForEach-Object { try { (Invoke-WebRequest -Uri http://localhost:8080/api/reservas/x -Headers $h -UseBasicParsing).StatusCode } catch { [int]$_.Exception.Response.StatusCode } }
```

Si el puerto 8080 está ocupado en tu máquina, poner `GW_HOST_PORT=8088` en `.env` (o `$env:GW_HOST_PORT=8088` en PowerShell) y usar ese puerto en todos los comandos.

## Cómo provocar cada escenario

Las fallas se activan sin editar código, con `POST /chaos` dentro de la red (no se publica). Este comando funciona en bash y en PowerShell; cambiar el servicio, el puerto y el cuerpo según el caso:

```
docker compose exec -T pasarela python -c "import urllib.request as u, json; print(u.urlopen(u.Request('http://127.0.0.1:8004/chaos', json.dumps({'latencia_segundos': 6, 'forzar_error': False}).encode(), {'Content-Type': 'application/json'})).read().decode())"
```

| Servicio           | Puerto interno | Cuerpo de `/chaos`                                       |
| ------------------ | -------------- | ---------------------------------------------------------- |
| `pasarela`       | 8004           | `{'latencia_segundos': N, 'forzar_error': True o False}` |
| `confirmaciones` | 8003           | `{'forzar_error': True o False}`                         |

**Escenario 1: camino feliz.** Reservar con el comando de arriba. Resultado: 201 `CONFIRMADA`. Repetir la misma petición devuelve 200 sin duplicar. `GET /api/reservas/reserva-001/resumen` muestra habitación `RESERVADA` y voucher `EMITIDO`.

**Escenario 2: falla del último paso.** Poner `forzar_error: True` en `confirmaciones`, reservar y volver a `False`. Resultado: 502 `COMPENSADA`, con el pago `REEMBOLSADO` y la habitación `LIBERADA`. En los logs se ve el orden: reembolso y luego liberación.

**Escenario 3: pasarela lenta y caída.**

1. Lenta: `latencia_segundos: 6` en `pasarela` y varias reservas simultáneas. Pagos corta a 1 s, el Bulkhead rechaza el exceso y las reservas se compensan o quedan `PENDIENTE_PAGO`.
2. Caída: `forzar_error: True` en `pasarela` y varias reservas seguidas. El Circuit Breaker abre y las siguientes responden sin llamar a la pasarela.
3. Ver el estado del breaker y del bulkhead:

```
docker compose exec -T pagos python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8002/metricas').read().decode())"
```

4. `GET /api/habitaciones/disponibilidad` y el resumen del BFF siguen respondiendo mientras Pagos tiene problemas.

Para repetir los escenarios con medición y evidencia:

```bash
pip install -r experimentos/requirements.txt
python experimentos/exp_feliz_compensacion.py
python experimentos/exp_escenario3.py
python experimentos/graficar_escenario3.py
```

El resultado queda en `evidence/docker/` y `experimentos/`, y se explica en [docs/experimentacion.md](docs/experimentacion.md).

## Observabilidad

Cada servicio escribe una línea por request con el `X-Correlation-ID`. Con un solo id se sigue una reserva por todos los contenedores:

```bash
docker compose logs --no-log-prefix | grep reserva-001
```

Si el cliente no envía el header, el gateway genera uno y lo devuelve en la respuesta.

## Tests

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # en Windows: .venv\Scripts\pip
.venv/bin/python -m pytest tests               # en Windows: .venv\Scripts\python
```

Cubren camino feliz, idempotencia, solape de fechas, compensación, bulkhead con Circuit Breaker y timeout de pago sin cobro huérfano.

## Decisiones

1. Python, FastAPI, SQLite: la tipica
2. SAGA orquestada: Flujo lineal de 3 pasos (Habitaciones -> Pagos -> Confirmacion), el orquestador centraliza el control del estado y sabe en que paso exacto falla
3. 2PC vs SAGA: 2PC bloquea hasta recibir respuestas ralentizando los otros procesos y si un servicio se cae, todo el sistema tambien. Ademas al conectarlo con una pasarela externa (en este caso el servicio de pagos) no puede controlarlos, mientras que SAGA puede tomar acciones a nivel de aplicacion (por ejemplo reembolsar)
4. Gateway sin lógica de negocio y BFF de solo lectura: el gateway autentica, limita y enruta; el BFF compone la respuesta para el cliente web. Las decisiones del gateway están en `contrato-api-v1/contrato/README.md`.
5. Una sola imagen Docker para todos los servicios: cada contenedor elige su módulo con `SERVICIO` y su puerto con `PORT`.

## Uso de IA

- Ayuda para armar rapidamente los microservicios base (Servicios habitacion, pagos y confirmacion)
- Ayudó con el gateway, el BFF, Docker, la correlación de logs, los experimentos y la documentación.
