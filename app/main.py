import asyncio
import base64
import json
import os
import sqlite3
import subprocess
import uuid
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask

DATA = Path("/app/data")
# Keep media inside the Docker volume so a container restart cannot discard it.
MEDIA = DATA / "media"
TIMELAPSE_RUNS = MEDIA / "timelapses"
DB = DATA / "jobs.db"
LOG_FILE = DATA / "camera-notifier.log"
TZ = ZoneInfo(os.getenv("TZ", "America/Santiago"))
RTSP_URL = os.getenv("CAMERA_RTSP_URL", "")
WAHA_URL = os.getenv("WAHA_URL", "").rstrip("/")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")
APP_API_TOKEN = os.getenv("APP_API_TOKEN", "")
SNAPSHOT_WARMUP_SECONDS = max(0, int(os.getenv("SNAPSHOT_WARMUP_SECONDS", "10")))
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("camera-notifier")
RUNNING_JOB_IDS: set[str] = set()


class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["snapshot", "video", "timelapse"]
    stream_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1, max_length=160)
    delivery_channel: Literal["waha", "telegram"] = "waha"
    recipient_id: str | None = None
    cron: str = Field(description="Cron de 5 campos, p.ej. 0 8 * * *")
    caption: str = Field(default="", max_length=1024)
    duration_seconds: int = Field(default=10, ge=2, le=60)
    timelapse_interval_seconds: int = Field(default=5, ge=1, le=3600)
    timelapse_frames: int = Field(default=12, ge=2, le=720)
    # Timelapses always play at 24 FPS. The client no longer exposes this as
    # a tuning knob: sampling cadence determines the real time compressed.
    timelapse_fps: int = Field(default=24, ge=1, le=60)
    image_delivery: Literal["image", "document"] = "image"
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def valid_cron(cls, value: str) -> str:
        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError("Debe ser un cron válido de 5 campos")
        return value

class StreamIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    source_type: Literal["rtsp", "mjpeg"]
    source_url: str = Field(min_length=8, max_length=2048)
    warmup_seconds: int = Field(default=10, ge=0, le=120)
    enabled: bool = True


class RecipientIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    channel: Literal["waha", "telegram"]
    destination: str = Field(min_length=1, max_length=160)
    enabled: bool = True


class SettingsIn(BaseModel):
    waha_url: str | None = Field(default=None, max_length=1024)
    waha_api_key: str | None = Field(default=None, max_length=1024)
    waha_session: str | None = Field(default=None, max_length=160)
    telegram_bot_token: str | None = Field(default=None, max_length=1024)


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def configure_file_logging() -> None:
    """Persist operational logs in the Docker data volume for the UI."""
    DATA.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(handler, "baseFilename", None) == str(LOG_FILE) for handler in root.handlers):
        return
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    ))
    root.addHandler(handler)


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS streams (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, source_type TEXT NOT NULL, source_url TEXT NOT NULL,
          warmup_seconds INTEGER NOT NULL DEFAULT 10, enabled INTEGER NOT NULL, created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, chat_id TEXT NOT NULL,
          cron TEXT NOT NULL, caption TEXT NOT NULL, duration_seconds INTEGER NOT NULL,
          image_delivery TEXT NOT NULL DEFAULT 'image', enabled INTEGER NOT NULL, last_run TEXT, last_status TEXT, last_error TEXT,
          created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS app_settings (
          id INTEGER PRIMARY KEY CHECK (id=1), waha_url TEXT NOT NULL DEFAULT '', waha_api_key TEXT NOT NULL DEFAULT '',
          waha_session TEXT NOT NULL DEFAULT 'default', telegram_bot_token TEXT NOT NULL DEFAULT ''
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS recipients (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, channel TEXT NOT NULL, destination TEXT NOT NULL,
          enabled INTEGER NOT NULL, created_at TEXT NOT NULL
        )""")
        con.execute("INSERT OR IGNORE INTO app_settings (id,waha_url,waha_api_key,waha_session,telegram_bot_token) VALUES (1,?,?,?,?)", (WAHA_URL, WAHA_API_KEY, WAHA_SESSION, os.getenv("TELEGRAM_BOT_TOKEN", "")))
        columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
        if "image_delivery" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN image_delivery TEXT NOT NULL DEFAULT 'image'")
        additions = {
            "stream_id": "TEXT",
            "timelapse_interval_seconds": "INTEGER NOT NULL DEFAULT 5",
            "timelapse_frames": "INTEGER NOT NULL DEFAULT 12",
            "timelapse_fps": "INTEGER NOT NULL DEFAULT 5",
            "run_started_at": "TEXT",
            "progress_current": "INTEGER NOT NULL DEFAULT 0",
            "progress_total": "INTEGER NOT NULL DEFAULT 0",
            "progress_phase": "TEXT",
            "progress_message": "TEXT",
            "delivery_channel": "TEXT NOT NULL DEFAULT 'waha'",
            "recipient_id": "TEXT",
        }
        for column, definition in additions.items():
            if column not in columns:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
        legacy = con.execute("SELECT id FROM streams LIMIT 1").fetchone()
        if not legacy and RTSP_URL:
            stream_id = uuid.uuid4().hex
            con.execute("INSERT INTO streams VALUES (?,?,?,?,?,?,?)", (stream_id, "EC6 principal", "rtsp", RTSP_URL, SNAPSHOT_WARMUP_SECONDS, 1, now()))
            legacy = {"id": stream_id}
        if legacy:
            con.execute("UPDATE jobs SET stream_id=? WHERE stream_id IS NULL OR stream_id=''", (legacy["id"],))
        con.execute("UPDATE jobs SET timelapse_fps=24 WHERE kind='timelapse'")
        # An in-memory job cannot survive a container restart. Do not leave a
        # stale 'running' state in the UI after the worker disappeared.
        con.execute("""UPDATE jobs SET last_status='interrupted', progress_phase='Interrumpido',
                    progress_message='El servicio se reinició durante la ejecución'
                    WHERE last_status='running'""")
        # Snapshots are intentionally inline WhatsApp images. A document keeps
        # bytes untouched but breaks the required in-chat preview experience.
        con.execute("UPDATE jobs SET image_delivery='image' WHERE kind='snapshot'")


def job_dict(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["enabled"] = bool(result["enabled"])
    return result


def delivery_settings() -> dict:
    with connect() as con:
        row = con.execute("SELECT * FROM app_settings WHERE id=1").fetchone()
    values = dict(row) if row else {}
    return {
        "waha_url": (values.get("waha_url") or WAHA_URL).rstrip("/"),
        "waha_api_key": values.get("waha_api_key") or WAHA_API_KEY,
        "waha_session": values.get("waha_session") or WAHA_SESSION,
        "telegram_bot_token": values.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", ""),
    }


def resolve_recipient(job: dict) -> dict:
    if not job.get("recipient_id"):
        return job
    with connect() as con:
        recipient = con.execute("SELECT * FROM recipients WHERE id=? AND enabled=1", (job["recipient_id"],)).fetchone()
    if not recipient:
        raise RuntimeError("El destinatario no existe o está desactivado")
    job = dict(job)
    job["delivery_channel"], job["chat_id"] = recipient["channel"], recipient["destination"]
    return job


def update_job_progress(job_id: str, current: int, total: int, phase: str, message: str) -> None:
    """Persist progress so the UI can report work occurring in a worker thread."""
    with connect() as con:
        con.execute(
            """UPDATE jobs SET progress_current=?, progress_total=?, progress_phase=?, progress_message=?
               WHERE id=?""",
            (current, total, phase, message, job_id),
        )


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    if not APP_API_TOKEN or x_api_token != APP_API_TOKEN:
        raise HTTPException(401, "Token de aplicación inválido")


def ffmpeg_input(stream: dict) -> list[str]:
    if stream["source_type"] == "rtsp":
        return ["-rtsp_transport", "tcp", "-fflags", "+discardcorrupt", "-analyzeduration", "10M", "-probesize", "10M", "-i", stream["source_url"]]
    return ["-fflags", "+discardcorrupt", "-i", stream["source_url"]]


def safe_ffmpeg_error(stderr: str, fallback: str) -> str:
    """Keep operational errors useful without leaking RTSP credentials."""
    text = re.sub(r"://[^\s/@]+(?::[^\s/@]*)?@", "://***@", stderr or fallback)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    useful = [
        line for line in lines
        if re.search(r"error|failed|invalid|unable|refused|timeout|not found|opening input", line, re.I)
    ]
    return " | ".join((useful or lines[-3:])[-4:])[-1000:] or fallback


def safe_exception(exc: BaseException) -> str:
    """Short, credential-safe message for operational logs."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"ffmpeg timeout after {exc.timeout}s"
    return safe_ffmpeg_error(str(exc), exc.__class__.__name__)


def write_timelapse_manifest(run_dir: Path, manifest: dict) -> None:
    """Atomically persist progress without storing stream credentials."""
    temporary = run_dir / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(run_dir / "manifest.json")


def mark_timelapse_delivery(file: Path, status: str, error: str | None = None) -> None:
    """Record delivery status while keeping the generated media recoverable."""
    manifest_path = file.parent / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        manifest.update({"delivery_status": status, "delivery_updated_at": now()})
        if error:
            manifest["delivery_error"] = error
        else:
            manifest.pop("delivery_error", None)
        write_timelapse_manifest(file.parent, manifest)
    except OSError as exc:
        logger.warning("Could not update persistent timelapse delivery state for %s: %s", file.name, exc)


def capture_with_retry(kind: str, duration: int, stream: dict, job: dict | None = None) -> Path:
    """Retry transient failures while opening a live RTSP source."""
    attempts = 3 if stream["source_type"] == "rtsp" else 1
    last_error = None
    for attempt in range(attempts):
        try:
            return capture(kind, duration, stream, job)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
            logger.warning(
                "RTSP source %s failed to capture (%s); retrying (%s/%s)",
                stream["id"], safe_exception(exc), attempt + 1, attempts,
            )
            time.sleep(3 * (attempt + 1))
    raise last_error or RuntimeError("No se pudo capturar la fuente")


def capture_timelapse(stream: dict, job: dict) -> Path:
    """Capture JPEG frames and turn the frames that succeeded into an MP4.

    A camera/RTSP relay can drop a connection between two snapshots.  That
    must not discard frames already captured or terminate the scheduler; the
    failed photo is skipped and the remaining files stay contiguous for
    ffmpeg's image-sequence input.
    """
    TIMELAPSE_RUNS.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now(TZ):%Y%m%d-%H%M%S}-{job['id'][:8]}-{uuid.uuid4().hex[:8]}"
    frame_dir = TIMELAPSE_RUNS / run_id
    target = frame_dir / "timelapse.mp4"
    frame_dir.mkdir()
    frames = job["timelapse_frames"]
    interval = job["timelapse_interval_seconds"]
    captured_frames = 0
    manifest = {
        "run_id": run_id,
        "job_id": job["id"],
        "job_name": job["name"],
        "kind": "timelapse",
        "started_at": now(),
        "status": "capturing",
        "expected_frames": frames,
        "captured_frames": captured_frames,
        "interval_seconds": interval,
        "fps": job["timelapse_fps"],
    }
    write_timelapse_manifest(frame_dir, manifest)
    update_job_progress(job["id"], 0, frames, "Capturando", f"Preparando 0 de {frames} fotos")
    try:
        for index in range(frames):
            started = time.monotonic()
            try:
                image = capture_with_retry("snapshot", 0, stream)
                image.replace(frame_dir / f"frame-{captured_frames:05d}.jpg")
                captured_frames += 1
            except Exception as exc:
                # Keep one lost frame from aborting the entire timelapse.
                # CancelledError is a BaseException, so shutdown cancellation
                # is still allowed to propagate normally.
                logger.warning(
                    "Timelapse %s: frame %s/%s unavailable: %s",
                    job["id"], index + 1, frames, safe_exception(exc),
                )
                manifest["last_frame_error"] = safe_exception(exc)
            manifest.update({"captured_frames": captured_frames, "updated_at": now()})
            write_timelapse_manifest(frame_dir, manifest)
            update_job_progress(
                job["id"], index + 1, frames, "Capturando",
                f"Foto {index + 1} de {frames} · {captured_frames} capturadas",
            )
            if index < frames - 1:
                # The interval measures the start of each photo. When RTSP
                # warmup itself is longer than the interval, capture again
                # immediately rather than adding extra delay.
                time.sleep(max(0, interval - (time.monotonic() - started)))
        if captured_frames == 0:
            raise RuntimeError("No se pudo capturar ningún frame del timelapse")
        update_job_progress(job["id"], frames, frames, "Generando video", f"Codificando {captured_frames} fotos")
        manifest.update({"status": "encoding", "captured_frames": captured_frames, "updated_at": now()})
        write_timelapse_manifest(frame_dir, manifest)
        cmd = [
            "ffmpeg", "-y", "-framerate", str(job["timelapse_fps"]),
            "-i", str(frame_dir / "frame-%05d.jpg"), "-an", "-c:v", "libx264",
            "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(target),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if completed.returncode or not target.exists() or target.stat().st_size == 0:
            raise RuntimeError(safe_ffmpeg_error(completed.stderr, "ffmpeg no generó el timelapse"))
        manifest.update({
            "status": "completed",
            "captured_frames": captured_frames,
            "completed_at": now(),
            "video": target.name,
            "video_bytes": target.stat().st_size,
        })
        write_timelapse_manifest(frame_dir, manifest)
        logger.info("Timelapse %s completed: %s/%s frames, file=%s, bytes=%s", job["id"], captured_frames, frames, target.name, target.stat().st_size)
        return target
    except Exception as exc:
        manifest.update({
            "status": "failed",
            "captured_frames": captured_frames,
            "failed_at": now(),
            "error": safe_exception(exc),
        })
        write_timelapse_manifest(frame_dir, manifest)
        raise


def capture(kind: str, duration: int, stream: dict, job: dict | None = None) -> Path:
    if kind == "timelapse":
        if not job:
            raise RuntimeError("Falta configuración de timelapse")
        return capture_timelapse(stream, job)
    MEDIA.mkdir(parents=True, exist_ok=True)
    suffix = ".jpg" if kind == "snapshot" else ".mp4"
    target = MEDIA / f"{kind}-{datetime.now(TZ):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}{suffix}"
    if kind == "snapshot":
        warmup = stream["warmup_seconds"] if stream["source_type"] == "rtsp" else 0
        cmd = ["ffmpeg", "-y"]
        if stream["source_type"] == "rtsp":
            cmd += ["-skip_frame", "nokey"]
        cmd += ffmpeg_input(stream) + ["-t", str(warmup + 15), "-frames:v", "1", "-vf", f"select=gte(t\\,{warmup}),format=yuvj420p", "-q:v", "1", str(target)]
        timeout = warmup + 60
    else:
        cmd = ["ffmpeg", "-y"] + ffmpeg_input(stream) + ["-t", str(duration), "-an", "-c:v", "libx264", "-preset", "veryfast", "-movflags", "+faststart", str(target)]
        timeout = duration + 45
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if completed.returncode or not target.exists() or target.stat().st_size == 0:
        raise RuntimeError(safe_ffmpeg_error(completed.stderr, "ffmpeg no generó el archivo"))
    return target


async def send_to_channel(path: Path, job: dict) -> None:
    config = delivery_settings()
    if job.get("delivery_channel", "waha") == "telegram":
        token = config["telegram_bot_token"]
        if not token:
            raise RuntimeError("Telegram no está configurado")
        media_key = "photo" if job["kind"] == "snapshot" else "video"
        endpoint = f"https://api.telegram.org/bot{token}/send{'Photo' if media_key == 'photo' else 'Video'}"
        async with httpx.AsyncClient(timeout=120) as client:
            with path.open("rb") as media:
                response = await client.post(endpoint, data={"chat_id": job["chat_id"], "caption": job["caption"]}, files={media_key: (path.name, media, "image/jpeg" if media_key == "photo" else "video/mp4")})
        if response.is_error:
            raise RuntimeError(f"Telegram {response.status_code}: {response.text[-400:]}")
        return
    if not config["waha_url"]:
        raise RuntimeError("WAHA no está configurada")
    mimetype = "image/jpeg" if job["kind"] == "snapshot" else "video/mp4"
    payload = {
        "session": config["waha_session"],
        "chatId": job["chat_id"],
        "caption": job["caption"],
        "file": {"mimetype": mimetype, "filename": path.name, "data": base64.b64encode(path.read_bytes()).decode()},
    }
    if job["kind"] in ("video", "timelapse"):
        payload.update({"asNote": False, "convert": False})
    headers = {"Content-Type": "application/json"}
    if config["waha_api_key"]:
        headers["X-Api-Key"] = config["waha_api_key"]
    endpoint = "/api/sendImage" if job["kind"] == "snapshot" else "/api/sendVideo"
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(config["waha_url"] + endpoint, headers=headers, json=payload)
        if response.is_error:
            raise RuntimeError(f"WAHA {response.status_code}: {response.text[-700:]}")


async def execute(job: dict) -> None:
    file = None
    logger.info("Job %s started: name=%s kind=%s stream=%s", job["id"], job["name"], job["kind"], job["stream_id"])
    total = job["timelapse_frames"] if job["kind"] == "timelapse" else 1
    try:
        update_job_progress(job["id"], 0, total, "Preparando", "Iniciando captura")
        with connect() as con:
            con.execute(
                """UPDATE jobs SET last_run=?, last_status='running', last_error=NULL, run_started_at=?
                   WHERE id=?""",
                (now(), now(), job["id"]),
            )
        with connect() as con:
            stream = con.execute("SELECT * FROM streams WHERE id=? AND enabled=1", (job["stream_id"],)).fetchone()
        if not stream:
            raise RuntimeError("La fuente de video no existe o está desactivada")
        job = resolve_recipient(job)
        file = await asyncio.to_thread(capture_with_retry, job["kind"], job["duration_seconds"], dict(stream), job)
        update_job_progress(job["id"], total, total, "Enviando", "Enviando por WhatsApp")
        await send_to_channel(file, job)
        status, error = "sent", None
        if job["kind"] == "timelapse":
            mark_timelapse_delivery(file, "sent")
        logger.info("Job %s sent successfully: file=%s bytes=%s", job["id"], file.name, file.stat().st_size)
    except Exception as exc:
        status, error = "error", safe_exception(exc)
        if file and job["kind"] == "timelapse":
            mark_timelapse_delivery(file, "error", error)
    finally:
        # Timelapses retain every captured frame and the generated MP4 even
        # when delivery fails; only confirmed non-timelapse deliveries clean up.
        if file and job["kind"] != "timelapse" and status == "sent":
            file.unlink(missing_ok=True)
    try:
        with connect() as con:
            con.execute(
                """UPDATE jobs SET last_run=?, last_status=?, last_error=?, progress_current=?,
                   progress_total=?, progress_phase=?, progress_message=? WHERE id=?""",
                (
                    now(), status, error, total if status == "sent" else 0, total,
                    "Enviado" if status == "sent" else "Error",
                    "Entrega completada" if status == "sent" else error,
                    job["id"],
                ),
            )
    finally:
        RUNNING_JOB_IDS.discard(job["id"])
    if error:
        # Scheduled work must record failures without producing an unobserved
        # asyncio task exception in container logs.
        logger.error("Job %s failed: %s", job["id"], error)


def start_job(job: dict) -> bool:
    if job["id"] in RUNNING_JOB_IDS:
        logger.warning("Job %s skipped because it is already running", job["id"])
        return False
    RUNNING_JOB_IDS.add(job["id"])
    asyncio.create_task(execute(job), name=f"camera-job-{job['id']}")
    return True


async def scheduler() -> None:
    last_minute = ""
    while True:
        current = datetime.now(TZ).replace(second=0, microsecond=0)
        marker = current.isoformat()
        if marker != last_minute:
            last_minute = marker
            with connect() as con:
                jobs = [job_dict(row) for row in con.execute("SELECT * FROM jobs WHERE enabled=1")]
            for job in jobs:
                if croniter.match(job["cron"], current):
                    start_job(job)
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_file_logging()
    init_db()
    logger.info("Application startup complete")
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()


app = FastAPI(title="Camera WAHA Notifier", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
def frontend():
    return FileResponse("app/static/index.html")


@app.get("/api/health")
def health(_: None = Depends(require_token)):
    return {"ok": True, "time": now(), "waha_configured": bool(WAHA_URL)}


@app.get("/api/logs")
def list_logs(limit: int = Query(default=200, ge=1, le=500), _: None = Depends(require_token)):
    if not LOG_FILE.exists():
        return {"lines": []}
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"lines": lines[-limit:]}


@app.get("/api/streams")
def list_streams(_: None = Depends(require_token)):
    with connect() as con:
        return [dict(row) | {"enabled": bool(row["enabled"])} for row in con.execute("SELECT * FROM streams ORDER BY created_at DESC")]


@app.get("/api/settings")
def get_settings(_: None = Depends(require_token)):
    config = delivery_settings()
    return {"waha_url": config["waha_url"], "waha_session": config["waha_session"], "waha_api_key_configured": bool(config["waha_api_key"]), "telegram_configured": bool(config["telegram_bot_token"])}


@app.put("/api/settings")
def update_settings(body: SettingsIn, _: None = Depends(require_token)):
    values = body.model_dump(exclude_none=True)
    with connect() as con:
        current = dict(con.execute("SELECT * FROM app_settings WHERE id=1").fetchone())
        for key, value in values.items():
            if key in ("waha_api_key", "telegram_bot_token") and not value:
                continue
            current[key] = value
        con.execute("UPDATE app_settings SET waha_url=?,waha_api_key=?,waha_session=?,telegram_bot_token=? WHERE id=1", (current["waha_url"].rstrip("/"), current["waha_api_key"], current["waha_session"], current["telegram_bot_token"]))
    return get_settings()


@app.get("/api/recipients")
def list_recipients(_: None = Depends(require_token)):
    with connect() as con:
        return [dict(row) | {"enabled": bool(row["enabled"])} for row in con.execute("SELECT * FROM recipients ORDER BY name")]


@app.post("/api/recipients", status_code=201)
def create_recipient(body: RecipientIn, _: None = Depends(require_token)):
    recipient = body.model_dump() | {"id": uuid.uuid4().hex, "created_at": now()}
    with connect() as con:
        con.execute("INSERT INTO recipients VALUES (:id,:name,:channel,:destination,:enabled,:created_at)", recipient)
    return recipient


@app.delete("/api/recipients/{recipient_id}", status_code=204)
def delete_recipient(recipient_id: str, _: None = Depends(require_token)):
    with connect() as con:
        if con.execute("SELECT 1 FROM jobs WHERE recipient_id=?", (recipient_id,)).fetchone():
            raise HTTPException(409, "No puedes borrar un destinatario asignado a tareas")
        con.execute("DELETE FROM recipients WHERE id=?", (recipient_id,))


@app.post("/api/streams", status_code=201)
def create_stream(body: StreamIn, _: None = Depends(require_token)):
    stream = body.model_dump() | {"id": uuid.uuid4().hex, "created_at": now()}
    with connect() as con:
        con.execute("""INSERT INTO streams (id,name,source_type,source_url,warmup_seconds,enabled,created_at)
                       VALUES (:id,:name,:source_type,:source_url,:warmup_seconds,:enabled,:created_at)""", stream)
    return stream


@app.put("/api/streams/{stream_id}")
def update_stream(stream_id: str, body: StreamIn, _: None = Depends(require_token)):
    stream = body.model_dump() | {"id": stream_id}
    with connect() as con:
        if not con.execute("SELECT 1 FROM streams WHERE id=?", (stream_id,)).fetchone():
            raise HTTPException(404, "Fuente no encontrada")
        con.execute("""UPDATE streams SET name=:name,source_type=:source_type,source_url=:source_url,
                     warmup_seconds=:warmup_seconds,enabled=:enabled WHERE id=:id""", stream)
    return stream


@app.delete("/api/streams/{stream_id}", status_code=204)
def delete_stream(stream_id: str, _: None = Depends(require_token)):
    with connect() as con:
        if con.execute("SELECT 1 FROM jobs WHERE stream_id=?", (stream_id,)).fetchone():
            raise HTTPException(409, "No puedes borrar una fuente asignada a tareas")
        con.execute("DELETE FROM streams WHERE id=?", (stream_id,))


@app.post("/api/streams/{stream_id}/test")
async def test_stream(stream_id: str, _: None = Depends(require_token)):
    with connect() as con:
        row = con.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Fuente no encontrada")
    file = await asyncio.to_thread(capture_with_retry, "snapshot", 0, dict(row))
    file.unlink(missing_ok=True)
    return {"ok": True, "message": "Snapshot capturado correctamente"}


@app.post("/api/streams/{stream_id}/snapshot")
async def stream_snapshot(stream_id: str, _: None = Depends(require_token)):
    """Capture one current frame for the UI and remove it after the response."""
    with connect() as con:
        row = con.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Fuente no encontrada")
    if not row["enabled"]:
        raise HTTPException(409, "La fuente está pausada")
    try:
        file = await asyncio.to_thread(capture_with_retry, "snapshot", 0, dict(row))
    except Exception as exc:
        raise HTTPException(502, f"No se pudo traer la instantánea: {safe_exception(exc)}") from exc
    logger.info("Preview snapshot captured for stream %s: %s", stream_id, file.name)
    return FileResponse(
        file,
        media_type="image/jpeg",
        filename=file.name,
        background=BackgroundTask(file.unlink, missing_ok=True),
    )


@app.get("/api/jobs")
def list_jobs(_: None = Depends(require_token)):
    with connect() as con:
        return [job_dict(row) for row in con.execute("SELECT * FROM jobs ORDER BY created_at DESC")]


@app.post("/api/jobs", status_code=201)
def create_job(body: JobIn, _: None = Depends(require_token)):
    job = body.model_dump()
    if job["kind"] == "timelapse":
        job["timelapse_fps"] = 24
    job["id"], job["created_at"] = uuid.uuid4().hex, now()
    with connect() as con:
        if not con.execute("SELECT 1 FROM streams WHERE id=?", (job["stream_id"],)).fetchone():
            raise HTTPException(422, "La fuente seleccionada no existe")
        con.execute("""INSERT INTO jobs (id,name,kind,stream_id,chat_id,delivery_channel,recipient_id,cron,caption,duration_seconds,timelapse_interval_seconds,timelapse_frames,timelapse_fps,image_delivery,enabled,created_at)
                       VALUES (:id,:name,:kind,:stream_id,:chat_id,:delivery_channel,:recipient_id,:cron,:caption,:duration_seconds,:timelapse_interval_seconds,:timelapse_frames,:timelapse_fps,:image_delivery,:enabled,:created_at)""", job)
    return job


@app.put("/api/jobs/{job_id}")
def update_job(job_id: str, body: JobIn, _: None = Depends(require_token)):
    job = body.model_dump() | {"id": job_id}
    if job["kind"] == "timelapse":
        job["timelapse_fps"] = 24
    with connect() as con:
        exists = con.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Tarea no encontrada")
        if not con.execute("SELECT 1 FROM streams WHERE id=?", (job["stream_id"],)).fetchone():
            raise HTTPException(422, "La fuente seleccionada no existe")
        con.execute("""UPDATE jobs SET name=:name,kind=:kind,stream_id=:stream_id,chat_id=:chat_id,delivery_channel=:delivery_channel,recipient_id=:recipient_id,cron=:cron,caption=:caption,
                     duration_seconds=:duration_seconds,timelapse_interval_seconds=:timelapse_interval_seconds,timelapse_frames=:timelapse_frames,timelapse_fps=:timelapse_fps,image_delivery=:image_delivery,enabled=:enabled WHERE id=:id""", job)
    return job


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, _: None = Depends(require_token)):
    with connect() as con:
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))


@app.post("/api/jobs/{job_id}/run", status_code=202)
async def run_now(job_id: str, _: None = Depends(require_token)):
    with connect() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Tarea no encontrada")
    if not start_job(job_dict(row)):
        raise HTTPException(409, "La tarea ya está en ejecución")
    return {"accepted": True}
