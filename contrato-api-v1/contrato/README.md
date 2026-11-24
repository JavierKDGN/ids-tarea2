# Contrato API v1

Contrato OpenAPI 3.0.3 del sistema de reservas (Forma H, hoteleria). Describe lo que **ya existe** en el codigo y lo que
**falta** (Gateway y BFF), para que cualquier cambio de rutas o formatos se haga primero aqui.

| Archivo | Componente | Visible desde fuera |
|---|---|---|
| `gateway.yaml` | API Gateway (contrato publico) | Si (puerto 8080, unico) |
| `bff.yaml` | BFF web (resumen agregado) | Solo via Gateway |
| `saga.yaml` | Coordinador de la Saga | Solo via Gateway |
| `habitaciones.yaml` | Servicio Habitaciones | No (solo `/disponibilidad` via Gateway) |
| `pagos.yaml` | Servicio Pagos | No |
| `confirmaciones.yaml` | Servicio Confirmaciones | No |
| `pasarela.yaml` | Pasarela externa simulada | No (solo la usa Pagos) |
| `common.yaml` | Esquemas, parametros y respuestas compartidos | - |

Cada operacion lleva `x-estado`: `implementado` (coincide con el codigo), `ajustar` (existe pero hay que cambiarla,
ver `x-ajuste`) o `nuevo` (no existe aun). Para verlos: Swagger Editor (editor.swagger.io, abrir cada archivo),
o `npx @redocly/cli lint contrato/gateway.yaml`. Los `$ref` entre archivos son relativos, asi que hay que mantener la carpeta junta.

## Convenciones

| Tema | Regla |
|---|---|
| Identificadores | `id_reserva` (lo genera la Saga si el cliente no lo envia: `res-{12 hex}`), `id_pago = pay-{id_reserva}`, `id_voucher = voucher-{id_reserva}`. Se mantienen los nombres del codigo (no `reserva_id`). |
| Fechas | `YYYY-MM-DD`. Estadia `[fecha_inicio, fecha_fin)`: salida y entrada el mismo dia no se solapan; `fecha_inicio < fecha_fin`. |
| Montos | String decimal con 2 decimales (`"125.50"`); Pagos persiste `monto_centavos` (entero). Moneda unica, no se transmite. |
| Correlacion | `X-Correlation-ID` en toda llamada; el Gateway lo genera si falta; todos lo registran en logs. |
| Idempotencia | Cliente: `Idempotency-Key` (+ `id_reserva` opcional). Entre servicios: `id_reserva`, `id_pago`, `id_voucher` deterministas. |
| Errores | `{ "error", "code", "message" }`. Excepcion: la Pasarela (tercero) mantiene `{"detail": ...}`. |
| Decision de Pagos | El cliente decide por el campo `estado`, no por el codigo HTTP (un 201/202/503 puede traer cualquier estado coherente). |

## Puertos, nombres DNS y variables de entorno

| Servicio | DNS interno | Puerto | Variables que consume |
|---|---|---|---|
| gateway | `gateway` | **8080 (publicado)** | `SAGA_URL`, `BFF_URL`, `HABITACIONES_URL`, `GATEWAY_TOKENS`, `RATE_LIMIT_PER_MINUTE`, `GATEWAY_TIMEOUT_POST`, `GATEWAY_TIMEOUT_GET` |
| bff | `bff` | 8005 | `SAGA_URL`, `HABITACIONES_URL`, `CONFIRMACION_URL`, `BFF_TIMEOUT` |
| saga | `saga` | 8000 | `SAGA_DB`, `HABITACIONES_URL`, `PAGOS_URL`, `CONFIRMACION_URL`, `SERVICES_TIMEOUT` |
| habitaciones | `habitaciones` | 8001 | `HABITACIONES_DB` |
| pagos | `pagos` | 8002 | `PAGOS_DB`, `PASARELA_URL`, `PASARELA_TIMEOUT`, `CB_FAILURE_THRESHOLD`, `CB_RECOVERY_SECONDS`, `BULKHEAD_MAX_CONCURRENT` |
| confirmaciones | `confirmaciones` | 8003 | `CONFIRMACIONES_DB` |
| pasarela | `pasarela` | 8004 | `PASARELA_DB` |

En Docker, la URL de cada servicio es `http://<dns>:<puerto>` (ej. `HABITACIONES_URL=http://habitaciones:8001`).
Ojo: la variable de la Saga es `CONFIRMACION_URL` (singular), tal como esta en el codigo.
Las bases SQLite necesitan un volumen por servicio.

## Catalogo de codigos de error (`code`)

| Codigo | HTTP | Donde |
|---|---|---|
| `VALIDACION_FALLIDA` | 422 | todos |
| `RECURSO_NO_ENCONTRADO` | 404 | todos |
| `ERROR_INTERNO` | 500 | todos |
| `IDEMPOTENCY_CONFLICT` | 409 | Saga, Habitaciones, Confirmaciones, Pagos |
| `HABITACION_NO_DISPONIBLE` | 409 | Habitaciones |
| `RESERVA_YA_LIBERADA` | 409 | Habitaciones |
| `VOUCHER_YA_ANULADO` | 409 | Confirmaciones |
| `CONFIRMACION_NO_DISPONIBLE` | 500 | Confirmaciones (fallo forzado) |
| `REEMBOLSO_INSEGURO` | 409 | Pagos |
| `RESET_NO_PERMITIDO` | 409 | Pagos |
| `PASARELA_NO_CONFIRMO` | 502 | Pagos |
| `REEMBOLSO_PENDIENTE` | 503 | Pagos |
| `SAGA_NO_DISPONIBLE` | 503 | BFF |
| `NO_AUTORIZADO` | 401 | Gateway |
| `LIMITE_EXCEDIDO` | 429 | Gateway |
| `UPSTREAM_NO_DISPONIBLE` | 503 | Gateway |
| `UPSTREAM_TIMEOUT` | 504 | Gateway |

## Cambios que el contrato exigia al codigo actual

Estado a 2026-11-23:

1. **Formato de error unico** (`{error, code, message}`): **hecho** (`src/common/errores.py`). Lo usan Gateway, BFF, Saga,
   Habitaciones, Confirmaciones y Pagos; la Pasarela, por ser un tercero, conserva `{"detail": ...}`. El Gateway
   reenvia los cuerpos de los servicios tal como llegan.
2. **`X-Correlation-ID`** leido, registrado y propagado: **hecho** (`src/common/correlacion.py`).
3. **Saga `GET /reservas/{id}`** responde 200 siempre que exista: **hecho**.
4. **Pagos `POST /pagos/{id}/reembolsar`** responde 502 `PASARELA_NO_CONFIRMO` si la pasarela contesta con error HTTP: **hecho**.
5. **Retirar** el alias `POST /api/reservar` de la Saga y **reemplazar** `bff/main.py` (borrar `bff/saga.py`): **hecho**.
6. **Nuevos**: Gateway, BFF `resumen` y `docker-compose.yml`: **hechos**. En lugar de un `Dockerfile` por servicio hay uno
   solo, comun, que elige el modulo con `SERVICIO` y `PORT`.

## Decisiones adoptadas

| Tema | Decision | Por que |
|---|---|---|
| Autenticacion | Token Bearer estatico por cliente (`GATEWAY_TOKENS`), no JWT | El enunciado pide que el gateway valide un token en la entrada; no exige JWT. Se verifico: sin token o con token invalido responde 401. |
| Rate limit | 60 peticiones por minuto por cliente (`RATE_LIMIT_PER_MINUTE`) | Valor por defecto del gateway; supera la carga normal de una demo. Los experimentos lo suben a 5000. Se verifico el 429 con `Retry-After`. |
| Timeouts del gateway | 10 s en POST y 4 s en GET | La saga hace hasta tres pasos mas compensaciones (unos 0,9 s sin fallas); el BFF espera como maximo 2 s por fuente. |
| Puertos | Gateway 8080 (unico publicado) y BFF 8005 (interno) | Cumple "solo el gateway se expone". Se verifico que los puertos internos no responden desde el host. |
| Timeout del BFF | `BFF_TIMEOUT` de 2,0 s por fuente | Con una fuente caida el resumen responde en unos 2 s, degradado. |
| Resumen del BFF | Agrega saga, habitaciones y confirmaciones; no incluye Pagos | Cumple "agregar al menos dos servicios" y sigue funcionando con Pagos caido (verificado deteniendo Pagos). |
| Destino no disponible | El gateway responde 503 (y 504 si vence el timeout), no 502 | El 502 queda reservado para la saga `COMPENSADA`, asi no se confunden. |
| Endpoints de demo | `/chaos`, `/metricas` y `/demo/*` no se publican; se usan con `docker compose exec` | Provocar fallas no debe estar disponible desde el exterior. Los experimentos funcionan asi. |
