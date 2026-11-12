# ids-tarea2


## Decisiones

1. Python, FastAPI, SQLite: la tipica
2. SAGA orquestada: Flujo lineal de 3 pasos (Habitaciones -> Pagos -> Confirmacion), el orquestador centraliza el control del estado y sabe en que paso exacto falla
3. 2PC vs SAGA: 2PC bloquea hasta recibir respuestas ralentizando los otros procesos y si un servicio se cae, todo el sistema tambien. Ademas al conectarlo con una pasarela externa (en este caso el servicio de pagos) no puede controlarlos, mientras que SAGA puede tomar acciones a nivel de aplicacion (por ejemplo reembolsar)

## Uso de IA

- Ayuda para armar rapidamente los microservicios base (Servicios habitacion, pagos y confirmacion)