# Portal al Sur · Camera Notifier

Portal Docker para capturar snapshots, videos cortos y timelapses desde cámaras RTSP/MJPEG, programarlos con calendario y enviarlos por WhatsApp (WAHA) o Telegram.

> Diseñado para una operación local: cámaras, scheduler, configuración de canales, destinatarios y logs en una sola interfaz protegida por token.

![Portal de tareas y monitoreo](docs/screenshots/portal-dashboard.png)

## Flujo de extremo a extremo

```mermaid
flowchart LR
    CAM["Cámara IP\nRTSP / ONVIF"] -->|RTSP| RELAY["go2rtc opcional\nRTSP · MJPEG · WebRTC"]
    CAM -->|RTSP directo| APP
    RELAY -->|RTSP o MJPEG| APP["Camera Notifier\nFastAPI + SQLite + ffmpeg"]
    UI["Portal web\nTareas · Configuración · Logs"] -->|API protegida| APP
    CRON["Cron scheduler"] -->|inicia tarea| APP
    APP -->|snapshot / video / timelapse| FFMPEG["ffmpeg\ncaptura y codifica"]
    FFMPEG --> APP
    APP -->|sendImage / sendVideo| WAHA["WAHA\nWhatsApp"]
    APP -->|sendPhoto / sendVideo| TG["Telegram Bot API"]
    WAHA --> PEOPLE["Destinatarios"]
    TG --> PEOPLE
```

## Qué incluye

- Múltiples cámaras RTSP y MJPEG.
- Snapshot, video y timelapse programados.
- Progreso persistente, reintentos RTSP y protección contra jobs solapados.
- Vista de instantánea actual desde el portal.
- Configuración separada de cámaras, WAHA, Telegram y destinatarios.
- Agenda reutilizable de destinos WhatsApp/Telegram.
- Logs persistentes consultables desde la interfaz.

## Inicio rápido

```bash
git clone https://github.com/TU_USUARIO/portal-al-sur.git
cd portal-al-sur
cp .env.example .env
# Define APP_API_TOKEN; las integraciones se pueden completar luego en el portal.
docker compose up -d --build
```

Abre `http://IP_DEL_SERVIDOR:8088` e inicia sesión con `APP_API_TOKEN`.

## Cámaras: RTSP directo o go2rtc

Puedes agregar una URL RTSP directa, por ejemplo:

```text
rtsp://usuario:password@192.168.1.50:554/stream1
```

Si la cámara es inestable, está detrás de NAT o necesitas un endpoint MJPEG más simple, usa [go2rtc](https://github.com/AlexxIT/go2rtc) como relay local. Configuración mínima:

```yaml
streams:
  patio: rtsp://usuario:password@192.168.1.50:554/stream1
```

Luego puedes usar cualquiera de estos endpoints desde Camera Notifier:

```text
rtsp://GO2RTC_HOST:8554/patio
http://GO2RTC_HOST:1984/api/stream.mjpeg?src=patio
```

Para RTSP/H.265 deja una estabilización de 10 s como punto de partida. Para MJPEG normalmente basta `0` s.

## Configuración del portal

En **Configuración** hay bloques desplegables para:

| Bloque | Qué configura |
| --- | --- |
| Cámaras | Fuentes RTSP/MJPEG, estabilización y prueba de instantánea. |
| WhatsApp / WAHA | URL, sesión y API key del servidor WAHA. |
| Telegram | Token del bot. |
| Destinatarios | Nombre, canal y destino (`numero@c.us` o `chat_id`). |

Después crea una tarea, elige cámara y destinatario, y define cuándo enviarla. No necesitas escribir cron salvo en la opción avanzada.

## Timelapses

El portal trabaja a **24 FPS fijos**. Solo eliges tiempo real de observación y frecuencia de foto; la interfaz calcula cantidad de cuadros y duración final.

| Ejemplo | Resultado aproximado |
| --- | --- |
| 1 hora, 1 foto/minuto | 61 cuadros → 2.5 s de video |
| 1 hora, 1 foto/10 s | 361 cuadros → 15 s de video |
| 1 hora, 1 foto/5 s | 721 cuadros → 30 s de video |

La estabilización RTSP es parte de la cadencia real: si tarda 10 s, no es posible capturar un frame nuevo cada 5 s sin usar una fuente persistente/relay más rápido.

## Variables de entorno

Las variables son respaldo de inicio. Los canales también pueden configurarse desde el portal y se persisten en el volumen Docker.

| Variable | Uso |
| --- | --- |
| `APP_API_TOKEN` | Protege interfaz y API; usa un valor largo y aleatorio. |
| `CAMERA_RTSP_URL` | Cámara inicial opcional. Codifica caracteres especiales en la URL. |
| `SNAPSHOT_WARMUP_SECONDS` | Espera de RTSP; por defecto `10`. |
| `WAHA_URL`, `WAHA_API_KEY`, `WAHA_SESSION` | Configuración inicial de WAHA. |
| `TELEGRAM_BOT_TOKEN` | Token inicial opcional para Telegram. |
| `TZ` | Zona horaria; por defecto `America/Santiago`. |

## Operación

```bash
docker compose ps
docker compose logs --tail=100 camera-notifier
curl -H "X-Api-Token: TU_TOKEN" http://localhost:8088/api/health
```

Los archivos de datos, tokens configurados, destinatarios y logs viven en el volumen Docker `camera_notifier_data`.

## Seguridad

- No publiques `.env`, bases SQLite, logs, videos ni snapshots.
- No expongas el puerto 8088 directamente a Internet. Usa VPN, Tailscale o reverse proxy HTTPS.
- Los tokens no se devuelven por la API ni se escriben en logs.
- Respalda el volumen Docker de forma segura: contiene configuración operativa sensible.

## Desarrollo y publicación

```bash
python -m py_compile app/main.py
node --check app/static/app.js
docker compose config --quiet
```

El repositorio ignora `.env`, datos, logs y medios generados. Antes de hacerlo público, agrega la licencia que corresponda a tu proyecto.
