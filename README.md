# EMSafe Edge Industrial

Nodo **Edge de planta** del segmento industrial de EMSafe. Recibe por **BLE** las lecturas del
dosímetro de cada operario, las valida, deduplica y guarda, y las publica por **MQTT** hacia la
ingesta de **Device Management**. También recibe de la nube el umbral definido por el responsable
de SSO y lo escribe en el dosímetro.

```
Dosímetro (ESP32) --BLE--> Edge industrial --MQTT--> Broker --> Ingesta Device Management
                                 ^                                   (Spring Boot)
                                 +------ config (umbral) <-- Device Management
```

El Edge **no** evalúa límites ni genera alertas, **no** controla actuadores y **no** calcula dosis.
La alarma local es del firmware; las alertas en la nube, de Personal Dosimetry y Occupational Health.

## Contenido

- [Arquitectura](#arquitectura)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Configuración](#configuración)
- [Ejecución](#ejecución)
- [Prueba manual con el dosímetro y Mosquitto](#prueba-manual-con-el-dosímetro-y-mosquitto)
- [Limitaciones conocidas](#limitaciones-conocidas)
- [Tests y CI](#tests-y-ci)
- [Contrato MQTT](#contrato-mqtt)

---

## Arquitectura

Un proceso Python con `asyncio`. DDD con un bounded context, **`device_gateway`**. Los puertos son
`Protocol` en `domain/ports.py` y se inyectan por constructor, así los tests reemplazan BLE, broker
y SQLite por dobles.

```text
device_gateway/
  domain/          Reading, OutboxMessage, DeviceConfig, value objects (ReadingIdentity,
                   MagneticFluxDensity, SyncOrigin, ReadingQuality, CapturedAtSource),
                   ReadingFactory, BatchPolicy y los puertos
  application/     ingest_reading, publish_outbox, apply_device_config,
                   track_connectivity, purge_outbox
  infrastructure/  ble_protocol (bytes <-> datos, sin bleak), ble_dosimeter_client (bleak),
                   mqtt_payloads (esquema 1.0), mqtt_client (paho), models, sqlite_outbox,
                   sqlite_config_store
  interfaces/      runner (arma y orquesta todo)
shared/            config (variables de entorno), database (SQLite en modo WAL), clock
tests/             unittest con dobles
main.py            arranque, logging y señales
```

### Flujo de una lectura

```text
notificación BLE ──> cola asyncio ──> IngestReading ──> outbox SQLite (PENDING)
 (solo decodifica)                    valida, marca       │
                                      saturada, dedupe    ▼
                                                     PublishOutbox: arma lotes (BATCHED, con messageId)
                                                          │  publica QoS 1 y espera el PUBACK
                                                          ▼
                                                     PUBLISHED (se conserva 7 días para deduplicar)
```

- **BLE:** un cliente por dosímetro. Escanea por nombre (y por UUID de servicio si viene en el
  anuncio) y al conectar ejecuta la secuencia *notify → umbral → hora*. El callback solo decodifica
  y encola, porque el dosímetro vacía su buffer a una lectura cada 15 ms. Reintenta a 1, 2, 4… hasta
  30 s; tras una sesión, reconecta en 1 s.
- **Ingesta:** descarta valores no numéricos, NaN, infinitos o negativos, marca `SATURATED` desde
  `EMSAFE_SATURATION_UT` y deduplica por `deviceId + sequence + ts crudo`.
- **Outbox:** una lectura se marca publicada **solo tras el PUBACK**. El publicador corre cada
  segundo, al instante con una lectura en tiempo real y al reconectar el broker, y se detiene en el
  primer fallo para no desordenar.
- **MQTT** (paho 2.x, MQTT v5): Last Will registrado antes de conectar y `clean_start`, porque la
  entrega la asegura el outbox. Sin conexión no publica telemetría. Al reconectar republica el
  estado del Edge y de cada dosímetro.
- **Config:** guarda en SQLite la versión válida más nueva y escribe el umbral al conectar y, si el
  dosímetro ya está conectado, en caliente.
- **Limpieza:** cada hora borra las lecturas publicadas hace más de `EMSAFE_OUTBOX_RETENTION_DAYS`
  días. Las pendientes o sin PUBACK **nunca** se borran por antigüedad.
- **Apagado** (Ctrl+C o SIGTERM): cierra BLE (dosímetros `OFFLINE`), detiene las tareas y publica
  `OFFLINE` del Edge antes de desconectar. Lo no publicado queda en el outbox. Un segundo Ctrl+C
  fuerza la salida.

## Requisitos

Preparación desde cero en Windows.

### Python y Bluetooth

- Python 3.10 o superior ([python.org](https://www.python.org/downloads/)). Al instalarlo, marca
  *Add python.exe to PATH*.
- Laptop con Bluetooth LE, activado en *Configuración → Bluetooth y dispositivos*.

### Mosquitto

1. Descarga el instalador de Windows desde
   [mosquitto.org/download](https://mosquitto.org/download/) e instálalo con las opciones por
   defecto. Queda como servicio *Mosquitto Broker* en el puerto 1883.
2. Comprueba que el servicio esté en ejecución con `sc query mosquitto` en cmd. Si no dice
   `RUNNING`, inícialo desde `services.msc` o con `net start mosquitto` en un cmd de administrador.
3. Agrega `C:\Program Files\mosquitto` al `PATH`: busca *Editar las variables de entorno del
   sistema* → *Variables de entorno* → `Path` → *Nuevo*.
4. Abre **dos ventanas nuevas de cmd**. Las terminales que ya estaban abiertas no ven el `PATH`
   nuevo.

   En la primera:

   ```bat
   mosquitto_sub -t prueba -v
   ```

   En la segunda:

   ```bat
   mosquitto_pub -t prueba -m hola
   ```

   La primera debe mostrar `prueba hola`.

### MQTT Explorer (opcional, recomendado)

Cliente gráfico para ver lo que publica el Edge y publicar la config sin pelear con las comillas.

1. Descárgalo desde [mqtt-explorer.com](https://mqtt-explorer.com/) o desde Microsoft Store.
2. Crea una conexión con host `localhost`, puerto `1883`, sin usuario ni contraseña y con
   *Encryption (tls)* desactivado. Conéctate.
3. **Ver la telemetría:** en el árbol de la izquierda abre `iot → EMSafe-DOS-01 → telemetry`. El
   panel derecho muestra el último mensaje con el JSON formateado y el historial de valores.
4. **Publicar la config:** en la sección *Publish* del panel derecho:
   - escribe el tópico `iot/EMSafe-DOS-01/config`;
   - elige el formato `json` y pega el payload (ver [Umbral desde la nube](#umbral-desde-la-nube));
   - marca **retain**, elige QoS 1 y pulsa *Publish*.

### Dosímetro

Un ESP32 con el firmware `emsafe_dosimetro` cargado. El firmware está en el repo
[`emsafe-edge-flask`](https://github.com/Aquitectura-Emergente/emsafe-edge-flask/tree/main/firmware/emsafe_dosimetro), en `firmware/emsafe_dosimetro`, y solo usa librerías incluidas en el paquete
esp32. Para cargarlo:

1. Instala el driver USB de la placa. El prototipo usa un **CP2102**: driver *CP210x VCP* de
   Silicon Labs.
2. Instala [Arduino IDE](https://www.arduino.cc/en/software). En *Boards Manager* instala el
   paquete **esp32** de Espressif Systems.
3. Abre `firmware/emsafe_dosimetro/emsafe_dosimetro.ino` y configura *Tools*:
   - *Board*: **ESP32 Dev Module**;
   - *Partition Scheme*: **Huge APP (3MB No OTA/1MB SPIFFS)**;
   - *Port*: el COM que aparece al conectar la placa.
4. Pulsa *Upload*. En el *Serial Monitor* (115200 baudios) se ven los mensajes del dosímetro. Se
   anuncia por BLE como `EMSafe-DOS-01`.

## Instalación

```powershell
cd emsafe-edge-industrial
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

En Linux o macOS: `python3 -m venv .venv` y `.venv/bin/pip install -r requirements.txt`.

Dependencias: `bleak`, `paho-mqtt` 2.x y `peewee`. Los tests usan `unittest` de la librería
estándar.

## Configuración

Todo se configura por variables de entorno, incluidas las credenciales. Un valor inválido detiene
el arranque con un mensaje que nombra la variable.

| Variable | Uso | Default |
|---|---|---|
| `EMSAFE_EDGE_NODE_ID` | Identificador del nodo Edge | `EDGE-PLANTA-01` |
| `EMSAFE_ORGANIZATION_ID` | Organización del nodo | `org-demo` |
| `EMSAFE_DEVICE_NAMES` | Nombres BLE de los dosímetros, separados por coma | `EMSafe-DOS-01` |
| `EMSAFE_DEFAULT_THRESHOLD_UT` | Umbral que recibe el dosímetro si no llegó config de la nube | `300` |
| `EMSAFE_SATURATION_UT` | Desde qué valor una lectura se marca `SATURATED` | `790` |
| `EMSAFE_MQTT_HOST` | Broker MQTT | `localhost` |
| `EMSAFE_MQTT_PORT` | Puerto del broker | `1883` |
| `EMSAFE_MQTT_TLS` | Activa TLS (`true`/`false`) | `false` |
| `EMSAFE_MQTT_USERNAME` / `EMSAFE_MQTT_PASSWORD` | Credenciales del nodo | vacío |
| `EMSAFE_DB_PATH` | Archivo SQLite del outbox y la config | `edge_industrial.db` |
| `EMSAFE_OUTBOX_RETENTION_DAYS` | Días que se conservan las lecturas ya publicadas (mínimo 1) | `7` |
| `EMSAFE_LOG_LEVEL` | Nivel de log (`DEBUG`, `INFO`, `WARNING`…) | `INFO` |

El umbral por defecto debe ser mayor que 0 y menor que `EMSAFE_SATURATION_UT`.

## Ejecución

```powershell
cd emsafe-edge-industrial
.\.venv\Scripts\python.exe main.py
```

Por defecto se conecta a `localhost:1883` y busca `EMSafe-DOS-01`. Para cambiar algo:

```powershell
$env:EMSAFE_MQTT_HOST = "broker.planta.local"
$env:EMSAFE_LOG_LEVEL = "DEBUG"
.\.venv\Scripts\python.exe main.py
```

`edge_industrial.db` (y sus `-wal`/`-shm`) se crea en la carpeta desde la que se ejecuta y está en
`.gitignore`.

## Prueba manual con el dosímetro y Mosquitto

En Windows, con Mosquitto instalado como **servicio nativo** (*Mosquitto Broker*) y
`mosquitto_sub`/`mosquitto_pub` en el `PATH`. Sin configuración, Mosquitto 2.x escucha solo en
`localhost`, que alcanza para esta prueba.

### Preparación

1. Comprueba que el servicio *Mosquitto Broker* esté en ejecución (`services.msc`, o
   `sc query mosquitto` en cmd).
2. Enciende el dosímetro y el Bluetooth de la laptop.
3. Cierra el Edge hogar u otra app conectada al dosímetro: el firmware acepta **una sola conexión
   BLE**.

### Paso a paso

**Terminal 1 — ver lo que publica el Edge:**

```bat
mosquitto_sub -t "iot/#" -t "edge/#" -v
```

**Terminal 2 — Edge:**

```powershell
cd emsafe-edge-industrial
.\.venv\Scripts\python.exe main.py
```

**Qué se espera:**

- En el log: `Conectado al broker MQTT` y luego `Conectado a EMSafe-DOS-01 (umbral 300.0 µT, ...)`,
  con una advertencia de umbral por defecto mientras no llegue config. El LED del dosímetro se pone
  verde.
- En la terminal 1: `edge/EDGE-PLANTA-01/status` con `ONLINE`, `iot/EMSafe-DOS-01/status` con
  `"connectivity":"ONLINE"` y un `iot/EMSafe-DOS-01/telemetry` por segundo con `origin` `REALTIME`.
- Si el dosímetro tenía lecturas guardadas, primero llegan lotes de hasta 50 con `origin`
  `DEFERRED` y `capturedAtSource` `DEVICE_CLOCK`.

### Umbral desde la nube

Para publicar la config usa **MQTT Explorer** (conexión a `localhost:1883`, tópico
`iot/EMSafe-DOS-01/config`, QoS 1, *retain* marcado) o **cmd**. No uses PowerShell: altera las
comillas del JSON al pasarlo a `mosquitto_pub` y el Edge recibe un JSON roto.

En **cmd**:

```bat
mosquitto_pub -r -q 1 -t iot/EMSafe-DOS-01/config -m "{\"schemaVersion\":\"1.0\",\"deviceId\":\"EMSafe-DOS-01\",\"configVersion\":1,\"alertThresholdUT\":200.0,\"definedBy\":\"SSO_RESPONSIBLE\",\"updatedAt\":\"2026-11-12T14:00:00Z\"}"
```

En el log aparece `Config de EMSafe-DOS-01 aplicada` y `Umbral 200.0 µT escrito en EMSafe-DOS-01`, y
el buzzer suena desde 200 µT. Para comprobar las reglas:

- Reenviar el mismo `configVersion` → se ignora.
- `alertThresholdUT` 800 (≥ saturación) → se rechaza y se conserva la vigente.
- Borrar el retained (`mosquitto_pub -r -t iot/EMSafe-DOS-01/config -n`) → el Edge conserva la
  config que tiene guardada.

### Pruebas con imán

Usa **un solo imán**, con el polo hacia el sensor, y acércalo **en línea recta**. Con varios imanes, o
con un imán que gira, los campos se pueden restar y la lectura puede *bajar* al acercarlo. Ver
también el campo residual en [Limitaciones conocidas](#limitaciones-conocidas).

- Lejos: valores bajos, `quality` `VALID`.
- Muy cerca: el firmware reporta 800 µT o más al saturar el magnetómetro, y el Edge publica la
  lectura con `quality` `SATURATED` (desde 790 µT).

### Corte de nube

1. Detén el servicio *Mosquitto Broker* (`services.msc` → Detener, o `net stop mosquitto` en un cmd
   de administrador). El Edge registra la desconexión y sigue guardando lecturas en el outbox.
2. Espera un rato y vuelve a iniciarlo (`net start mosquitto`).
3. Vuelve a abrir el `mosquitto_sub` de la terminal 1, que se cierra al caer el broker. El Edge se
   reconecta solo y publica las acumuladas, en orden y sin huecos en `sequence`.

### Corte de BLE

1. Apaga el Bluetooth de la laptop unos 60 s, o apaga el Edge con Ctrl+C. El dosímetro sigue
   midiendo y guardando (LED azul) y se publica `OFFLINE` en `iot/EMSafe-DOS-01/status`.
2. Reactiva el Bluetooth o vuelve a arrancar el Edge: `ONLINE`, luego las diferidas con
   `DEVICE_CLOCK` sin duplicados ni huecos, y después tiempo real.

### Apagado

- Ctrl+C en la terminal del Edge publica `OFFLINE` del dosímetro y del Edge.
- Si el proceso muere sin despedirse (cerrar la ventana, matar el proceso), el broker publica el
  Last Will `{"status":"OFFLINE"}` en `edge/EDGE-PLANTA-01/status` en unos 45 s (keepalive de 30 s).

### Revisar el outbox

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('edge_industrial.db'); print(c.execute('select status, count(*) from outbox_readings group by status').fetchall())"
```

Con el broker conectado, todo debería quedar en `PUBLISHED`. `PENDING` o `BATCHED` indican lecturas
que esperan publicación o PUBACK.

## Limitaciones conocidas

- **Reinicios del dosímetro sin hora.** Antes de la primera sincronización de hora, el firmware
  guarda las lecturas con `ts = 0`. El Edge las publica con `capturedAtSource = EDGE_RECEIVED` y
  `capturedAt` igual a la hora de recepción, así que todas las de un mismo vaciado del buffer salen
  **con la misma hora** (o casi). Además, si el dosímetro se reinicia más de una vez sin conectarse a
  un Edge, su buffer puede tener lecturas de varios arranques con el mismo `sequence` y `ts = 0`:
  solo se conserva la primera, y el Edge registra una advertencia cuando `sequence` retrocede. Se
  resolverá con un `bootId` en el firmware (cambio futuro del contrato BLE).
- **Sensor del prototipo.** El magnetómetro (QMC5883L) mide **campo estático**, no la componente
  ELF, y satura en unos 800 µT: al saturar, el firmware reporta 800 µT o más y el Edge marca
  `SATURATED` desde 790 µT. El firmware publica la magnitud del vector menos el campo base, así que
  `SATURATED` es una heurística sobre la magnitud, no la saturación de un eje. Si se usa el
  HMC5883L que admite el firmware, hay que ajustar `EMSAFE_SATURATION_UT` a su rango.
  Una lectura `SATURATED` significa que el campo fue **de al menos ese valor, posiblemente mayor**
  (ver [Filtrado](#telemetría--iotdeviceidtelemetry) en el contrato).
- **Campo residual.** Un imán muy cerca puede magnetizar el sensor y dejar un desplazamiento que
  persiste. Solo se corrige **reiniciando el dosímetro lejos de imanes**, para que vuelva a medir el
  campo base.
- **Un nombre BLE por dosímetro.** El `deviceId` es el nombre BLE. El firmware del prototipo tiene
  el nombre fijo `EMSafe-DOS-01`: para varios dosímetros en un mismo Edge hacen falta firmwares con
  nombres distintos, listados en `EMSAFE_DEVICE_NAMES`.
- **Resolución de 1 s** en `capturedAt`, porque el `ts` del firmware está en segundos.
- **Retención de deduplicación.** Pasados `EMSAFE_OUTBOX_RETENTION_DAYS` días, una lectura
  publicada se borra y su identidad se olvida. El buffer del dosímetro dura unos 30 minutos, así que
  en la práctica no llegan reenvíos tan viejos.
- `firmwareVersion` y `batteryLevel` se publican en `null` hasta que el firmware los exponga.

## Tests y CI

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -b -v
.\.venv\Scripts\python.exe -m compileall -q main.py device_gateway shared tests
```

Los tests usan dobles y SQLite en memoria, sin BLE ni broker. `tests/test_contract_payload.py`
compara el JSON que genera el código con los ejemplos de [Contrato MQTT](#contrato-mqtt). El CI
corre lo mismo en Python 3.11 en cada push y pull request.

---

## Contrato MQTT

Versión del esquema **1.0** (revisión 1.0.1: aclaraciones sin cambio de formato). Es el contrato
compartido con el contenedor de ingesta del backend. Cualquier cambio sube la versión y se avisa
al equipo de backend.

> El test `tests/test_contract_payload.py` lee el **primer bloque JSON de esta sección** (el
> ejemplo de telemetría) y lo compara con lo que genera el código. Si cambias el ejemplo o el
> serializador, cambia los dos.

### Tópicos

| Tópico | Sentido | QoS | Retained | Publica | Consume |
|---|---|---|---|---|---|
| `iot/{deviceId}/telemetry` | Edge → nube | 1 | No | Edge | Ingesta (`MqttTelemetrySubscriber`) |
| `iot/{deviceId}/status` *(propuesto)* | Edge → nube | 1 | Sí | Edge | Ingesta (conectividad del dispositivo) |
| `iot/{deviceId}/config` *(propuesto)* | Nube → Edge | 1 | Sí | Device Management | Edge |
| `edge/{edgeNodeId}/status` *(propuesto)* | Edge → nube | 1 | Sí (LWT) | Edge / broker | Ingesta (monitoreo de nodos) |

`deviceId` es el identificador del dispositivo registrado en Device Management (prototipo:
`EMSafe-DOS-01`, que es también su nombre BLE).

### Telemetría — `iot/{deviceId}/telemetry`

```json
{
  "schemaVersion": "1.0",
  "messageId": "6f1c2a9e-8d2b-4c7e-9a51-2f0b7c3d4e10",
  "organizationId": "org-demo",
  "edgeNodeId": "EDGE-PLANTA-01",
  "deviceId": "EMSafe-DOS-01",
  "deviceType": "PERSONAL_DOSIMETER",
  "publishedAt": "2026-11-12T15:04:05Z",
  "readings": [
    {
      "sequence": 1234,
      "capturedAt": "2026-11-12T15:04:04Z",
      "capturedAtSource": "DEVICE_CLOCK",
      "receivedAt": "2026-11-12T15:04:04Z",
      "magneticFluxDensityUT": 219.5,
      "origin": "REALTIME",
      "quality": "VALID"
    }
  ]
}
```

| Campo | Tipo | Regla |
|---|---|---|
| `schemaVersion` | string | `"1.0"`. La ingesta rechaza versiones mayores desconocidas. |
| `messageId` | UUID | Único por mensaje. Un reintento conserva el mismo `messageId`. |
| `organizationId` | string | Configurado en el Edge (`EMSAFE_ORGANIZATION_ID`). |
| `edgeNodeId` | string | Identificador del nodo Edge (`EMSAFE_EDGE_NODE_ID`). |
| `deviceId` | string | Igual al del tópico. |
| `deviceType` | enum | `PERSONAL_DOSIMETER` \| `FIXED_SENSOR`. |
| `publishedAt` | ISO-8601 UTC | Momento del intento de publicación (se actualiza en cada reintento). |
| `readings[].sequence` | int ≥ 0 | Secuencia del firmware. Se reinicia si el dispositivo se reinicia. |
| `readings[].capturedAt` | ISO-8601 UTC | Hora de captura. Si el dispositivo no tenía hora (`ts = 0`), el Edge usa `receivedAt`. |
| `readings[].capturedAtSource` | enum | `DEVICE_CLOCK` \| `EDGE_RECEIVED`. |
| `readings[].receivedAt` | ISO-8601 UTC | Hora en que el Edge recibió la lectura por BLE. |
| `readings[].magneticFluxDensityUT` | number ≥ 0 | Densidad de flujo magnético en µT. |
| `readings[].origin` | enum | `REALTIME` \| `DEFERRED` (SyncOrigin). |
| `readings[].quality` | enum | `VALID` \| `SATURATED` (≥ `EMSAFE_SATURATION_UT`, 790 µT por defecto). |

**Filtrado:** las lecturas no numéricas, negativas o NaN se descartan con un registro en el log
y no se publican. Las saturadas **sí** se publican, con `quality = SATURATED`.

**Lecturas saturadas:** `SATURATED` indica que el campo fue **de al menos**
`magneticFluxDensityUT`, posiblemente mayor, porque el sensor llegó a su tope. La ingesta y
Personal Dosimetry **no deben descartarlas**: deben usar el valor como mínimo (criterio
conservador) y marcar la jornada como afectada por lecturas fuera de rango. Descartarlas
subestimaría la exposición.

### ReadingIdentity (deduplicación)

- **En el Edge:** `deviceId + sequence + ts crudo` (el `ts` tal como llega por BLE, incluido 0).
  El firmware reenvía el buffer completo desde el inicio si la conexión se corta a mitad. Con
  `ts = 0`, `capturedAt` sale de `receivedAt`, así que usarlo como clave haría que cada reenvío
  pareciera nuevo.
- **`capturedAt` se fija en la primera recepción** y queda guardado. Un reenvío BLE se descarta
  antes de guardarse, y un reintento MQTT publica el mismo `capturedAt`.
- **En la ingesta:** `deviceId + sequence + capturedAt`, equivalente a la del Edge gracias a la regla
  anterior.
- **Limitación conocida:** si el dosímetro se reinicia más de una vez sin conectarse a un Edge,
  su buffer puede tener lecturas de varios arranques con el mismo `sequence` y `ts = 0`. Solo se
  conserva la primera, y el Edge registra una advertencia cuando `sequence` retrocede con `ts = 0`.
  Se resolverá con un `bootId` en el firmware (cambio futuro del contrato BLE).
- `capturedAt` tiene resolución de 1 s (el `ts` del firmware está en segundos).

### Lotes y reintentos

- Un mensaje lleva lecturas de **un solo dispositivo y un solo `origin`**. Las de tiempo real van de
  a 1 por mensaje; las diferidas, en lotes de hasta 50. Los mensajes se publican en orden de llegada.
- **Outbox local (SQLite):** toda lectura válida se guarda primero. Una lectura se marca publicada
  **solo tras el PUBACK** (QoS 1). Si el broker cae, se acumula y se reenvía en orden al reconectar.
- El reintento reenvía **el mismo mensaje**: mismo `messageId` y mismas lecturas, con `publishedAt`
  actualizado. La ingesta es idempotente por ReadingIdentity.

### Estado del dispositivo — `iot/{deviceId}/status` (retained)

Lo publica el Edge al conectar o perder el BLE con el dosímetro.

```json
{
  "schemaVersion": "1.0",
  "deviceId": "EMSafe-DOS-01",
  "edgeNodeId": "EDGE-PLANTA-01",
  "connectivity": "ONLINE",
  "changedAt": "2026-11-12T15:00:00Z",
  "firmwareVersion": null,
  "batteryLevel": null
}
```

`connectivity`: `ONLINE` \| `OFFLINE`. `firmwareVersion` y `batteryLevel` van en `null` hasta que
el firmware los exponga por BLE.

### Configuración del dispositivo — `iot/{deviceId}/config` (retained)

La publica Device Management al registrar el dispositivo o cuando el responsable de SSO cambia el
umbral. El Edge la guarda y la escribe en el dosímetro por BLE al conectar y cuando cambia.

```json
{
  "schemaVersion": "1.0",
  "deviceId": "EMSafe-DOS-01",
  "configVersion": 3,
  "alertThresholdUT": 300.0,
  "definedBy": "SSO_RESPONSIBLE",
  "updatedAt": "2026-11-12T14:00:00Z"
}
```

- Se ignora una configuración con `configVersion` menor o igual a la vigente.
- También se rechaza, con un registro en el log, si `alertThresholdUT` no es numérico, es ≤ 0 o es
  ≥ `EMSAFE_SATURATION_UT`. En ese caso se conserva la última válida.
- Sin configuración, el Edge usa `EMSAFE_DEFAULT_THRESHOLD_UT` (300 µT) y registra una advertencia.

### Estado del nodo Edge — `edge/{edgeNodeId}/status` (retained, Last Will)

Al conectar al broker, el Edge publica:

```json
{"status": "ONLINE", "changedAt": "2026-11-12T15:00:00Z"}
```

Antes de conectar, deja registrado un **Last Will** que el broker publica si el Edge cae sin
despedirse:

```json
{"status": "OFFLINE"}
```

El Last Will **no lleva `changedAt`**: se registra al conectar, antes de saber cuándo caerá el Edge.

### Seguridad

- **Producción:** MQTT v5 sobre TLS (puerto 8883), credenciales por nodo Edge y ACL por
  organización.
- **Prototipo:** Mosquitto local en el puerto 1883 sin TLS. Es una desviación declarada; TLS y
  credenciales se activan por variables de entorno (`EMSAFE_MQTT_TLS`, `EMSAFE_MQTT_USERNAME`,
  `EMSAFE_MQTT_PASSWORD`).
