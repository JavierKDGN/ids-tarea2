# Imagen unica para todos los servicios: cada contenedor elige su modulo con SERVICIO y su puerto con PORT.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Las bases SQLite viven en /data (volumen por servicio)
RUN mkdir -p /data

ENV SERVICIO=saga \
    PORT=8000

CMD ["sh", "-c", "exec python -m uvicorn src.servicios.${SERVICIO}.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log"]
