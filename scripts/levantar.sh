#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
mkdir -p data
export HABITACIONES_DB="${HABITACIONES_DB:-$ROOT/data/habitaciones.db}"
export PAGOS_DB="${PAGOS_DB:-$ROOT/data/pagos.db}"
export CONFIRMACIONES_DB="${CONFIRMACIONES_DB:-$ROOT/data/confirmaciones.db}"
export SAGA_DB="${SAGA_DB:-$ROOT/data/saga.db}"
export PASARELA_DB="${PASARELA_DB:-$ROOT/data/pasarela.db}"
export PASARELA_URL="${PASARELA_URL:-http://127.0.0.1:8004}"
export HABITACIONES_URL="${HABITACIONES_URL:-http://127.0.0.1:8001}"
export PAGOS_URL="${PAGOS_URL:-http://127.0.0.1:8002}"
export CONFIRMACION_URL="${CONFIRMACION_URL:-http://127.0.0.1:8003}"
export PASARELA_TIMEOUT="${PASARELA_TIMEOUT:-1.0}"
export SERVICES_TIMEOUT="${SERVICES_TIMEOUT:-2.5}"
export CB_FAILURE_THRESHOLD="${CB_FAILURE_THRESHOLD:-2}"
export CB_RECOVERY_SECONDS="${CB_RECOVERY_SECONDS:-4}"
export BULKHEAD_MAX_CONCURRENT="${BULKHEAD_MAX_CONCURRENT:-2}"
pids=()
cleanup() {
  echo 'Cerrando procesos de laboratorio...'
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait || true
}
trap cleanup EXIT INT TERM
for item in 'pasarela 8004' 'habitaciones 8001' 'pagos 8002' 'confirmaciones 8003' 'saga 8000'; do
  read -r service port <<< "$item"
  python -m uvicorn "src.servicios.${service}.main:app" --host 127.0.0.1 --port "$port" --workers 1 --no-access-log &
  pids+=("$!")
done
for port in 8000 8001 8002 8003 8004; do
  for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then break; fi
    if [ "$i" = 60 ]; then echo "No inicio servicio en $port" >&2; exit 1; fi
    sleep 0.2
  done
done
printf '\nServicios disponibles:\n  Saga: http://127.0.0.1:8000/docs\n  Habitaciones: http://127.0.0.1:8001/docs\n  Pagos: http://127.0.0.1:8002/docs\n  Confirmaciones: http://127.0.0.1:8003/docs\n  Pasarela: http://127.0.0.1:8004/docs\n\n'
echo 'Ejecuta en otra terminal: python scripts/experimentos.py'
wait
