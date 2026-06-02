# backend/app.py
import base64
import copy
import datetime
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import time
from urllib.parse import urlencode
from collections import Counter, deque
from functools import wraps
from types import SimpleNamespace
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from flask import Flask, Response, jsonify, render_template, request, session, redirect
import sqlite3
import threading
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    import psutil
except Exception:
    class _PsutilFallback:
        @staticmethod
        def boot_time():
            raise RuntimeError("psutil is unavailable")

        @staticmethod
        def cpu_percent(interval=None):
            if interval:
                time.sleep(float(interval))
            return 0.0

        @staticmethod
        def virtual_memory():
            percent = 0.0
            try:
                page_size = os.sysconf("SC_PAGE_SIZE")
                total_pages = os.sysconf("SC_PHYS_PAGES")
                available_pages = os.sysconf("SC_AVPHYS_PAGES")
                total = page_size * total_pages
                available = page_size * available_pages
                if total > 0:
                    percent = round(((total - available) / total) * 100, 2)
            except Exception:
                pass
            return SimpleNamespace(percent=percent)

        @staticmethod
        def disk_usage(path):
            usage = shutil.disk_usage(path)
            percent = round((usage.used / usage.total) * 100, 2) if usage.total else 0.0
            return SimpleNamespace(total=usage.total, used=usage.used, free=usage.free, percent=percent)

        @staticmethod
        def net_io_counters():
            return SimpleNamespace(bytes_sent=0, bytes_recv=0)

        @staticmethod
        def pids():
            return []

    psutil = _PsutilFallback()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if load_dotenv is not None:
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
else:
    print("⚠️ python-dotenv is not installed; falling back to environment variables only.")

# Basic startup configuration hints
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
if FLASK_DEBUG:
    print("⚙️ Starting in DEBUG mode (FLASK_DEBUG=1)")

_required_env = ["FLASK_SECRET"]
_missing_env = [v for v in _required_env if not os.getenv(v)]
if _missing_env:
    print(f"⚠️ Missing environment variables: {', '.join(_missing_env)}.")


def get_env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def get_env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def get_env_set(name, default):
    raw = os.getenv(name, default)
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def get_env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def is_production_environment():
    configured_env = str(
        os.getenv("APP_ENV")
        or os.getenv("FLASK_ENV")
        or os.getenv("RAILWAY_ENVIRONMENT")
        or ""
    ).strip().lower()
    return configured_env in {"production", "prod"} or bool(os.getenv("RAILWAY_PUBLIC_DOMAIN"))


def bounded_int(value, default, minimum=1, maximum=200):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def resolve_project_path(value):
    if os.path.isabs(value):
        return value
    return os.path.join(PROJECT_ROOT, value)


def normalize_camera_name(value):
    return " ".join(str(value or "").strip().split())


def normalize_mobile_camera_id(value, default="default"):
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return normalized or default


def mobile_camera_source(device_id):
    return f"mobile://{normalize_mobile_camera_id(device_id)}"


def is_mobile_camera_source(value):
    return str(value if value is not None else "").strip().lower().startswith("mobile://")


def get_mobile_camera_id_from_source(source):
    raw = str(source if source is not None else "").strip()
    if not is_mobile_camera_source(raw):
        return ""
    return normalize_mobile_camera_id(raw.split("://", 1)[1] or "default")


def camera_source_display(value):
    source = str(value if value is not None else "").strip()
    return source or str(CAMERA_INDEX)


def normalize_camera_source(value):
    raw = str(value if value is not None else "").strip()
    if not raw:
        return raw
    raw_lower = raw.lower()

    if raw_lower in {"mobile", "phone", "mobile-camera", "mobile-cam"}:
        return mobile_camera_source("default")

    if raw_lower.startswith("mobile://"):
        return mobile_camera_source(raw_lower.split("://", 1)[1] or "default")

    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        return raw

    # Treat bare IP/hostname or host:port values as HTTP camera streams.
    if "." in raw and not raw.startswith("/"):
        host_part, sep, path = raw.partition("/")
        url = f"http://{host_part}"
        if sep:
            url += f"/{path}"
        else:
            url += "/video"
        return url

    return raw


def coerce_camera_capture_source(value):
    if isinstance(value, int):
        return value

    raw = str(value if value is not None else "").strip()
    if not raw:
        return CAMERA_INDEX

    if raw.lstrip("-").isdigit():
        try:
            return int(raw)
        except ValueError:
            return raw
    return raw


def infer_camera_transport(camera_type, source):
    normalized_type = str(camera_type or "usb").strip().lower()
    normalized_source = str(source if source is not None else "").strip().lower()
    if normalized_type in {"mobile", "phone"} or normalized_source.startswith("mobile://"):
        return "mobile"
    if normalized_type in {"rtsp", "mjpeg", "http", "https", "ip", "network"}:
        return "network"
    if normalized_source.startswith(("rtsp://", "http://", "https://")):
        return "network"
    return "local"


def build_camera_profile(profile_id, name, source, camera_type="usb", enabled=True):
    normalized_name = normalize_camera_name(name) or "Camera"
    source_value = normalize_camera_source(source)
    transport = infer_camera_transport(camera_type, source_value)
    camera_type_value = str(camera_type or "").strip().lower() or "usb"
    if transport == "mobile":
        camera_type_value = "mobile"
    if transport == "network":
        camera_type_value = "network"
    kind = "Network" if transport == "network" else "Mobile" if transport == "mobile" else "USB"
    return {
        "id": str(profile_id),
        "name": normalized_name,
        "source": source_value,
        "source_display": source_value,
        "type": camera_type_value,
        "transport": transport,
        "enabled": bool(enabled),
        "label": f"{normalized_name} • {kind}",
    }


def build_default_camera_profile():
    return build_camera_profile(
        profile_id=f"default-{CAMERA_INDEX}",
        name=f"Camera {CAMERA_INDEX}",
        source=str(CAMERA_INDEX),
        camera_type="usb",
        enabled=True,
    )


def load_camera_profiles_from_disk():
    if not os.path.exists(CAMERA_CONFIG_PATH):
        return [build_default_camera_profile()]

    try:
        with open(CAMERA_CONFIG_PATH, "r", encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
    except Exception:
        return [build_default_camera_profile()]

    if isinstance(payload, dict):
        raw_profiles = payload.get("list") or payload.get("cameras") or []
    elif isinstance(payload, list):
        raw_profiles = payload
    else:
        raw_profiles = []

    profiles = []

    for profile_data in raw_profiles:
        if not isinstance(profile_data, dict):
            continue
        profiles.append(
            build_camera_profile(
                profile_id=profile_data.get("id") or f"cam-{len(profiles) + 1}",
                name=profile_data.get("name"),
                source=profile_data.get("source"),
                camera_type=profile_data.get("type", profile_data.get("camera_type", "usb")),
                enabled=profile_data.get("enabled", True),
            )
        )
    return profiles


def save_camera_profiles_to_disk(profiles):
    directory = os.path.dirname(CAMERA_CONFIG_PATH) or PROJECT_ROOT
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    payload = {"list": profiles}
    with open(CAMERA_CONFIG_PATH, "w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, indent=2)


# ------------ Optional imports: Camera + YOLO -------------
try:
    import cv2
    import numpy as np

    CAMERA_AVAILABLE = True
except Exception:
    cv2 = None
    np = None
    CAMERA_AVAILABLE = False

# Some OpenCV builds include `cv2.face` but may omit the LBPH factory.
# The test suite expects `cv2.face.LBPHFaceRecognizer_create` to exist so it can be mocked.
if cv2 is not None:
    _face_module = getattr(cv2, "face", None)
    if _face_module is not None and not hasattr(_face_module, "LBPHFaceRecognizer_create"):
        def _lbph_face_recognizer_create(*_args, **_kwargs):
            raise AttributeError("LBPHFaceRecognizer_create not available in this OpenCV build")

        setattr(_face_module, "LBPHFaceRecognizer_create", _lbph_face_recognizer_create)

# YOLO (Ultralytics)
YOLO_AVAILABLE = False
YOLO_MODEL = None
YOLO_ENABLED = False
YOLO_STATUS_MESSAGE = "YOLO model not initialized"
YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "yolo11n.pt")
YOLO_CONFIDENCE = get_env_float("YOLO_CONFIDENCE", 0.45)
ALERT_OBJECTS = get_env_set("ALERT_OBJECTS", "person,cell phone")
ALERT_COOLDOWN_SECONDS = max(3, get_env_int("ALERT_COOLDOWN_SECONDS", 15))
CAMERA_INDEX = get_env_int("CAMERA_INDEX", 0)
CAMERA_CONFIG_PATH = resolve_project_path(os.getenv("CAMERA_CONFIG_PATH", "cameras.json"))
CAMERA_FALLBACK_DELAY = 0.25
MOBILE_CAMERA_FRAME_TTL_SECONDS = max(2, get_env_int("MOBILE_CAMERA_FRAME_TTL_SECONDS", 8))
MOBILE_CAMERA_FRAME_MAX_BYTES = max(64 * 1024, get_env_int("MOBILE_CAMERA_FRAME_MAX_BYTES", 3 * 1024 * 1024))
MOBILE_CAMERA_UPLOAD_INTERVAL_MS = max(150, get_env_int("MOBILE_CAMERA_UPLOAD_INTERVAL_MS", 350))
MOBILE_CAMERA_TOKEN = str(os.getenv("MOBILE_CAMERA_TOKEN") or "").strip()
DISABLE_FACE_RECOGNITION = get_env_bool("DISABLE_FACE_RECOGNITION", False)
KNOWN_FACES_DIR = resolve_project_path(os.getenv("KNOWN_FACES_DIR", "known_faces"))
FACE_RECOGNITION_THRESHOLD = get_env_float("FACE_RECOGNITION_THRESHOLD", 70)
FACE_IMAGE_SIZE = (160, 160)
FACE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
FACE_UPLOAD_MAX_FILES = max(1, get_env_int("FACE_UPLOAD_MAX_FILES", 6))
FACE_UPLOAD_MAX_BYTES = max(128 * 1024, get_env_int("FACE_UPLOAD_MAX_BYTES", 5 * 1024 * 1024))
PROFILE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PROFILE_UPLOAD_MAX_BYTES = max(128 * 1024, get_env_int("PROFILE_UPLOAD_MAX_BYTES", 5 * 1024 * 1024))
PROFILE_UPLOAD_DIR = resolve_project_path(os.getenv("PROFILE_UPLOAD_DIR", "static/uploads/profiles"))
AUTH_MIN_PASSWORD_LENGTH = 8

try:
    from ultralytics import YOLO

    YOLO_AVAILABLE = True
except Exception:
    YOLO_AVAILABLE = False

# ------------ Gemini AI (Google Generative AI) -------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")
GEMINI_CLIENT = None

try:
    import google.genai as genai

    if GEMINI_API_KEY:
        GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    GEMINI_AVAILABLE = True
except Exception:
    GEMINI_AVAILABLE = False
    genai = None
    GEMINI_CLIENT = None

IS_PRODUCTION = is_production_environment()
app = Flask(
    __name__,
    template_folder=os.path.join(PROJECT_ROOT, "templates"),
    static_folder=os.path.join(PROJECT_ROOT, "static"),
)
# Session security configuration
app.secret_key = os.getenv("FLASK_SECRET")
if IS_PRODUCTION and (
    not app.secret_key
    or app.secret_key == "change-me-before-production"
    or len(app.secret_key) < 32
):
    raise RuntimeError("FLASK_SECRET must be a strong value of at least 32 characters in production.")
if not app.secret_key:
    print("⚠️ WARNING: FLASK_SECRET environment variable is not set. Using an insecure default for development only.")
    print("⚠️ Set FLASK_SECRET in .env for production use!")
    app.secret_key = "dev-secret-change-in-production"

if not MOBILE_CAMERA_TOKEN:
    MOBILE_CAMERA_TOKEN = hashlib.sha256(str(app.secret_key).encode("utf-8")).hexdigest()[:20]

app.config.update(
    SESSION_COOKIE_SECURE=True if IS_PRODUCTION else get_env_bool("SESSION_COOKIE_SECURE", False),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=1),
    MAX_CONTENT_LENGTH=max(
        1024 * 1024,
        get_env_int("REQUEST_MAX_BYTES", 32 * 1024 * 1024),
    ),
)
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "app.db")
DEFAULT_DEVICE_STATE = {
    "light": "OFF",
    "fan": "OFF",
    "ac": "OFF",
    "tv": "OFF",
    "alarm": "OFF",
    "defense mode": "OFF",
}
VALID_USER_ROLES = {"admin", "user"}
VALID_ASSISTANT_MODES = {"hybrid", "manual", "ai", "research", "self_monitoring", "sentinel"}
VALID_CONTROL_MODES = {"self_monitoring", "manual"}
VALID_PROFILE_VISIBILITY = {"private", "team", "public"}
VALID_ACTIVITY_VISIBILITY = {"private", "admins", "team"}
VALID_SECURITY_RISK = {"normal", "suspicious", "high", "critical"}
DEFENSE_TRIGGER_OBJECTS = get_env_set("DEFENSE_TRIGGER_OBJECTS", "knife,gun,fire,smoke")
VALID_INCIDENT_SEVERITY = {"info", "warning", "error", "critical"}
VALID_INCIDENT_STATUS = {"open", "acknowledged", "resolved"}
VALID_DETECTION_TYPES = {"motion", "person", "face", "vehicle", "pet", "sound"}
VALID_PATROL_PRESETS = {"entry_sweep", "perimeter_scan", "night_guard", "idle"}
VALID_ACCESS_EVENT_TYPES = {
    "access_granted",
    "access_denied",
    "door_forced",
    "door_held",
    "tailgating",
    "lockdown",
    "panic",
}
INCIDENT_SNAPSHOT_DIR = resolve_project_path(
    os.getenv("INCIDENT_SNAPSHOT_DIR", "static/uploads/incidents")
)
INCIDENT_AUTO_CREATE = get_env_bool("INCIDENT_AUTO_CREATE", True)
INCIDENT_AUTO_SNAPSHOT = get_env_bool("INCIDENT_AUTO_SNAPSHOT", True)
INCIDENT_AUTO_COOLDOWN_SECONDS = max(10, get_env_int("INCIDENT_AUTO_COOLDOWN_SECONDS", 45))
ENABLE_EMAIL_NOTIFICATIONS = get_env_bool("ENABLE_EMAIL_NOTIFICATIONS", False)
SMTP_HOST = str(os.getenv("SMTP_HOST") or "").strip()
SMTP_PORT = get_env_int("SMTP_PORT", 587)
SMTP_USERNAME = str(os.getenv("SMTP_USERNAME") or "").strip()
SMTP_PASSWORD = str(os.getenv("SMTP_PASSWORD") or "").strip()
SMTP_SENDER = str(os.getenv("SMTP_SENDER") or "").strip()
SECURITY_ENABLE_2FA = get_env_bool("SECURITY_ENABLE_2FA", False)
HEALTH_CACHE_TTL_SECONDS = max(0.0, get_env_float("HEALTH_CACHE_TTL_SECONDS", 1.2))
ALLOW_LOCAL_VOICE_BYPASS = get_env_bool("ALLOW_LOCAL_VOICE_BYPASS", not IS_PRODUCTION)

DEFAULT_AUTOMATION_STATE = {
    "mode": "self_monitoring",
    "environment": {
        "temperature_c": 31.0,
        "ambient_light": 32.0,
        "humidity": 46.0,
        "security_risk": "normal",
    },
    "thresholds": {
        "temperature_high_c": 30.0,
        "temperature_low_c": 24.0,
        "ambient_light_low": 35.0,
        "ambient_light_high": 65.0,
    },
    "defense": {
        "armed": True,
        "auto_alarm": True,
        "auto_defense": True,
    },
    "last_actions": [],
    "last_reasons": [],
    "runtime_risk": "normal",
    "status": "Self monitoring is armed",
    "last_evaluated_at": None,
}

DEFAULT_POLICY_SNAPSHOT = {
    "retention_days": 14,
    "access_scope": "Authorized admins and operators only",
    "audio_recording": "Disabled by default unless explicitly justified",
    "data_rights": "Users can view, update, export-ready, and delete their profile data",
    "notice_required": True,
}

DEFAULT_CAMERA_INTELLIGENCE_STATE = {
    "privacy_mode": False,
    "sensitivity": 65,
    "detection": {
        "motion": True,
        "person": True,
        "face": True,
        "vehicle": False,
        "pet": False,
        "sound": False,
    },
    "activity_zones": [
        {
            "id": "entry-watch",
            "name": "Entry Watch",
            "enabled": True,
            "x": 8,
            "y": 12,
            "w": 38,
            "h": 42,
        },
        {
            "id": "asset-corner",
            "name": "Asset Corner",
            "enabled": True,
            "x": 58,
            "y": 18,
            "w": 32,
            "h": 36,
        },
    ],
    "quiet_hours": {
        "enabled": False,
        "start": "22:00",
        "end": "06:00",
    },
    "patrol": {
        "enabled": False,
        "preset": "entry_sweep",
        "interval_seconds": 300,
    },
    "deterrence": {
        "light": False,
        "siren": False,
    },
    "updated_at": None,
}

DEFAULT_ACCESS_CONTROL_STATE = {
    "doors": [
        {
            "id": "main-entry",
            "name": "Main Entry",
            "camera_id": "",
            "locked": True,
            "online": True,
        },
        {
            "id": "operations-door",
            "name": "Operations Door",
            "camera_id": "",
            "locked": True,
            "online": True,
        },
    ],
    "events": [],
    "lockdown": False,
    "updated_at": None,
}


def port_is_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def choose_start_port(preferred_port, attempts=5):
    for offset in range(attempts):
        candidate = preferred_port + offset
        if port_is_available(candidate):
            return candidate
    return preferred_port


def run_server():
    port = bounded_int(os.getenv("PORT", "5000"), 5000, minimum=1, maximum=65535)
    host = "0.0.0.0" if os.getenv("HOST_PUBLIC", "0") == "1" else "127.0.0.1"
    selected_port = choose_start_port(port)

    if selected_port != port:
        print(f"Port {port} busy hai, SmartAI {selected_port} par start ho raha hai.")

    print(f"Starting SmartAI Flask Server on {host}:{selected_port}")
    app.run(debug=FLASK_DEBUG, host=host, port=selected_port)


@app.after_request
def add_smartai_backend_header(response):
    response.headers["X-SmartAI-Backend"] = "1"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    if app.config.get("SESSION_COOKIE_SECURE"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

EMPTY_FRAME_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAME"
    "BgUGBgYFBgYGBwkIBgcJBwYGCAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUD"
    "AwUKBwYHCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoK"
    "CgoKCgr/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQF"
    "BgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEI"
    "I0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZ"
    "WZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxM"
    "XGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAA"
    "AAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdh"
    "cRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmq"
    "srO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMB"
    "AAIRAxEAPwD+f+iiigD/2Q=="
)

# ---------------------------------------------------------
# Helper: system uptime, processes, network
# ---------------------------------------------------------
def get_uptime():
    try:
        boot = datetime.datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.datetime.now() - boot
        days = uptime.days
        hrs, rem = divmod(uptime.seconds, 3600)
        mins, secs = divmod(rem, 60)
        return f"{days}d {hrs:02d}:{mins:02d}:{secs:02d}"
    except Exception:
        return "N/A"


def safe_psutil_pids_count():
    """psutil.pids() can fail with PermissionError in restricted environments."""
    try:
        return len(psutil.pids())
    except Exception:
        return 0


def get_network_usage_mb():
    try:
        net = psutil.net_io_counters()
        sent_mb = round(net.bytes_sent / (1024 * 1024), 2)
        recv_mb = round(net.bytes_recv / (1024 * 1024), 2)
        return sent_mb, recv_mb
    except Exception:
        return 0.0, 0.0


# ---------------------------------------------------------
# Global alert state (camera + system)
# ---------------------------------------------------------
latest_alert = "✅ No alerts"
latest_alert_lock = threading.Lock()
device_state = {}
health_cache = {"by_user": {}}
health_cache_lock = threading.Lock()

# --- Simple SQLite persistence (users, devices, activity) ---
def db_now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def normalize_device_name(value):
    return " ".join(str(value or "").split())


def normalize_person_name(value):
    return " ".join(str(value or "").strip().split())


def normalize_profile_text(value, max_length=280):
    text = " ".join(str(value or "").strip().split())
    return text[:max_length]


def normalize_phone_number(value):
    value = str(value or "").strip()
    allowed = {"+", "-", " ", "(", ")"}
    filtered = "".join(ch for ch in value if ch.isdigit() or ch in allowed)
    return filtered[:32]


def normalize_choice(value, allowed, default):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def coerce_bool_flag(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def get_db_path():
    return os.getenv("APP_DB_PATH", DEFAULT_DB_PATH)


def get_db_conn():
    timeout_seconds = max(1.0, get_env_float("SQLITE_TIMEOUT_SECONDS", 30))
    conn = sqlite3.connect(get_db_path(), timeout=timeout_seconds, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
    return conn


def get_table_columns(conn, table_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def ensure_column(conn, table_name, column_name, definition):
    if column_name in get_table_columns(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def init_db():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_login_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            name TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_email TEXT,
            actor_role TEXT,
            action TEXT NOT NULL,
            target_type TEXT,
            target_name TEXT,
            source TEXT NOT NULL DEFAULT 'ui',
            details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'warning',
            status TEXT NOT NULL DEFAULT 'open',
            source TEXT NOT NULL DEFAULT 'system',
            camera_id TEXT,
            snapshot_path TEXT,
            risk_score INTEGER,
            tags TEXT,
            details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            acknowledged_by TEXT,
            acknowledged_at TEXT,
            resolved_by TEXT,
            resolved_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT,
            level TEXT NOT NULL DEFAULT 'info',
            type TEXT NOT NULL DEFAULT 'system',
            message TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            read_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ip_address TEXT,
            user_agent TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    ensure_column(conn, "users", "full_name", "full_name TEXT")
    ensure_column(conn, "users", "role", "role TEXT NOT NULL DEFAULT 'user'")
    ensure_column(conn, "users", "created_at", "created_at TEXT")
    ensure_column(conn, "users", "last_login_at", "last_login_at TEXT")
    ensure_column(conn, "users", "avatar_path", "avatar_path TEXT")
    ensure_column(conn, "users", "bio", "bio TEXT")
    ensure_column(conn, "users", "phone", "phone TEXT")
    ensure_column(conn, "users", "location", "location TEXT")
    ensure_column(conn, "users", "profile_visibility", "profile_visibility TEXT NOT NULL DEFAULT 'team'")
    ensure_column(conn, "users", "activity_visibility", "activity_visibility TEXT NOT NULL DEFAULT 'admins'")
    ensure_column(conn, "users", "alert_opt_in", "alert_opt_in INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "users", "face_enrollment_opt_in", "face_enrollment_opt_in INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "users", "privacy_policy_acknowledged_at", "privacy_policy_acknowledged_at TEXT")
    ensure_column(conn, "users", "two_factor_enabled", "two_factor_enabled INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "users", "two_factor_method", "two_factor_method TEXT")
    ensure_column(conn, "users", "two_factor_secret", "two_factor_secret TEXT")
    ensure_column(conn, "users", "session_revoked_at", "session_revoked_at TEXT")
    ensure_column(conn, "users", "last_login_ip", "last_login_ip TEXT")
    ensure_column(conn, "users", "last_login_user_agent", "last_login_user_agent TEXT")
    ensure_column(conn, "devices", "created_at", "created_at TEXT")
    ensure_column(conn, "devices", "updated_at", "updated_at TEXT")

    now_value = db_now_iso()
    cur.execute(
        "UPDATE users SET created_at = ? WHERE created_at IS NULL OR created_at = ''",
        (now_value,),
    )
    cur.execute(
        "UPDATE users SET role = 'user' WHERE role IS NULL OR role = ''",
    )
    cur.execute(
        "UPDATE users SET profile_visibility = 'team' WHERE profile_visibility IS NULL OR profile_visibility = ''",
    )
    cur.execute(
        "UPDATE users SET activity_visibility = 'admins' WHERE activity_visibility IS NULL OR activity_visibility = ''",
    )
    cur.execute(
        "UPDATE users SET alert_opt_in = 1 WHERE alert_opt_in IS NULL",
    )
    cur.execute(
        "UPDATE users SET face_enrollment_opt_in = 1 WHERE face_enrollment_opt_in IS NULL",
    )
    cur.execute(
        "UPDATE users SET two_factor_enabled = 0 WHERE two_factor_enabled IS NULL",
    )
    cur.execute(
        "UPDATE devices SET created_at = ? WHERE created_at IS NULL OR created_at = ''",
        (now_value,),
    )
    cur.execute(
        """
        UPDATE devices
        SET updated_at = COALESCE(updated_at, created_at, ?)
        WHERE updated_at IS NULL OR updated_at = ''
        """,
        (now_value,),
    )
    cur.execute("SELECT COUNT(*) AS count FROM users WHERE role = 'admin'")
    if cur.fetchone()["count"] == 0:
        cur.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1")
        first_user = cur.fetchone()
        if first_user:
            cur.execute("UPDATE users SET role = 'admin' WHERE id = ?", (first_user["id"],))
    conn.commit()
    conn.close()


def serialize_user_row(row):
    if row is None:
        return None

    display_name = normalize_person_name(row["full_name"])
    if not display_name:
        display_name = row["email"].split("@", 1)[0]

    return {
        "email": row["email"],
        "full_name": normalize_person_name(row["full_name"]),
        "display_name": display_name,
        "initials": build_profile_initials(display_name),
        "avatar_seed": build_avatar_seed(row["email"]),
        "avatar_url": build_profile_avatar_url(row["avatar_path"]),
        "avatar_path": row["avatar_path"] or "",
        "bio": normalize_profile_text(row["bio"], max_length=320),
        "phone": normalize_phone_number(row["phone"]),
        "location": normalize_profile_text(row["location"], max_length=120),
        "profile_visibility": normalize_choice(row["profile_visibility"], VALID_PROFILE_VISIBILITY, "team"),
        "activity_visibility": normalize_choice(row["activity_visibility"], VALID_ACTIVITY_VISIBILITY, "admins"),
        "alert_opt_in": bool(row["alert_opt_in"]),
        "face_enrollment_opt_in": bool(row["face_enrollment_opt_in"]),
        "privacy_policy_acknowledged_at": row["privacy_policy_acknowledged_at"],
        "two_factor_enabled": bool(row["two_factor_enabled"]) if "two_factor_enabled" in row.keys() else False,
        "two_factor_method": str(row["two_factor_method"] or "") if "two_factor_method" in row.keys() else "",
        "session_revoked_at": row["session_revoked_at"] if "session_revoked_at" in row.keys() else None,
        "last_login_ip": str(row["last_login_ip"] or "") if "last_login_ip" in row.keys() else "",
        "last_login_user_agent": str(row["last_login_user_agent"] or "") if "last_login_user_agent" in row.keys() else "",
        "role": (row["role"] or "user").lower(),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def build_profile_avatar_url(stored_path):
    relative_path = str(stored_path or "").strip().replace("\\", "/").lstrip("/")
    if not relative_path:
        return ""
    return f"/{relative_path}"


def get_user_by_email(email):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return None

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            id,
            email,
            password,
            full_name,
            role,
            created_at,
            last_login_at,
            avatar_path,
            bio,
            phone,
            location,
            profile_visibility,
            activity_visibility,
            alert_opt_in,
            face_enrollment_opt_in,
            privacy_policy_acknowledged_at,
            two_factor_enabled,
            two_factor_method,
            two_factor_secret,
            session_revoked_at,
            last_login_ip,
            last_login_user_agent
        FROM users
        WHERE email = ?
        """,
        (normalized_email,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_user_role(email, default="user"):
    row = get_user_by_email(email)
    if row is None:
        return default
    role = str(row["role"] or default).strip().lower()
    return role if role in VALID_USER_ROLES else default


def create_user(email: str, password: str, full_name: Optional[str] = None):
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        
        # 🔥 YAHI CHANGE KARNA HAI
        hashed = generate_password_hash(password, method="pbkdf2:sha256")
        
        role = "admin" if not has_users() else "user"
        cur.execute(
            """
            INSERT INTO users (email, password, full_name, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (email, hashed, normalize_person_name(full_name), role, db_now_iso()),
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "An account with this email already exists."
    except Exception:
        return False, "Unable to create account right now."
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def verify_user(email: str, password: str) -> bool:
    row = get_user_by_email(email)
    if not row:
        return False
    return check_password_hash(row["password"], password)


def update_last_login(email):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_login_at = ? WHERE email = ?",
        (db_now_iso(), normalized_email),
    )
    conn.commit()
    conn.close()


def has_users() -> bool:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row is not None


def count_users():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS count FROM users")
    count = cur.fetchone()["count"]
    conn.close()
    return int(count)


def count_admin_users():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS count FROM users WHERE role = 'admin'")
    count = cur.fetchone()["count"]
    conn.close()
    return int(count)


def list_users_db():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            email,
            full_name,
            role,
            created_at,
            last_login_at,
            avatar_path,
            bio,
            phone,
            location,
            profile_visibility,
            activity_visibility,
            alert_opt_in,
            face_enrollment_opt_in,
            privacy_policy_acknowledged_at
        FROM users
        ORDER BY LOWER(email)
        """
    )
    rows = cur.fetchall()
    conn.close()
    return [serialize_user_row(row) for row in rows]


def update_user_role_db(email, role):
    normalized_email = normalize_email(email)
    normalized_role = str(role or "").strip().lower()
    if not normalized_email or normalized_role not in VALID_USER_ROLES:
        return False

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET role = ? WHERE email = ?",
        (normalized_role, normalized_email),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def update_user_profile_db(email, payload):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return None

    full_name = normalize_person_name(payload.get("full_name"))
    bio = normalize_profile_text(payload.get("bio"), max_length=320)
    phone = normalize_phone_number(payload.get("phone"))
    location = normalize_profile_text(payload.get("location"), max_length=120)
    profile_visibility = normalize_choice(payload.get("profile_visibility"), VALID_PROFILE_VISIBILITY, "team")
    activity_visibility = normalize_choice(payload.get("activity_visibility"), VALID_ACTIVITY_VISIBILITY, "admins")
    alert_opt_in = 1 if coerce_bool_flag(payload.get("alert_opt_in"), True) else 0
    face_enrollment_opt_in = 1 if coerce_bool_flag(payload.get("face_enrollment_opt_in"), True) else 0
    privacy_ack = db_now_iso() if coerce_bool_flag(payload.get("privacy_policy_acknowledged"), False) else None

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET
            full_name = ?,
            bio = ?,
            phone = ?,
            location = ?,
            profile_visibility = ?,
            activity_visibility = ?,
            alert_opt_in = ?,
            face_enrollment_opt_in = ?,
            privacy_policy_acknowledged_at = COALESCE(?, privacy_policy_acknowledged_at)
        WHERE email = ?
        """,
        (
            full_name,
            bio,
            phone,
            location,
            profile_visibility,
            activity_visibility,
            alert_opt_in,
            face_enrollment_opt_in,
            privacy_ack,
            normalized_email,
        ),
    )
    conn.commit()
    conn.close()
    return serialize_user_row(get_user_by_email(normalized_email))


def update_user_avatar_path_db(email, avatar_path):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return None

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET avatar_path = ? WHERE email = ?",
        (str(avatar_path or "").strip(), normalized_email),
    )
    conn.commit()
    conn.close()
    return serialize_user_row(get_user_by_email(normalized_email))


def build_avatar_seed(value):
    digest = hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()
    return int(digest[:6], 16) % 360


def build_profile_initials(value):
    parts = [part for part in normalize_person_name(value).split(" ") if part]
    if not parts:
        return "SA"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


def count_user_activity(email):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return 0

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS count FROM activity_logs WHERE actor_email = ?",
        (normalized_email,),
    )
    count = cur.fetchone()["count"]
    conn.close()
    return int(count)


def build_user_profile(email):
    row = get_user_by_email(email)
    serialized = serialize_user_row(row)
    if serialized is None:
        return None

    serialized["activity_count"] = count_user_activity(email)
    serialized["device_count"] = len(list_devices_db())
    serialized["assistant_ready"] = True
    serialized["gemini_configured"] = bool(GEMINI_API_KEY and GEMINI_AVAILABLE)
    serialized["policy"] = copy.deepcopy(DEFAULT_POLICY_SNAPSHOT)
    return serialized


def get_setting_value_db(key, default=""):
    normalized_key = str(key or "").strip()
    if not normalized_key:
        return default

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT value FROM system_settings WHERE key = ? LIMIT 1",
        (normalized_key,),
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        return default
    return row["value"]


def set_setting_value_db(key, value):
    normalized_key = str(key or "").strip()
    if not normalized_key:
        return

    conn = get_db_conn()
    cur = conn.cursor()
    now_value = db_now_iso()
    cur.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (normalized_key, str(value), now_value),
    )
    conn.commit()
    conn.close()


def get_json_setting_db(key, default):
    raw_value = get_setting_value_db(key, "")
    if not raw_value:
        return copy.deepcopy(default)

    try:
        parsed = json.loads(raw_value)
    except Exception:
        return copy.deepcopy(default)

    if not isinstance(parsed, type(default)):
        return copy.deepcopy(default)
    return parsed


def set_json_setting_db(key, value):
    set_setting_value_db(key, json.dumps(value, ensure_ascii=True))


def deep_merge_dict(base, override):
    merged = copy.deepcopy(base)
    if not isinstance(override, dict):
        return merged

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_time_hhmm(value, default):
    text = str(value or "").strip()
    if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", text):
        return text
    return default


def normalize_zone_id(value, fallback):
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return normalized or fallback


def normalize_activity_zone(zone, index=1):
    if not isinstance(zone, dict):
        zone = {}
    default_zone = DEFAULT_CAMERA_INTELLIGENCE_STATE["activity_zones"][
        min(index - 1, len(DEFAULT_CAMERA_INTELLIGENCE_STATE["activity_zones"]) - 1)
    ]
    zone_id = normalize_zone_id(zone.get("id") or zone.get("name"), f"zone-{index}")
    name = normalize_profile_text(zone.get("name") or default_zone.get("name") or f"Zone {index}", max_length=48)
    return {
        "id": zone_id,
        "name": name or f"Zone {index}",
        "enabled": coerce_bool_flag(zone.get("enabled"), default_zone.get("enabled", True)),
        "x": int(clamp_number(zone.get("x"), default_zone.get("x", 0), 0, 100)),
        "y": int(clamp_number(zone.get("y"), default_zone.get("y", 0), 0, 100)),
        "w": int(clamp_number(zone.get("w"), default_zone.get("w", 25), 1, 100)),
        "h": int(clamp_number(zone.get("h"), default_zone.get("h", 25), 1, 100)),
    }


def sanitize_camera_intelligence_state(value):
    state = deep_merge_dict(DEFAULT_CAMERA_INTELLIGENCE_STATE, value if isinstance(value, dict) else {})
    state["privacy_mode"] = coerce_bool_flag(state.get("privacy_mode"), False)
    state["sensitivity"] = int(clamp_number(state.get("sensitivity"), 65, 1, 100))

    detection_payload = state.get("detection") if isinstance(state.get("detection"), dict) else {}
    state["detection"] = {
        key: coerce_bool_flag(detection_payload.get(key), DEFAULT_CAMERA_INTELLIGENCE_STATE["detection"].get(key, False))
        for key in sorted(VALID_DETECTION_TYPES)
    }

    zones = state.get("activity_zones") if isinstance(state.get("activity_zones"), list) else []
    normalized_zones = [
        normalize_activity_zone(zone, index=index)
        for index, zone in enumerate(zones[:6], start=1)
    ]
    if not normalized_zones:
        normalized_zones = [
            normalize_activity_zone(zone, index=index)
            for index, zone in enumerate(DEFAULT_CAMERA_INTELLIGENCE_STATE["activity_zones"], start=1)
        ]
    state["activity_zones"] = normalized_zones

    quiet_payload = state.get("quiet_hours") if isinstance(state.get("quiet_hours"), dict) else {}
    state["quiet_hours"] = {
        "enabled": coerce_bool_flag(quiet_payload.get("enabled"), False),
        "start": normalize_time_hhmm(quiet_payload.get("start"), DEFAULT_CAMERA_INTELLIGENCE_STATE["quiet_hours"]["start"]),
        "end": normalize_time_hhmm(quiet_payload.get("end"), DEFAULT_CAMERA_INTELLIGENCE_STATE["quiet_hours"]["end"]),
    }

    patrol_payload = state.get("patrol") if isinstance(state.get("patrol"), dict) else {}
    preset = str(patrol_payload.get("preset") or DEFAULT_CAMERA_INTELLIGENCE_STATE["patrol"]["preset"]).strip().lower()
    state["patrol"] = {
        "enabled": coerce_bool_flag(patrol_payload.get("enabled"), False),
        "preset": preset if preset in VALID_PATROL_PRESETS else DEFAULT_CAMERA_INTELLIGENCE_STATE["patrol"]["preset"],
        "interval_seconds": int(clamp_number(patrol_payload.get("interval_seconds"), 300, 30, 3600)),
    }

    deterrence_payload = state.get("deterrence") if isinstance(state.get("deterrence"), dict) else {}
    state["deterrence"] = {
        "light": coerce_bool_flag(deterrence_payload.get("light"), False),
        "siren": coerce_bool_flag(deterrence_payload.get("siren"), False),
    }
    state["updated_at"] = state.get("updated_at")
    return state


def get_camera_intelligence_state():
    return sanitize_camera_intelligence_state(
        get_json_setting_db("camera_intelligence_state", {})
    )


def save_camera_intelligence_state(state):
    sanitized = sanitize_camera_intelligence_state(state)
    sanitized["updated_at"] = now_iso()
    set_json_setting_db("camera_intelligence_state", sanitized)
    return sanitized


def update_camera_intelligence_state(payload):
    state = get_camera_intelligence_state()
    if not isinstance(payload, dict):
        payload = {}

    if "privacy_mode" in payload:
        state["privacy_mode"] = coerce_bool_flag(payload.get("privacy_mode"), state["privacy_mode"])
    if "sensitivity" in payload:
        state["sensitivity"] = int(clamp_number(payload.get("sensitivity"), state["sensitivity"], 1, 100))
    if isinstance(payload.get("detection"), dict):
        for key, value in payload["detection"].items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in VALID_DETECTION_TYPES:
                state["detection"][normalized_key] = coerce_bool_flag(value, state["detection"].get(normalized_key, False))
    if isinstance(payload.get("activity_zones"), list):
        state["activity_zones"] = [
            normalize_activity_zone(zone, index=index)
            for index, zone in enumerate(payload["activity_zones"][:6], start=1)
        ] or state["activity_zones"]
    if isinstance(payload.get("quiet_hours"), dict):
        quiet = payload["quiet_hours"]
        state["quiet_hours"]["enabled"] = coerce_bool_flag(quiet.get("enabled"), state["quiet_hours"]["enabled"])
        state["quiet_hours"]["start"] = normalize_time_hhmm(quiet.get("start"), state["quiet_hours"]["start"])
        state["quiet_hours"]["end"] = normalize_time_hhmm(quiet.get("end"), state["quiet_hours"]["end"])
    if isinstance(payload.get("patrol"), dict):
        patrol = payload["patrol"]
        state["patrol"]["enabled"] = coerce_bool_flag(patrol.get("enabled"), state["patrol"]["enabled"])
        preset = str(patrol.get("preset") or state["patrol"]["preset"]).strip().lower()
        state["patrol"]["preset"] = preset if preset in VALID_PATROL_PRESETS else state["patrol"]["preset"]
        state["patrol"]["interval_seconds"] = int(clamp_number(patrol.get("interval_seconds"), state["patrol"]["interval_seconds"], 30, 3600))
    if isinstance(payload.get("deterrence"), dict):
        for key in ("light", "siren"):
            if key in payload["deterrence"]:
                state["deterrence"][key] = coerce_bool_flag(payload["deterrence"].get(key), state["deterrence"][key])

    return save_camera_intelligence_state(state)


def normalize_access_event_type(value):
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in VALID_ACCESS_EVENT_TYPES else "access_granted"


def normalize_access_door(door, index=1):
    if not isinstance(door, dict):
        door = {}
    default_door = DEFAULT_ACCESS_CONTROL_STATE["doors"][
        min(index - 1, len(DEFAULT_ACCESS_CONTROL_STATE["doors"]) - 1)
    ]
    door_id = normalize_zone_id(door.get("id") or door.get("name"), f"door-{index}")
    return {
        "id": door_id,
        "name": normalize_profile_text(door.get("name") or default_door.get("name") or f"Door {index}", max_length=64),
        "camera_id": str(door.get("camera_id") or default_door.get("camera_id") or "").strip(),
        "locked": coerce_bool_flag(door.get("locked"), default_door.get("locked", True)),
        "online": coerce_bool_flag(door.get("online"), default_door.get("online", True)),
    }


def sanitize_access_control_state(value):
    state = deep_merge_dict(DEFAULT_ACCESS_CONTROL_STATE, value if isinstance(value, dict) else {})
    doors = state.get("doors") if isinstance(state.get("doors"), list) else []
    state["doors"] = [
        normalize_access_door(door, index=index)
        for index, door in enumerate(doors[:24], start=1)
    ] or [
        normalize_access_door(door, index=index)
        for index, door in enumerate(DEFAULT_ACCESS_CONTROL_STATE["doors"], start=1)
    ]
    events = state.get("events") if isinstance(state.get("events"), list) else []
    state["events"] = [event for event in events[:80] if isinstance(event, dict)]
    state["lockdown"] = coerce_bool_flag(state.get("lockdown"), False)
    state["updated_at"] = state.get("updated_at")
    return state


def get_access_control_state():
    return sanitize_access_control_state(get_json_setting_db("access_control_state", {}))


def save_access_control_state(state):
    sanitized = sanitize_access_control_state(state)
    sanitized["updated_at"] = db_now_iso()
    set_json_setting_db("access_control_state", sanitized)
    return sanitized


def find_access_door(state, door_id_or_name):
    lookup = str(door_id_or_name or "").strip().lower()
    doors = state.get("doors") or []
    if not lookup and doors:
        return doors[0]
    for door in doors:
        if lookup in {str(door.get("id") or "").lower(), str(door.get("name") or "").lower()}:
            return door
    return doors[0] if doors else normalize_access_door({}, 1)


def access_event_severity(event_type):
    if event_type in {"door_forced", "panic", "lockdown"}:
        return "critical"
    if event_type in {"door_held", "tailgating", "access_denied"}:
        return "warning"
    return "info"


def record_access_event(payload, *, actor_email=None, actor_role=None, source="access"):
    data = payload if isinstance(payload, dict) else {}
    state = get_access_control_state()
    event_type = normalize_access_event_type(data.get("event_type") or data.get("type"))
    door = find_access_door(state, data.get("door_id") or data.get("door") or data.get("door_name"))
    active_camera = get_active_camera_profile(refresh=False)
    camera_id = str(data.get("camera_id") or door.get("camera_id") or active_camera.get("id") or "").strip()
    severity = access_event_severity(event_type)
    created_at = db_now_iso()
    event = {
        "id": f"access-{int(time.time() * 1000)}-{secrets.token_hex(2)}",
        "event_type": event_type,
        "door_id": door.get("id"),
        "door_name": door.get("name"),
        "actor": normalize_profile_text(data.get("actor") or data.get("person") or actor_email or "Unknown", max_length=80),
        "status": "allowed" if event_type == "access_granted" else "attention",
        "severity": severity,
        "camera_id": camera_id,
        "camera_name": active_camera.get("name") if active_camera.get("id") == camera_id else "",
        "created_at": created_at,
        "details": data.get("details") if isinstance(data.get("details"), dict) else {},
    }

    if event_type == "lockdown":
        state["lockdown"] = True
    state["events"] = [event, *list(state.get("events") or [])][:80]
    saved = save_access_control_state(state)

    log_activity(
        "access_event_recorded",
        actor_email=actor_email,
        actor_role=actor_role,
        target_type="access",
        target_name=door.get("name"),
        source=source,
        details={"event_type": event_type, "severity": severity, "camera_id": camera_id},
    )

    if severity in {"warning", "critical"}:
        create_notification(
            f"Access event: {event_type.replace('_', ' ')} at {door.get('name')}",
            level="error" if severity == "critical" else "warning",
            type="access",
            details={"event_id": event["id"], "door_id": door.get("id"), "camera_id": camera_id},
        )
    if severity == "critical":
        create_incident(
            f"Access control alert: {event_type.replace('_', ' ')}",
            severity="critical",
            status="open",
            source="access",
            camera_id=camera_id,
            tags=["access", event_type],
            details={"access_event": event, "risk_level": "critical", "risk_score": 92},
            actor_email=actor_email,
        )

    return event, saved


def get_automation_state():
    stored = get_json_setting_db("automation_state", {})
    state = deep_merge_dict(DEFAULT_AUTOMATION_STATE, stored)
    state["mode"] = normalize_choice(state.get("mode"), VALID_CONTROL_MODES, DEFAULT_AUTOMATION_STATE["mode"])
    environment = state.get("environment", {})
    thresholds = state.get("thresholds", {})
    defense = state.get("defense", {})
    state["environment"] = {
        "temperature_c": float(environment.get("temperature_c", DEFAULT_AUTOMATION_STATE["environment"]["temperature_c"])),
        "ambient_light": float(environment.get("ambient_light", DEFAULT_AUTOMATION_STATE["environment"]["ambient_light"])),
        "humidity": float(environment.get("humidity", DEFAULT_AUTOMATION_STATE["environment"]["humidity"])),
        "security_risk": normalize_choice(
            environment.get("security_risk"),
            VALID_SECURITY_RISK,
            DEFAULT_AUTOMATION_STATE["environment"]["security_risk"],
        ),
    }
    state["thresholds"] = {
        "temperature_high_c": float(thresholds.get("temperature_high_c", DEFAULT_AUTOMATION_STATE["thresholds"]["temperature_high_c"])),
        "temperature_low_c": float(thresholds.get("temperature_low_c", DEFAULT_AUTOMATION_STATE["thresholds"]["temperature_low_c"])),
        "ambient_light_low": float(thresholds.get("ambient_light_low", DEFAULT_AUTOMATION_STATE["thresholds"]["ambient_light_low"])),
        "ambient_light_high": float(thresholds.get("ambient_light_high", DEFAULT_AUTOMATION_STATE["thresholds"]["ambient_light_high"])),
    }
    state["defense"] = {
        "armed": coerce_bool_flag(defense.get("armed"), True),
        "auto_alarm": coerce_bool_flag(defense.get("auto_alarm"), True),
        "auto_defense": coerce_bool_flag(defense.get("auto_defense"), True),
    }
    state["last_actions"] = list(state.get("last_actions") or [])
    state["last_reasons"] = list(state.get("last_reasons") or [])
    state["runtime_risk"] = normalize_choice(state.get("runtime_risk"), VALID_SECURITY_RISK, "normal")
    state["status"] = str(state.get("status") or DEFAULT_AUTOMATION_STATE["status"]).strip()
    state["last_evaluated_at"] = state.get("last_evaluated_at")
    return state


def save_automation_state(state):
    set_json_setting_db("automation_state", state)


def ensure_profile_upload_dir():
    os.makedirs(PROFILE_UPLOAD_DIR, exist_ok=True)


def safe_remove_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        return


def build_profile_photo_filename(email, original_name):
    extension = os.path.splitext(str(original_name or ""))[1].lower()
    if extension not in PROFILE_IMAGE_EXTENSIONS:
        extension = ".png"
    email_hash = hashlib.sha1(normalize_email(email).encode("utf-8")).hexdigest()[:12]
    return f"profile-{email_hash}-{int(time.time() * 1000)}{extension}"


def resolve_profile_photo_path(stored_path):
    cleaned = str(stored_path or "").strip().replace("\\", "/").lstrip("/")
    if not cleaned:
        return ""
    return os.path.join(PROJECT_ROOT, cleaned)


def save_profile_photo(email, file_storage):
    if file_storage is None or not getattr(file_storage, "filename", ""):
        return None, "Please choose an image."

    extension = os.path.splitext(str(file_storage.filename or ""))[1].lower()
    if extension and extension not in PROFILE_IMAGE_EXTENSIONS:
        return None, "Only JPG, PNG, or WEBP profile photos are supported."

    raw_bytes = file_storage.read()
    try:
        file_storage.stream.seek(0)
    except Exception:
        pass

    if not raw_bytes:
        return None, "The selected photo is empty."
    if len(raw_bytes) > PROFILE_UPLOAD_MAX_BYTES:
        return None, "Profile photo is too large."

    ensure_profile_upload_dir()
    profile = build_user_profile(email) or {}
    previous_path = resolve_profile_photo_path(profile.get("avatar_path"))
    file_name = build_profile_photo_filename(email, file_storage.filename)
    relative_path = os.path.join("static", "uploads", "profiles", file_name).replace("\\", "/")
    absolute_path = os.path.join(PROFILE_UPLOAD_DIR, file_name)

    with open(absolute_path, "wb") as output_handle:
        output_handle.write(raw_bytes)

    update_user_avatar_path_db(email, relative_path)
    if previous_path and previous_path != absolute_path:
        safe_remove_file(previous_path)
    return build_user_profile(email), None


def delete_profile_photo(email):
    profile = build_user_profile(email)
    if profile is None:
        return None, "Profile not found."

    existing_path = resolve_profile_photo_path(profile.get("avatar_path"))
    update_user_avatar_path_db(email, "")
    safe_remove_file(existing_path)
    return build_user_profile(email), None


def delete_user_account_db(email):
    profile = build_user_profile(email)
    if profile is None:
        return False, "Profile not found."

    if profile["role"] == "admin" and count_admin_users() <= 1 and count_users() > 1:
        return False, "Create another admin before deleting this admin profile."

    avatar_path = resolve_profile_photo_path(profile.get("avatar_path"))
    normalized_email = normalize_email(email)
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE email = ?", (normalized_email,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()

    if deleted:
        safe_remove_file(avatar_path)
    return deleted, None if deleted else "Profile not found."


def log_activity(
    action,
    actor_email=None,
    actor_role=None,
    target_type=None,
    target_name=None,
    source="ui",
    details=None,
):
    try:
        normalized_actor = normalize_email(actor_email) if actor_email else None
        normalized_role = str(actor_role or "").strip().lower() or None
        if normalized_role not in VALID_USER_ROLES:
            normalized_role = get_user_role(normalized_actor, default="user") if normalized_actor else None

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO activity_logs (
                actor_email,
                actor_role,
                action,
                target_type,
                target_name,
                source,
                details,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_actor,
                normalized_role,
                action,
                target_type,
                target_name,
                source,
                json.dumps(details or {}, ensure_ascii=True),
                db_now_iso(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        return


def list_activity_logs(limit=25):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, actor_email, actor_role, action, target_type, target_name, source, details, created_at
        FROM activity_logs
        ORDER BY id DESC
        LIMIT ?
        """,
        (bounded_int(limit, 25, maximum=1000),),
    )
    rows = cur.fetchall()
    conn.close()

    items = []
    for row in rows:
        details = {}
        try:
            details = json.loads(row["details"] or "{}")
        except Exception:
            details = {}
        items.append(
            {
                "id": row["id"],
                "actor_email": row["actor_email"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "target_type": row["target_type"],
                "target_name": row["target_name"],
                "source": row["source"],
                "details": details,
                "created_at": row["created_at"],
            }
        )
    return items


def normalize_incident_severity(value, default="warning"):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_INCIDENT_SEVERITY else default


def normalize_incident_status(value, default="open"):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_INCIDENT_STATUS else default


def ensure_directory(path):
    directory = str(path or "").strip()
    if not directory:
        return False
    os.makedirs(directory, exist_ok=True)
    return True


def write_incident_snapshot(snapshot_bytes, suffix="jpg"):
    if not snapshot_bytes:
        return ""
    if not ensure_directory(INCIDENT_SNAPSHOT_DIR):
        return ""

    safe_suffix = re.sub(r"[^a-z0-9]+", "", str(suffix or "jpg").lower()) or "jpg"
    filename = f"incident-{int(time.time() * 1000)}-{secrets.token_hex(3)}.{safe_suffix}"
    full_path = os.path.join(INCIDENT_SNAPSHOT_DIR, filename)
    try:
        with open(full_path, "wb") as file_handle:
            file_handle.write(snapshot_bytes)
    except Exception:
        return ""
    relative_path = os.path.relpath(full_path, PROJECT_ROOT).replace("\\", "/")
    return relative_path


def create_incident(
    title,
    *,
    severity="warning",
    status="open",
    source="system",
    camera_id=None,
    snapshot_bytes=None,
    risk_score=None,
    tags=None,
    details=None,
    actor_email=None,
):
    normalized_title = " ".join(str(title or "").strip().split())[:160]
    if not normalized_title:
        normalized_title = "Incident"
    severity_value = normalize_incident_severity(severity)
    status_value = normalize_incident_status(status)
    now_value = db_now_iso()

    snapshot_path = ""
    if snapshot_bytes and INCIDENT_AUTO_SNAPSHOT:
        snapshot_path = write_incident_snapshot(snapshot_bytes)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO incidents (
            title,
            severity,
            status,
            source,
            camera_id,
            snapshot_path,
            risk_score,
            tags,
            details,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalized_title,
            severity_value,
            status_value,
            str(source or "system"),
            str(camera_id or "").strip() or None,
            snapshot_path or None,
            int(risk_score) if isinstance(risk_score, (int, float)) else None,
            json.dumps(tags or [], ensure_ascii=True),
            json.dumps(details or {}, ensure_ascii=True),
            now_value,
            now_value,
        ),
    )
    incident_id = cur.lastrowid
    conn.commit()
    conn.close()

    log_activity(
        "incident_created",
        actor_email=actor_email,
        actor_role=current_user_role() if actor_email else None,
        target_type="incident",
        target_name=str(incident_id),
        source="incident",
        details={
            "title": normalized_title,
            "severity": severity_value,
            "status": status_value,
            "source": source,
            "camera_id": camera_id,
        },
    )
    return get_incident(incident_id)


def get_incident(incident_id):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, severity, status, source, camera_id, snapshot_path, risk_score, tags, details,
               created_at, updated_at, acknowledged_by, acknowledged_at, resolved_by, resolved_at
        FROM incidents
        WHERE id = ?
        """,
        (int(incident_id),),
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None

    try:
        tags = json.loads(row["tags"] or "[]")
    except Exception:
        tags = []
    try:
        details = json.loads(row["details"] or "{}")
    except Exception:
        details = {}

    return {
        "id": row["id"],
        "title": row["title"],
        "severity": normalize_incident_severity(row["severity"]),
        "status": normalize_incident_status(row["status"]),
        "source": row["source"],
        "camera_id": row["camera_id"] or "",
        "snapshot_url": build_profile_avatar_url(row["snapshot_path"]) if row["snapshot_path"] else "",
        "risk_score": row["risk_score"],
        "tags": tags if isinstance(tags, list) else [],
        "details": details if isinstance(details, dict) else {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "acknowledged_by": row["acknowledged_by"] or "",
        "acknowledged_at": row["acknowledged_at"],
        "resolved_by": row["resolved_by"] or "",
        "resolved_at": row["resolved_at"],
    }


def list_incidents(limit=50, *, status=None, severity=None):
    status_filter = normalize_incident_status(status, default="") if status else ""
    severity_filter = normalize_incident_severity(severity, default="") if severity else ""
    clauses = []
    params = []
    if status_filter:
        clauses.append("status = ?")
        params.append(status_filter)
    if severity_filter:
        clauses.append("severity = ?")
        params.append(severity_filter)

    where_clause = ""
    if clauses:
        where_clause = "WHERE " + " AND ".join(clauses)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id
        FROM incidents
        {where_clause}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, bounded_int(limit, 50, maximum=1000)),
    )
    ids = [row["id"] for row in cur.fetchall()]
    conn.close()
    return [get_incident(item_id) for item_id in ids if item_id is not None]


def update_incident_status(incident_id, status, *, actor_email=None):
    status_value = normalize_incident_status(status)
    now_value = db_now_iso()
    conn = get_db_conn()
    cur = conn.cursor()

    updates = ["status = ?", "updated_at = ?"]
    params = [status_value, now_value]

    normalized_actor = normalize_email(actor_email) if actor_email else None
    if status_value == "acknowledged":
        updates.extend(["acknowledged_by = ?", "acknowledged_at = ?"])
        params.extend([normalized_actor, now_value])
    if status_value == "resolved":
        updates.extend(["resolved_by = ?", "resolved_at = ?"])
        params.extend([normalized_actor, now_value])

    params.append(int(incident_id))
    cur.execute(
        f"UPDATE incidents SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    conn.commit()
    conn.close()

    log_activity(
        "incident_status_updated",
        actor_email=normalized_actor,
        actor_role=current_user_role() if normalized_actor else None,
        target_type="incident",
        target_name=str(incident_id),
        source="incident",
        details={"status": status_value},
    )
    return get_incident(incident_id)


def create_notification(message, *, level="info", user_email=None, type="system", details=None):
    normalized_message = " ".join(str(message or "").strip().split())[:240]
    if not normalized_message:
        return None

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO notifications (user_email, level, type, message, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            normalize_email(user_email) if user_email else None,
            str(level or "info").strip().lower() or "info",
            str(type or "system").strip().lower() or "system",
            normalized_message,
            json.dumps(details or {}, ensure_ascii=True),
            db_now_iso(),
        ),
    )
    notification_id = cur.lastrowid
    conn.commit()
    conn.close()
    return notification_id


def list_notifications(user_email=None, limit=50, unread_only=False):
    normalized_email = normalize_email(user_email) if user_email else None
    clauses = []
    params = []
    if normalized_email:
        clauses.append("(user_email IS NULL OR user_email = ?)")
        params.append(normalized_email)
    if unread_only:
        clauses.append("read_at IS NULL")
    where_clause = "WHERE " + " AND ".join(clauses) if clauses else ""

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id, user_email, level, type, message, details, created_at, read_at
        FROM notifications
        {where_clause}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, bounded_int(limit, 50, maximum=1000)),
    )
    rows = cur.fetchall()
    conn.close()

    items = []
    for row in rows:
        try:
            details = json.loads(row["details"] or "{}")
        except Exception:
            details = {}
        items.append(
            {
                "id": row["id"],
                "user_email": row["user_email"] or "",
                "level": str(row["level"] or "info").lower(),
                "type": str(row["type"] or "system").lower(),
                "message": row["message"],
                "details": details if isinstance(details, dict) else {},
                "created_at": row["created_at"],
                "read_at": row["read_at"],
            }
        )
    return items


def mark_notification_read(notification_id, *, actor_email=None):
    now_value = db_now_iso()
    conn = get_db_conn()
    cur = conn.cursor()
    normalized_actor = normalize_email(actor_email) if actor_email else None
    if normalized_actor:
        cur.execute(
            """
            UPDATE notifications
            SET read_at = ?
            WHERE id = ?
              AND read_at IS NULL
              AND (user_email IS NULL OR user_email = ?)
            """,
            (now_value, int(notification_id), normalized_actor),
        )
    else:
        cur.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL AND user_email IS NULL",
            (now_value, int(notification_id)),
        )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    if updated:
        log_activity(
            "notification_read",
            actor_email=actor_email,
            actor_role=current_user_role() if actor_email else None,
            target_type="notification",
            target_name=str(notification_id),
            source="notification",
        )
    return updated


def audit_login_attempt(email, success):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO audit_logins (email, success, ip_address, user_agent, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            normalize_email(email) if email else None,
            1 if success else 0,
            str(request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64],
            str(request.headers.get("User-Agent") or "")[:240],
            db_now_iso(),
        ),
    )
    conn.commit()
    conn.close()


def update_user_login_metadata(email):
    normalized_email = normalize_email(email)
    ip_address = str(request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64]
    user_agent = str(request.headers.get("User-Agent") or "")[:240]
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_login_ip = ?, last_login_user_agent = ? WHERE email = ?",
        (ip_address, user_agent, normalized_email),
    )
    conn.commit()
    conn.close()


def get_user_security_settings_db(email):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return None
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT two_factor_enabled, two_factor_method, session_revoked_at, last_login_ip, last_login_user_agent
        FROM users
        WHERE email = ?
        """,
        (normalized_email,),
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "two_factor_enabled": bool(row["two_factor_enabled"]),
        "two_factor_method": str(row["two_factor_method"] or ""),
        "session_revoked_at": row["session_revoked_at"],
        "last_login_ip": str(row["last_login_ip"] or ""),
        "last_login_user_agent": str(row["last_login_user_agent"] or ""),
    }


def set_two_factor_state_db(email, *, enabled, method="email"):
    normalized_email = normalize_email(email)
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET two_factor_enabled = ?, two_factor_method = ?
        WHERE email = ?
        """,
        (1 if enabled else 0, str(method or "email"), normalized_email),
    )
    conn.commit()
    conn.close()


def revoke_sessions_db(email):
    normalized_email = normalize_email(email)
    now_value = db_now_iso()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET session_revoked_at = ? WHERE email = ?",
        (now_value, normalized_email),
    )
    conn.commit()
    conn.close()
    return now_value


def list_login_audit(limit=50, *, email=None):
    normalized_email = normalize_email(email) if email else None
    conn = get_db_conn()
    cur = conn.cursor()
    if normalized_email:
        cur.execute(
            """
            SELECT id, email, success, ip_address, user_agent, created_at
            FROM audit_logins
            WHERE email = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (normalized_email, bounded_int(limit, 50, maximum=1000)),
        )
    else:
        cur.execute(
            """
            SELECT id, email, success, ip_address, user_agent, created_at
            FROM audit_logins
            ORDER BY id DESC
            LIMIT ?
            """,
            (bounded_int(limit, 50, maximum=1000),),
        )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": row["id"],
            "email": row["email"] or "",
            "success": bool(row["success"]),
            "ip_address": row["ip_address"] or "",
            "user_agent": row["user_agent"] or "",
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def incident_cooldown_allows(signature):
    now_ts = time.time()
    last_seen = float(last_incident_created_at.get(signature) or 0.0)
    if now_ts - last_seen < INCIDENT_AUTO_COOLDOWN_SECONDS:
        return False
    last_incident_created_at[signature] = now_ts
    return True


def compute_risk_score(risk_level, reasons=None):
    normalized = normalize_choice(risk_level, VALID_SECURITY_RISK, "normal")
    base = {"normal": 12, "suspicious": 45, "high": 72, "critical": 92}.get(normalized, 12)
    bonus = min(8, len(reasons or [])) if reasons else 0
    return int(max(0, min(100, base + bonus)))


def build_ai_recommendations(risk_level, reasons=None):
    normalized = normalize_choice(risk_level, VALID_SECURITY_RISK, "normal")
    recommendations = []
    if normalized in {"suspicious", "high", "critical"}:
        recommendations.append("Verify camera feed, focus on entry points, and confirm operator presence.")
    if any("Unknown" in str(item) for item in (reasons or [])):
        recommendations.append("Check face roster and validate visitor authorization; consider enabling stricter access policy.")
    if normalized == "critical":
        recommendations.append("Escalate to emergency mode, notify admins, and preserve incident evidence (snapshot + timeline).")
    if not recommendations:
        recommendations.append("System nominal. Maintain routine monitoring and validate alert policies weekly.")
    return recommendations[:4]


def maybe_auto_create_incident(title, *, severity, source, camera_id=None, details=None, frame=None):
    if not INCIDENT_AUTO_CREATE:
        return None

    severity_value = normalize_incident_severity(severity)
    signature = (source, severity_value, title, camera_id or "")
    if not incident_cooldown_allows(signature):
        return None

    snapshot_bytes = None
    if frame is not None and INCIDENT_AUTO_SNAPSHOT and CAMERA_AVAILABLE and cv2 is not None:
        try:
            ok, buffer = cv2.imencode(".jpg", frame)
            if ok:
                snapshot_bytes = buffer.tobytes()
        except Exception:
            snapshot_bytes = None

    incident = create_incident(
        title,
        severity=severity_value,
        status="open",
        source=source,
        camera_id=camera_id,
        snapshot_bytes=snapshot_bytes,
        risk_score=(details or {}).get("risk_score"),
        tags=(details or {}).get("tags") if isinstance((details or {}).get("tags"), list) else [],
        details=details or {},
        actor_email=None,
    )

    if incident:
        create_notification(
            f"Incident created: {incident['title']}",
            level=incident["severity"],
            user_email=None,
            type="incident",
            details={"incident_id": incident["id"], "severity": incident["severity"]},
        )
    return incident


def list_devices_db():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT name, state, created_at, updated_at
        FROM devices
        ORDER BY LOWER(name), name
        """
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "name": row["name"],
            "state": row["state"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def get_device_record_db(name: str):
    normalized_name = normalize_device_name(name)
    if not normalized_name:
        return None

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT name, state, created_at, updated_at
        FROM devices
        WHERE LOWER(name) = LOWER(?)
        LIMIT 1
        """,
        (normalized_name,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def resolve_device_name(name: str):
    row = get_device_record_db(name)
    return row["name"] if row else None


def get_device_state_db(name: str):
    row = get_device_record_db(name)
    return row["state"] if row else None


def set_device_state_db(name: str, state: str):
    normalized_name = normalize_device_name(name)
    if not normalized_name:
        return

    now_value = db_now_iso()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO devices (name, state, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            state = excluded.state,
            updated_at = excluded.updated_at
        """,
        (normalized_name, state, now_value, now_value),
    )
    conn.commit()
    conn.close()


def delete_device_db(name: str):
    resolved_name = resolve_device_name(name)
    if not resolved_name:
        return None

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM devices WHERE name = ?", (resolved_name,))
    conn.commit()
    conn.close()
    refresh_device_state_cache()
    return resolved_name


def ensure_default_devices_seeded():
    existing_names = {item["name"].lower() for item in list_devices_db()}
    for name, state in DEFAULT_DEVICE_STATE.items():
        if name.lower() not in existing_names:
            set_device_state_db(name, state)


def refresh_device_state_cache():
    global device_state
    device_state = {item["name"]: item["state"] for item in list_devices_db()}
    return device_state


def ensure_device_state(device_name, desired_state, source="ui", actor_email=None, actor_role=None):
    resolved_name = resolve_device_name(device_name)
    if not resolved_name:
        return None, "Device not found"

    current_state = get_device_state_db(resolved_name) or "OFF"
    new_state = "ON" if str(desired_state or "").strip().upper() == "ON" else "OFF"
    changed = new_state != current_state

    if not changed:
        return {"name": resolved_name, "state": current_state, "changed": False}, None

    set_device_state_db(resolved_name, new_state)
    refresh_device_state_cache()

    message = f"{resolved_name.capitalize()} turned {new_state}"
    log_system_alert(
        message,
        level="info",
        details={"device": resolved_name, "state": new_state, "action": "set"},
    )
    log_activity(
        "device_updated",
        actor_email=actor_email,
        actor_role=actor_role,
        target_type="device",
        target_name=resolved_name,
        source=source,
        details={"state": new_state, "action": "set"},
    )
    return {"name": resolved_name, "state": new_state, "changed": True}, None


def perform_device_action(device_name, action, source="ui", actor_email=None, actor_role=None):
    resolved_name = resolve_device_name(device_name)
    if not resolved_name:
        return None, "Device not found"

    current_state = get_device_state_db(resolved_name) or "OFF"
    normalized_action = str(action or "").strip().lower()
    if normalized_action == "toggle":
        desired_state = "ON" if current_state == "OFF" else "OFF"
    elif normalized_action == "on":
        desired_state = "ON"
    elif normalized_action == "off":
        desired_state = "OFF"
    else:
        return None, "Unsupported device action"

    device, error_message = ensure_device_state(
        resolved_name,
        desired_state,
        source=source,
        actor_email=actor_email,
        actor_role=actor_role,
    )
    if device is None or error_message:
        return None, error_message
    if not device.get("changed"):
        device["state"] = desired_state
    return {"name": device["name"], "state": device["state"]}, None


# Initialize DB on startup
init_db()
ensure_default_devices_seeded()
refresh_device_state_cache()
save_automation_state(get_automation_state())
latest_detections = []
latest_faces = []
latest_detection_objects = []
vision_telemetry = deque(maxlen=max(60, get_env_int("VISION_TELEMETRY_MAX_FRAMES", 180)))
latest_camera_alert_payload = {
    "id": "camera-initial",
    "alert": latest_alert,
    "message": latest_alert,
    "source": "camera",
    "level": "info",
    "time": None,
    "details": {"detections": [], "faces": []},
}
camera_state = {
    "available": False,
    "message": "Waiting for camera",
    "last_frame_at": None,
    "updated_at": None,
    "frames_processed": 0,
    "last_detection_at": None,
    "last_error": None,
    "stream_source": "server",
    "active_objects": [],
    "object_count": 0,
    "yolo_status": YOLO_STATUS_MESSAGE,
    "active_camera_id": None,
    "active_camera_name": None,
    "active_camera_type": None,
    "active_camera_source": None,
    "camera_count": 0,
}
face_state = {
    "enabled": False,
    "message": "Face recognition unavailable",
    "known_people": 0,
    "updated_at": None,
}

# Human behavior tracking
human_behavior = {
    "person_present": False,
    "person_last_seen": None,
    "person_duration": 0,
    "person_count": 0,
    "last_activity": "idle",
    "mood_estimate": "neutral",
    "interaction_count": 0,
}
alert_history = deque(maxlen=25)
last_logged_alert_at = {}
last_incident_created_at = {}
FACE_RECOGNIZER = None
FACE_DETECTOR = None
FACE_RECOGNITION_ENABLED = False
FACE_LABELS = {}
camera_registry_lock = threading.RLock()
camera_registry = []
active_camera_id = None
mobile_camera_lock = threading.Lock()
mobile_camera_state = {
    "device_id": "default",
    "frame_bytes": None,
    "updated_at": None,
    "updated_ts": 0.0,
    "frame_count": 0,
    "last_error": None,
}


def update_camera_profile_state(profile):
    active_profile = profile or build_default_camera_profile()
    camera_state["active_camera_id"] = active_profile["id"]
    camera_state["active_camera_name"] = active_profile["name"]
    camera_state["active_camera_type"] = active_profile["type"]
    camera_state["active_camera_source"] = active_profile["source_display"]
    camera_state["camera_count"] = len(camera_registry) or 1


def refresh_camera_registry():
    global active_camera_id, camera_registry

    with camera_registry_lock:
        camera_registry = load_camera_profiles_from_disk()
        if not camera_registry:
            camera_registry = [build_default_camera_profile()]

        valid_ids = {profile["id"] for profile in camera_registry}
        if active_camera_id not in valid_ids:
            active_camera_id = camera_registry[0]["id"]

        update_camera_profile_state(get_active_camera_profile(refresh=False))
        return list(camera_registry)


def get_active_camera_profile(refresh=True):
    global active_camera_id

    if refresh or not camera_registry:
        refresh_camera_registry()

    for profile in camera_registry:
        if profile["id"] == active_camera_id:
            return profile

    fallback = camera_registry[0] if camera_registry else build_default_camera_profile()
    active_camera_id = fallback["id"]
    update_camera_profile_state(fallback)
    return fallback


def list_camera_profiles():
    refresh_camera_registry()
    return list(camera_registry)


def set_active_camera_profile(camera_id):
    global active_camera_id

    refresh_camera_registry()
    lookup = str(camera_id or "").strip().lower()
    if not lookup:
        return None

    for profile in camera_registry:
        if (
            profile["id"].lower() == lookup
            or profile["name"].lower() == lookup
            or profile["source_display"].lower() == lookup
        ):
            active_camera_id = profile["id"]
            update_camera_profile_state(profile)
            update_camera_status(
                camera_state["available"],
                f"Selected {profile['label']}",
                last_frame_at=camera_state.get("last_frame_at"),
            )
            return profile
    return None


def cycle_active_camera(step=1):
    refresh_camera_registry()
    if not camera_registry:
        return build_default_camera_profile()

    if len(camera_registry) == 1:
        active_profile = get_active_camera_profile(refresh=False)
        update_camera_profile_state(active_profile)
        return active_profile

    active_profile = get_active_camera_profile(refresh=False)
    current_index = next(
        (index for index, profile in enumerate(camera_registry) if profile["id"] == active_profile["id"]),
        0,
    )
    next_index = (current_index + int(step)) % len(camera_registry)
    return set_active_camera_profile(camera_registry[next_index]["id"])


def register_mobile_camera_profile(device_id, name=None, set_active=False):
    global active_camera_id

    normalized_id = normalize_mobile_camera_id(device_id)
    profile_name = normalize_camera_name(name) or f"Mobile {normalized_id}"
    source_value = mobile_camera_source(normalized_id)
    profile_id = f"mobile-{normalized_id}"

    with camera_registry_lock:
        existing = load_camera_profiles_from_disk()
        filtered = []

        for profile in existing:
            if not isinstance(profile, dict):
                continue
            source_matches = str(profile.get("source", "")).strip().lower() == source_value
            id_matches = str(profile.get("id", "")).strip().lower() == profile_id
            if source_matches or id_matches:
                continue
            filtered.append(profile)

        profile = build_camera_profile(
            profile_id=profile_id,
            name=profile_name,
            source=source_value,
            camera_type="mobile",
            enabled=True,
        )
        filtered.append(profile)
        save_camera_profiles_to_disk(filtered)
        refresh_camera_registry()

        if set_active:
            active_camera_id = profile["id"]
            update_camera_profile_state(profile)
            update_camera_status(
                camera_state["available"],
                f"Selected {profile['label']}",
                last_frame_at=camera_state.get("last_frame_at"),
            )

        return profile


def mobile_camera_frame_is_fresh(state):
    if not state.get("frame_bytes") or not state.get("updated_ts"):
        return False
    return (time.time() - float(state["updated_ts"])) <= MOBILE_CAMERA_FRAME_TTL_SECONDS


def get_latest_mobile_camera_frame(device_id):
    normalized_id = normalize_mobile_camera_id(device_id)
    with mobile_camera_lock:
        same_device = normalize_mobile_camera_id(mobile_camera_state.get("device_id")) == normalized_id
        if not same_device or not mobile_camera_frame_is_fresh(mobile_camera_state):
            return None, None, mobile_camera_state.get("last_error")
        return (
            mobile_camera_state.get("frame_bytes"),
            mobile_camera_state.get("updated_at"),
            mobile_camera_state.get("last_error"),
        )


def update_mobile_camera_frame(device_id, frame_bytes):
    normalized_id = normalize_mobile_camera_id(device_id)
    now_value = now_iso()
    with mobile_camera_lock:
        mobile_camera_state["device_id"] = normalized_id
        mobile_camera_state["frame_bytes"] = frame_bytes
        mobile_camera_state["updated_at"] = now_value
        mobile_camera_state["updated_ts"] = time.time()
        mobile_camera_state["frame_count"] = int(mobile_camera_state.get("frame_count", 0)) + 1
        mobile_camera_state["last_error"] = None
        return {
            "device_id": normalized_id,
            "updated_at": now_value,
            "frame_count": mobile_camera_state["frame_count"],
        }


def mark_mobile_camera_error(message):
    with mobile_camera_lock:
        mobile_camera_state["last_error"] = str(message or "mobile_camera_error")


def build_mobile_camera_urls(device_id):
    normalized_id = normalize_mobile_camera_id(device_id)
    host_root = str(request.host_url or "").rstrip("/")
    token = MOBILE_CAMERA_TOKEN
    page_params = {"device": normalized_id, "token": token}
    upload_params = {"device_id": normalized_id, "token": token}
    return {
        "device_id": normalized_id,
        "mobile_page_url": f"{host_root}/mobile-camera?{urlencode(page_params)}",
        "mobile_upload_url": f"{host_root}/api/mobile-camera/frame?{urlencode(upload_params)}",
    }


refresh_camera_registry()


def now_string():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_iso():
    return db_now_iso()


def build_alert_payload(message, source, level="info", details=None):
    return {
        "id": f"{source}-{int(time.time() * 1000)}",
        "alert": message,
        "message": message,
        "source": source,
        "level": level,
        "time": now_iso(),
        "details": details or {},
    }


def log_alert(payload):
    signature = (payload["source"], payload["level"], payload["message"])
    last_seen = last_logged_alert_at.get(signature, 0.0)
    now_ts = time.time()

    if now_ts - last_seen < ALERT_COOLDOWN_SECONDS:
        return

    last_logged_alert_at[signature] = now_ts
    alert_history.appendleft(payload)


def update_camera_status(available, message, last_frame_at=None):
    camera_state["available"] = bool(available)
    camera_state["message"] = message
    camera_state["updated_at"] = now_iso()
    if last_frame_at is not None:
        camera_state["last_frame_at"] = last_frame_at
    if available:
        camera_state["last_error"] = None


def update_camera_diagnostics(
    *,
    active_objects=None,
    frames_processed=None,
    last_detection_at=None,
    last_error=None,
    stream_source=None,
):
    if active_objects is not None:
        camera_state["active_objects"] = list(active_objects)
        camera_state["object_count"] = sum(item.get("count", 0) for item in camera_state["active_objects"])
    if frames_processed is not None:
        camera_state["frames_processed"] = int(frames_processed)
    if last_detection_at is not None:
        camera_state["last_detection_at"] = last_detection_at
    if last_error is not None:
        camera_state["last_error"] = last_error
    if stream_source is not None:
        camera_state["stream_source"] = stream_source
    camera_state["yolo_status"] = YOLO_STATUS_MESSAGE


def set_camera_alert(message, level="info", details=None, log=False):
    global latest_alert, latest_camera_alert_payload, latest_detections, latest_faces, latest_detection_objects

    payload = build_alert_payload(message, "camera", level=level, details=details)
    latest_alert = message
    latest_detections = list((details or {}).get("detections", []))
    latest_faces = list((details or {}).get("faces", []))
    latest_detection_objects = list((details or {}).get("objects", []))
    latest_camera_alert_payload = payload

    if log:
        log_alert(payload)

    try:
        if str(level or "").strip().lower() == "error":
            camera = get_active_camera_profile(refresh=False)
            maybe_auto_create_incident(
                message or "Critical camera event",
                severity="critical",
                source="camera",
                camera_id=camera.get("id") if isinstance(camera, dict) else None,
                details=details or {},
                frame=None,
            )
    except Exception:
        pass

    return payload


def log_system_alert(message, level="warning", details=None):
    payload = build_alert_payload(message, "system", level=level, details=details)
    log_alert(payload)
    return payload


def risk_rank(value):
    order = {"normal": 0, "suspicious": 1, "high": 2, "critical": 3}
    return order.get(str(value or "").strip().lower(), 0)


def max_risk(*values):
    normalized = [normalize_choice(value, VALID_SECURITY_RISK, "normal") for value in values]
    return max(normalized, key=risk_rank, default="normal")


def clamp_number(value, default, minimum, maximum):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = float(default)
    return max(minimum, min(maximum, numeric))


def build_camera_risk_snapshot():
    reasons = []
    risk = "normal"
    intelligence = get_camera_intelligence_state()

    if intelligence.get("privacy_mode"):
        return "normal", ["Camera privacy mode is active"]

    unknown_count = sum(int(item.get("count", 0) or 0) for item in latest_faces if not item.get("recognized"))
    if unknown_count:
        risk = max_risk(risk, "high")
        reasons.append(f"Unknown visitor activity detected ({unknown_count})")

    danger_labels = []
    for item in latest_detections or []:
        label = str(item.get("label", "") or "").strip().lower()
        if label in DEFENSE_TRIGGER_OBJECTS:
            danger_labels.append(label)
    if danger_labels:
        risk = max_risk(risk, "critical")
        reasons.append(f"Danger objects detected: {', '.join(sorted(set(danger_labels)))}")

    current_level = str(latest_camera_alert_payload.get("level", "info") or "info").lower()
    if current_level == "error":
        risk = max_risk(risk, "critical")
        reasons.append("Camera pipeline reported an error state")
    elif current_level == "warning":
        risk = max_risk(risk, "suspicious")
        if not reasons:
            reasons.append(str(latest_camera_alert_payload.get("message") or "Camera warning detected"))

    return risk, reasons


def build_alert_feed(limit=25):
    candidates = []
    latest_payload = dict(latest_camera_alert_payload)
    if latest_payload.get("message"):
        candidates.append(latest_payload)
    candidates.extend(list(alert_history))

    items = []
    seen = set()
    for payload in candidates:
        if not isinstance(payload, dict):
            continue
        signature = (
            str(payload.get("source") or ""),
            str(payload.get("level") or ""),
            str(payload.get("message") or payload.get("alert") or ""),
        )
        if signature in seen:
            continue
        seen.add(signature)
        items.append(payload)
        if len(items) >= bounded_int(limit, 25, maximum=1000):
            break
    return items


def build_automation_status_text(mode, runtime_risk, reasons):
    if mode == "manual":
        return "Manual operating is active. Devices respond only to operator actions."
    if runtime_risk in {"high", "critical"}:
        return "Self monitoring escalated security and armed defensive automation."
    if reasons:
        return f"Self monitoring is active. {reasons[0]}."
    return "Self monitoring is active and the environment is stable."


def build_automation_snapshot(state=None):
    active_state = copy.deepcopy(state or get_automation_state())
    devices = list_devices_db()
    return {
        "mode": active_state["mode"],
        "mode_label": "Self monitoring" if active_state["mode"] == "self_monitoring" else "Manual operating",
        "environment": active_state["environment"],
        "thresholds": active_state["thresholds"],
        "defense": active_state["defense"],
        "runtime_risk": active_state["runtime_risk"],
        "status": active_state["status"],
        "last_actions": list(active_state.get("last_actions") or []),
        "last_reasons": list(active_state.get("last_reasons") or []),
        "last_evaluated_at": active_state.get("last_evaluated_at"),
        "policy": copy.deepcopy(DEFAULT_POLICY_SNAPSHOT),
        "devices": devices,
        "active_devices": [item["name"] for item in devices if item["state"] == "ON"],
    }


def evaluate_automation(actor_email=None, actor_role="system", source="automation"):
    state = get_automation_state()
    reasons = []
    actions = []

    environment = state["environment"]
    thresholds = state["thresholds"]
    runtime_risk = normalize_choice(environment.get("security_risk"), VALID_SECURITY_RISK, "normal")
    desired_states = {}

    camera_risk, camera_reasons = build_camera_risk_snapshot()
    runtime_risk = max_risk(runtime_risk, camera_risk)
    reasons.extend(camera_reasons)

    if state["mode"] == "self_monitoring":
        if environment["temperature_c"] >= thresholds["temperature_high_c"]:
            desired_states["fan"] = "ON"
            desired_states["ac"] = "ON"
            reasons.append(f"Temperature {environment['temperature_c']:.1f}C exceeded the cooling threshold")
        elif environment["temperature_c"] <= thresholds["temperature_low_c"]:
            desired_states["fan"] = "OFF"
            desired_states["ac"] = "OFF"
            reasons.append(f"Temperature dropped to {environment['temperature_c']:.1f}C")

        if environment["ambient_light"] <= thresholds["ambient_light_low"]:
            desired_states["light"] = "ON"
            reasons.append(f"Ambient light fell to {environment['ambient_light']:.0f}%")
        elif environment["ambient_light"] >= thresholds["ambient_light_high"]:
            desired_states["light"] = "OFF"
            reasons.append(f"Ambient light recovered to {environment['ambient_light']:.0f}%")

        if state["defense"]["armed"] and runtime_risk in {"high", "critical"}:
            if state["defense"]["auto_alarm"]:
                desired_states["alarm"] = "ON"
            if state["defense"]["auto_defense"]:
                desired_states["defense mode"] = "ON"
        elif state["defense"]["armed"] and runtime_risk == "normal":
            if state["defense"]["auto_alarm"]:
                desired_states["alarm"] = "OFF"
            if state["defense"]["auto_defense"]:
                desired_states["defense mode"] = "OFF"

        for device_name, desired_state in desired_states.items():
            updated_device, error_message = ensure_device_state(
                device_name,
                desired_state,
                source=f"{source}-automation",
                actor_email=actor_email,
                actor_role=actor_role,
            )
            if error_message or updated_device is None or not updated_device.get("changed"):
                continue
            actions.append(
                {
                    "type": "device",
                    "name": updated_device["name"],
                    "state": updated_device["state"],
                    "reason": reasons[-1] if reasons else "Automatic policy",
                }
            )

        if actions and runtime_risk in {"high", "critical"}:
            log_system_alert(
                "Defense automation activated due to a security event",
                level="warning" if runtime_risk == "high" else "error",
                details={
                    "runtime_risk": runtime_risk,
                    "reasons": reasons[:6],
                    "actions": actions,
                },
            )

    state["runtime_risk"] = runtime_risk
    state["last_actions"] = actions[:6]
    state["last_reasons"] = reasons[:6]
    state["last_evaluated_at"] = now_iso()
    state["status"] = build_automation_status_text(state["mode"], runtime_risk, reasons)
    save_automation_state(state)
    return build_automation_snapshot(state)


def format_detection_summary(counts):
    if not counts:
        return ""

    parts = []
    for label, count in counts.most_common(4):
        parts.append(f"{label} ({count})" if count > 1 else label)
    return ", ".join(parts)


def get_yolo_label(class_id):
    names = getattr(YOLO_MODEL, "names", {})
    if isinstance(names, dict):
        return str(names.get(class_id, class_id))
    if isinstance(names, (list, tuple)) and 0 <= class_id < len(names):
        return str(names[class_id])
    return str(class_id)


def update_face_status(enabled, message, known_people=0):
    face_state["enabled"] = bool(enabled)
    face_state["message"] = message
    face_state["known_people"] = int(known_people)
    face_state["updated_at"] = now_iso()


def get_face_cascade_path():
    if cv2 is None:
        return None
    haar_root = getattr(getattr(cv2, "data", None), "haarcascades", "")
    if not haar_root:
        return None
    return os.path.join(haar_root, "haarcascade_frontalface_default.xml")


def build_face_detector():
    if not CAMERA_AVAILABLE or cv2 is None:
        return None, "OpenCV face detection is unavailable"

    cascade_path = get_face_cascade_path()
    if not cascade_path or not os.path.exists(cascade_path):
        return None, "Face detection cascade not found"

    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        return None, "Face detection cascade failed to load"

    return detector, None


def detect_faces_in_gray(gray_image, detector):
    if detector is None:
        return ()

    faces = detector.detectMultiScale(
        gray_image,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(60, 60),
    )
    return faces if faces is not None else ()


def normalize_face_sample(gray_image, face_box):
    x, y, w, h = [int(value) for value in face_box]
    face_sample = gray_image[y : y + h, x : x + w]
    if face_sample.size == 0:
        return None
    face_sample = cv2.resize(face_sample, FACE_IMAGE_SIZE)
    return cv2.equalizeHist(face_sample)


def load_face_sample_from_image(image, detector):
    if cv2 is None:
        return None, "opencv_unavailable"
    if image is None:
        return None, "unreadable"

    gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray_image = cv2.equalizeHist(gray_image)
    faces = detect_faces_in_gray(gray_image, detector)

    if len(faces) == 0:
        return None, "no_face"
    if len(faces) > 1:
        return None, "multiple_faces"

    sample = normalize_face_sample(gray_image, faces[0])
    if sample is None:
        return None, "invalid_face"

    return sample, None


def load_face_sample_from_file(path, detector):
    if cv2 is None:
        return None, "opencv_unavailable"

    image = cv2.imread(path)
    return load_face_sample_from_image(image, detector)


def iter_known_face_images(root_dir):
    if not os.path.isdir(root_dir):
        return

    for person_name in sorted(os.listdir(root_dir)):
        person_dir = os.path.join(root_dir, person_name)
        if not os.path.isdir(person_dir):
            continue

        for file_name in sorted(os.listdir(person_dir)):
            extension = os.path.splitext(file_name)[1].lower()
            if extension not in FACE_IMAGE_EXTENSIONS:
                continue
            yield person_name, os.path.join(person_dir, file_name)


def list_known_face_people():
    people = []
    if not os.path.isdir(KNOWN_FACES_DIR):
        return people

    for person_name in sorted(os.listdir(KNOWN_FACES_DIR)):
        person_dir = os.path.join(KNOWN_FACES_DIR, person_name)
        if not os.path.isdir(person_dir):
            continue

        image_files = []
        for file_name in sorted(os.listdir(person_dir)):
            extension = os.path.splitext(file_name)[1].lower()
            if extension in FACE_IMAGE_EXTENSIONS:
                image_files.append(file_name)

        if not image_files:
            continue

        people.append(
            {
                "name": person_name,
                "sample_count": len(image_files),
                "last_updated_at": datetime.datetime.fromtimestamp(
                    os.path.getmtime(person_dir)
                ).isoformat(timespec="seconds"),
            }
        )

    return people


def resolve_known_face_person_dir(name):
    target_name = normalize_person_name(name)
    if not target_name or not os.path.isdir(KNOWN_FACES_DIR):
        return None, None

    for person_name in os.listdir(KNOWN_FACES_DIR):
        person_dir = os.path.join(KNOWN_FACES_DIR, person_name)
        if not os.path.isdir(person_dir):
            continue
        if normalize_person_name(person_name).lower() == target_name.lower():
            return person_name, person_dir
    return None, None


def decode_uploaded_face_image(file_storage):
    if cv2 is None or np is None:
        return None, "opencv_unavailable", b""

    raw_bytes = file_storage.read()
    try:
        file_storage.stream.seek(0)
    except Exception:
        pass

    if not raw_bytes:
        return None, "empty", b""
    if len(raw_bytes) > FACE_UPLOAD_MAX_BYTES:
        return None, "too_large", raw_bytes

    array = np.frombuffer(raw_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        return None, "unreadable", raw_bytes
    return image, None, raw_bytes


def build_face_upload_filename(person_name, original_name, index):
    extension = os.path.splitext(str(original_name or ""))[1].lower()
    if extension not in FACE_IMAGE_EXTENSIONS:
        extension = ".jpg"

    base_name = secure_filename(normalize_person_name(person_name).lower()) or "person"
    return f"{base_name}-{int(time.time() * 1000)}-{index}{extension}"


def save_known_face_uploads(person_name, files):
    normalized_name = normalize_person_name(person_name)
    if not normalized_name:
        return [], Counter({"missing_name": 1}), "Full name is required."

    upload_files = [file_item for file_item in files if getattr(file_item, "filename", "")]
    if not upload_files:
        return [], Counter({"missing_images": 1}), "Please upload at least one image."

    detector, error = build_face_detector()
    if error:
        return [], Counter({"detector_unavailable": 1}), error

    os.makedirs(KNOWN_FACES_DIR, exist_ok=True)
    target_dir = os.path.join(KNOWN_FACES_DIR, normalized_name)
    os.makedirs(target_dir, exist_ok=True)

    saved_files = []
    skipped = Counter()

    for index, file_storage in enumerate(upload_files[:FACE_UPLOAD_MAX_FILES], start=1):
        extension = os.path.splitext(str(file_storage.filename or ""))[1].lower()
        if extension and extension not in FACE_IMAGE_EXTENSIONS:
            skipped["unsupported_extension"] += 1
            continue

        image, image_error, raw_bytes = decode_uploaded_face_image(file_storage)
        if image_error:
            skipped[image_error] += 1
            continue

        _, reason = load_face_sample_from_image(image, detector)
        if reason:
            skipped[reason] += 1
            continue

        file_name = build_face_upload_filename(normalized_name, file_storage.filename, index)
        with open(os.path.join(target_dir, file_name), "wb") as output_handle:
            output_handle.write(raw_bytes)
        saved_files.append(file_name)

    if not saved_files and not os.listdir(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)

    return saved_files, skipped, None


def load_known_face_training_data(root_dir, detector):
    samples = []
    labels = []
    label_map = {}
    name_to_label = {}
    skipped = Counter()

    for person_name, image_path in iter_known_face_images(root_dir) or ():
        sample, reason = load_face_sample_from_file(image_path, detector)
        if sample is None:
            skipped[reason] += 1
            continue

        if person_name not in name_to_label:
            label_id = len(name_to_label)
            name_to_label[person_name] = label_id
            label_map[label_id] = person_name

        samples.append(sample)
        labels.append(name_to_label[person_name])

    return samples, labels, label_map, {
        "loaded": len(samples),
        "people": len(label_map),
        "skipped": skipped,
    }


def init_face_recognition():
    global FACE_DETECTOR, FACE_LABELS, FACE_RECOGNITION_ENABLED, FACE_RECOGNIZER

    FACE_DETECTOR = None
    FACE_RECOGNIZER = None
    FACE_LABELS = {}
    FACE_RECOGNITION_ENABLED = False

    if DISABLE_FACE_RECOGNITION:
        update_face_status(False, "Face recognition disabled in configuration")
        return

    if not CAMERA_AVAILABLE or cv2 is None or np is None:
        update_face_status(False, "OpenCV face recognition is unavailable")
        return

    if not os.path.isdir(KNOWN_FACES_DIR):
        update_face_status(False, "No known faces directory found")
        return

    face_module = getattr(cv2, "face", None)
    if face_module is None or not hasattr(face_module, "LBPHFaceRecognizer_create"):
        update_face_status(False, "Install opencv-contrib-python-headless for face recognition")
        return

    detector, error = build_face_detector()
    if error:
        update_face_status(False, error)
        return

    samples, labels, label_map, _stats = load_known_face_training_data(
        KNOWN_FACES_DIR,
        detector,
    )
    if not samples:
        update_face_status(False, "No valid face training images found")
        return

    try:
        recognizer = face_module.LBPHFaceRecognizer_create()
        recognizer.train(samples, np.array(labels, dtype=np.int32))
    except Exception as exc:
        update_face_status(False, f"Face training failed: {exc}")
        return

    FACE_DETECTOR = detector
    FACE_RECOGNIZER = recognizer
    FACE_LABELS = label_map
    FACE_RECOGNITION_ENABLED = True
    update_face_status(
        True,
        f"Ready with {len(label_map)} known people",
        known_people=len(label_map),
    )


def format_face_summary(faces):
    if not faces:
        return ""

    parts = []
    for item in faces[:4]:
        label = item["name"] if item.get("recognized") else "Unknown face"
        count = item.get("count", 0)
        parts.append(f"{label} ({count})" if count > 1 else label)
    return ", ".join(parts)


def predict_face_match(face_sample, recognizer=None, label_map=None, threshold=None):
    active_recognizer = recognizer or FACE_RECOGNIZER
    active_labels = label_map or FACE_LABELS
    active_threshold = FACE_RECOGNITION_THRESHOLD if threshold is None else float(threshold)

    label_id, confidence = active_recognizer.predict(face_sample)
    confidence = float(confidence)
    recognized = confidence <= active_threshold and label_id in active_labels
    name = active_labels.get(label_id, "Unknown") if recognized else "Unknown"
    return {
        "name": name,
        "recognized": recognized,
        "confidence": confidence,
        "label_id": int(label_id),
    }


def run_face_recognition_on_frame(frame):
    result = {
        "faces": [],
        "detected": False,
        "recognized_count": 0,
        "unknown_count": 0,
    }

    if not FACE_RECOGNITION_ENABLED or FACE_DETECTOR is None or FACE_RECOGNIZER is None:
        return result

    try:
        gray_image = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray_image = cv2.equalizeHist(gray_image)
        faces = detect_faces_in_gray(gray_image, FACE_DETECTOR)

        if len(faces) == 0:
            return result

        known_counts = Counter()
        unknown_count = 0

        for face_box in faces:
            face_sample = normalize_face_sample(gray_image, face_box)
            if face_sample is None:
                continue

            prediction = predict_face_match(face_sample)
            x, y, w, h = [int(value) for value in face_box]
            recognized = prediction["recognized"]
            label = prediction["name"]

            if recognized:
                known_counts[label] += 1
            else:
                unknown_count += 1

            color = (0, 180, 0) if recognized else (0, 0, 255)
            text = label if recognized else "Unknown"
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            cv2.putText(
                frame,
                text,
                (x, max(20, y - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                color,
                2,
                cv2.LINE_AA,
            )

        result["faces"] = [
            {"name": name, "count": count, "recognized": True}
            for name, count in known_counts.most_common()
        ]
        if unknown_count:
            result["faces"].append(
                {"name": "Unknown", "count": unknown_count, "recognized": False}
            )

        result["detected"] = bool(result["faces"])
        result["recognized_count"] = sum(known_counts.values())
        result["unknown_count"] = unknown_count
        return result
    except Exception:
        return result


def is_ajax_request():
    return request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest"


def expects_json_response():
    if is_ajax_request():
        return True

    if request.path.startswith("/api/") or request.path.startswith("/toggle/"):
        return True

    if request.path in {"/health", "/camera_feed", "/get_alert", "/alerts", "/assistant"}:
        return True

    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    if best != "application/json":
        return False
    return (
        request.accept_mimetypes["application/json"]
        >= request.accept_mimetypes["text/html"]
    )


def auth_redirect_target():
    return "/register" if not has_users() else "/login"


def auth_required_response():
    redirect_target = auth_redirect_target()
    if expects_json_response():
        return jsonify(
            {"error": "authentication required", "redirect": redirect_target}
        ), 401
    return redirect(redirect_target)


def is_local_voice_assistant_request():
    remote_addr = str(request.remote_addr or "")
    assistant_header = str(request.headers.get("X-SmartAI-Voice-Assistant", "") or "").strip()

    return (
        ALLOW_LOCAL_VOICE_BYPASS
        and
        remote_addr in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        and assistant_header == "1"
    )


def has_valid_mobile_camera_token(payload=None):
    data = payload if isinstance(payload, dict) else {}
    provided = (
        request.headers.get("X-Mobile-Camera-Token")
        or request.args.get("token")
        or data.get("token")
    )
    provided = str(provided or "").strip()
    if not provided:
        return False
    return secrets.compare_digest(provided, MOBILE_CAMERA_TOKEN)


def is_mobile_camera_upload_authorized(payload=None):
    if session.get("user"):
        return True
    if has_valid_mobile_camera_token(payload):
        return True
    return False


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        email = session.get("user")
        if email:
            issued_at = str(session.get("issued_at") or "").strip()
            if issued_at:
                try:
                    security = get_user_security_settings_db(email) or {}
                    revoked_at = str(security.get("session_revoked_at") or "").strip()
                    if revoked_at and revoked_at >= issued_at:
                        session.clear()
                        return auth_required_response()
                except Exception:
                    pass
            return view(*args, **kwargs)
        return auth_required_response()

    return wrapped_view


def login_or_local_voice_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if is_local_voice_assistant_request():
            return view(*args, **kwargs)
        email = session.get("user")
        if email:
            issued_at = str(session.get("issued_at") or "").strip()
            if issued_at:
                try:
                    security = get_user_security_settings_db(email) or {}
                    revoked_at = str(security.get("session_revoked_at") or "").strip()
                    if revoked_at and revoked_at >= issued_at:
                        session.clear()
                        return auth_required_response()
                except Exception:
                    pass
            return view(*args, **kwargs)
        return auth_required_response()

    return wrapped_view


def normalize_email(value):
    return str(value or "").strip().lower()


def current_user_role():
    current_user = session.get("user")
    if not current_user:
        return "user"

    role = get_user_role(current_user)
    session["user_role"] = role
    return role


def set_authenticated_session(email):
    profile = build_user_profile(email) or {}
    session.permanent = True
    session["user"] = email
    session["user_role"] = get_user_role(email)
    session["user_full_name"] = profile.get("full_name", "")
    session["issued_at"] = db_now_iso()
    return session["user_role"]


def forbidden_response(message="admin access required"):
    if expects_json_response():
        return jsonify({"error": message}), 403
    return redirect("/")


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        email = session.get("user")
        if not email:
            return auth_required_response()

        issued_at = str(session.get("issued_at") or "").strip()
        if issued_at:
            security = get_user_security_settings_db(email) or {}
            revoked_at = str(security.get("session_revoked_at") or "").strip()
            if revoked_at and revoked_at >= issued_at:
                session.clear()
                return auth_required_response()

        role = get_user_role(email)
        session["user_role"] = role
        if role != "admin":
            return forbidden_response()
        return view(*args, **kwargs)

    return wrapped_view


def render_login_page_view(error_message=None):
    return render_template(
        "login.html",
        needs_registration=not has_users(),
        error_message=error_message,
    )


def render_register_page_view(error_message=None):
    return render_template(
        "register.html",
        has_users=has_users(),
        error_message=error_message,
        min_password_length=AUTH_MIN_PASSWORD_LENGTH,
    )


def auth_success_response(redirect_to="/"):
    if is_ajax_request():
        profile = build_user_profile(session.get("user")) if session.get("user") else None
        return jsonify(
            {
                "ok": True,
                "redirect": redirect_to,
                "role": current_user_role() if session.get("user") else None,
                "profile": profile,
            }
        )
    return redirect(redirect_to)


def auth_error_response(message, status=400, redirect_to=None, page=None):
    payload = {"error": message}
    if redirect_to:
        payload["redirect"] = redirect_to

    if is_ajax_request():
        return jsonify(payload), status

    if page == "login":
        return render_login_page_view(error_message=message), status
    if page == "register":
        return render_register_page_view(error_message=message), status
    if redirect_to:
        return redirect(redirect_to)
    return jsonify(payload), status


def validate_auth_form(email, password):
    if not email or not password:
        return "Email and password are required."
    return None


def contains_devanagari(text):
    return any("\u0900" <= char <= "\u097F" for char in str(text or ""))


def prefers_hindi_response(payload, query):
    preferred_language = str(payload.get("preferred_language", "") or "").lower()
    source = str(payload.get("source", "") or "").lower()
    q_lower = str(query or "").strip().lower()

    if preferred_language.startswith("hi"):
        return True
    if contains_devanagari(query):
        return True
    if any(token in q_lower for token in ("samay", "waqt", "namaste", "kya", "kaise", "haan", "nahi", "band")):
        return True
    return source == "voice"


@app.route("/about")
def about_page():
    return render_template(
        "about.html",
        is_authenticated=bool(session.get("user")),
    )


# ---------------------------------------------------------
# Home route - Dashboard
# ---------------------------------------------------------
@app.route("/")
@login_required
def home():
    role = current_user_role()
    profile = build_user_profile(session.get("user")) or {}
    return render_template(
        "dashboard_new.html",
        current_user=session.get("user"),
        current_user_name=profile.get("display_name", session.get("user")),
        current_user_role=role,
        is_admin=role == "admin",
    )


@app.route("/mobile-camera")
def mobile_camera_page():
    if not (
        session.get("user")
        or has_valid_mobile_camera_token()
    ):
        return auth_required_response()

    device_id = normalize_mobile_camera_id(
        request.args.get("device")
        or request.args.get("device_id")
        or "default"
    )
    urls = build_mobile_camera_urls(device_id)
    return render_template(
        "mobile_camera.html",
        device_id=device_id,
        mobile_page_url=urls["mobile_page_url"],
        mobile_upload_url=urls["mobile_upload_url"],
        mobile_token=MOBILE_CAMERA_TOKEN,
        upload_interval_ms=MOBILE_CAMERA_UPLOAD_INTERVAL_MS,
    )


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("user"):
        return redirect("/")
    if not has_users():
        return redirect("/register")
    return render_login_page_view()


@app.route("/register", methods=["GET"])
def register_page():
    if session.get("user"):
        return redirect("/")
    return render_register_page_view()


# ---------------------------------------------------------
# Health route (auto-refresh + alerts)
# ---------------------------------------------------------
@app.route("/health")
@login_required
def health():
    cache_key = normalize_email(session.get("user")) or "anonymous"
    now_ts = time.time()
    with health_cache_lock:
        cached = health_cache["by_user"].get(cache_key)
        if cached and (now_ts - float(cached.get("updated_ts") or 0.0)) <= HEALTH_CACHE_TTL_SECONDS:
            return jsonify(cached.get("payload") or {})

    cpu = psutil.cpu_percent(interval=0.5)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    devices = list_devices_db()
    active_camera = get_active_camera_profile()
    automation_snapshot = build_automation_snapshot()

    alert = "✅ Normal"
    alert_level = "info"
    if cpu > 85:
        alert = f"⚠️ High CPU usage: {cpu}%"
        alert_level = "warning"
    elif memory > 90:
        alert = f"⚠️ High Memory usage: {memory}%"
        alert_level = "warning"
    elif disk > 90:
        alert = f"⚠️ Low Disk Space: {disk}% used"
        alert_level = "warning"

    if alert_level != "info":
        log_system_alert(
            alert,
            level=alert_level,
            details={
                "cpu_percent": cpu,
                "memory": memory,
                "disk": disk,
            },
        )

    sent_mb, recv_mb = get_network_usage_mb()

    data = {
        "time": now_string(),
        "cpu_percent": cpu,
        "memory": memory,
        "disk": disk,
        "os": platform.platform(),
        "uptime": get_uptime(),
        "processes": safe_psutil_pids_count(),
        "net_sent": sent_mb,
        "net_recv": recv_mb,
        "alert": alert,
        "camera_status": camera_state["message"],
        "camera_available": camera_state["available"],
        "active_camera": active_camera,
        "camera_count": len(list_camera_profiles()),
        "yolo_enabled": YOLO_ENABLED,
        "yolo_status": YOLO_STATUS_MESSAGE,
        "face_recognition_enabled": FACE_RECOGNITION_ENABLED,
        "face_recognition_status": face_state["message"],
        "known_people": face_state["known_people"],
        "current_user_role": current_user_role(),
        "device_count": len(devices),
        "active_device_count": sum(1 for item in devices if item["state"] == "ON"),
        "gemini_configured": bool(GEMINI_API_KEY and GEMINI_AVAILABLE),
        "system_mode": automation_snapshot["mode"],
        "system_mode_label": automation_snapshot["mode_label"],
        "automation_status": automation_snapshot["status"],
        "automation_risk": automation_snapshot["runtime_risk"],
        "environment": automation_snapshot["environment"],
    }
    camera_risk, risk_reasons = build_camera_risk_snapshot()
    data["risk_level"] = camera_risk
    data["risk_score"] = compute_risk_score(camera_risk, risk_reasons)
    data["risk_reasons"] = risk_reasons[:6]
    data["unread_notifications"] = sum(1 for item in list_notifications(session.get("user"), limit=60, unread_only=True) if item)
    data["open_incidents"] = len([item for item in list_incidents(limit=200, status="open") if item])

    with health_cache_lock:
        health_cache["by_user"][cache_key] = {"updated_ts": now_ts, "payload": data}
    return jsonify(data)


# ---------------------------------------------------------
# Camera stream generator (YOLO + fallback)
# ---------------------------------------------------------
def gen_empty_frame():
    """Return a lightweight placeholder frame when the live camera is unavailable."""
    if CAMERA_AVAILABLE and cv2 is not None and np is not None:
        frame = np.zeros((480, 640, 3), dtype="uint8")
        cv2.putText(
            frame,
            camera_state["message"],
            (18, 240),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        ok, buffer = cv2.imencode(".jpg", frame)
        if ok:
            return buffer.tobytes()
    return EMPTY_FRAME_JPEG


def gen_privacy_frame():
    previous_message = camera_state.get("message") or "Camera unavailable"
    camera_state["message"] = "Privacy mode is active"
    try:
        return gen_empty_frame()
    finally:
        camera_state["message"] = previous_message


def stream_frame_bytes(frame_bytes):
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
    )


def stream_placeholder_frames():
    while True:
        yield stream_frame_bytes(gen_empty_frame())
        time.sleep(CAMERA_FALLBACK_DELAY)


def describe_camera_profile(profile):
    active_profile = profile or get_active_camera_profile()
    if active_profile.get("transport") == "mobile":
        transport = "mobile"
    elif active_profile.get("transport") == "network":
        transport = "network"
    else:
        transport = "local"
    return f"{active_profile['name']} ({transport})"


def build_mobile_camera_health(device_id):
    normalized_id = normalize_mobile_camera_id(device_id)
    with mobile_camera_lock:
        same_device = normalize_mobile_camera_id(mobile_camera_state.get("device_id")) == normalized_id
        updated_ts = float(mobile_camera_state.get("updated_ts") or 0.0) if same_device else 0.0
        last_frame_age_ms = int(max(0.0, (time.time() - updated_ts) * 1000)) if updated_ts else None
        fresh = same_device and mobile_camera_frame_is_fresh(mobile_camera_state)
        return {
            "device_id": normalized_id,
            "frame_count": int(mobile_camera_state.get("frame_count") or 0) if same_device else 0,
            "fresh": bool(fresh),
            "last_error": mobile_camera_state.get("last_error") if same_device else None,
            "last_frame_at": mobile_camera_state.get("updated_at") if same_device else None,
            "last_frame_age_ms": last_frame_age_ms,
        }


def build_camera_profile_status(profile):
    active_profile = get_active_camera_profile(refresh=False)
    is_active = bool(profile and profile.get("id") == active_profile.get("id"))
    if is_active:
        status = "online" if camera_state.get("available") else "offline"
        message = camera_state.get("message") or "No camera status yet"
    elif profile.get("enabled") is False:
        status = "disabled"
        message = "Camera is disabled"
    else:
        status = "unknown"
        message = "Not checked in this session"

    return {
        "status": status,
        "available": status == "online",
        "checked_at": camera_state.get("updated_at") if is_active else None,
        "message": message,
        "last_error": camera_state.get("last_error") if is_active else None,
        "last_frame_at": camera_state.get("last_frame_at") if is_active else None,
        "stream_source": camera_state.get("stream_source") if is_active else profile.get("transport"),
        "active": is_active,
    }


def attach_camera_status(profile):
    profile_copy = dict(profile)
    profile_copy["status"] = build_camera_profile_status(profile_copy)
    return profile_copy


def check_camera_profile(profile):
    if profile is None:
        return None

    checked_at = now_iso()
    result = {
        "camera_id": profile["id"],
        "camera_name": profile["name"],
        "source": profile["source_display"],
        "transport": profile["transport"],
        "checked_at": checked_at,
        "available": False,
        "status": "offline",
        "message": "",
        "reason": None,
    }

    if profile.get("enabled") is False:
        result.update({"status": "disabled", "message": "Camera is disabled", "reason": "camera_disabled"})
        return result

    if profile.get("transport") == "mobile" or is_mobile_camera_source(profile.get("source")):
        device_id = get_mobile_camera_id_from_source(profile.get("source")) or "default"
        mobile_health = build_mobile_camera_health(device_id)
        result["mobile"] = mobile_health
        if mobile_health["fresh"]:
            result.update({"available": True, "status": "online", "message": "Mobile camera frames are fresh"})
        else:
            result.update({"message": "Waiting for fresh mobile camera frames", "reason": mobile_health.get("last_error") or "mobile_frame_timeout"})
        return result

    if not CAMERA_AVAILABLE or cv2 is None:
        result.update({"message": "OpenCV camera support is unavailable", "reason": "opencv_unavailable"})
        return result

    if os.getenv("DISABLE_CAMERA") == "1":
        result.update({"message": "Camera disabled in configuration", "reason": "camera_disabled"})
        return result

    cap = None
    try:
        capture_source = coerce_camera_capture_source(profile["source"])
        cap = cv2.VideoCapture(capture_source)
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1500)
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1500)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
        opened = bool(cap.isOpened()) if hasattr(cap, "isOpened") else True
        ok, frame = cap.read()
        if opened and ok and frame is not None:
            result.update({"available": True, "status": "online", "message": "Camera opened and returned a frame"})
        else:
            result.update({"message": "Camera did not return a readable frame", "reason": "camera_read_failed"})
    except Exception as exc:
        result.update({"message": f"Camera check failed: {exc}", "reason": str(exc)})
    finally:
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass
    return result


def init_camera():
    """Try to open the configured camera."""
    update_camera_diagnostics(stream_source="server")
    active_profile = get_active_camera_profile()
    update_camera_profile_state(active_profile)

    if active_profile.get("transport") == "mobile" or is_mobile_camera_source(active_profile.get("source")):
        update_camera_status(False, f"Waiting for mobile stream: {active_profile['name']}")
        update_camera_diagnostics(stream_source="mobile", last_error="mobile_stream_waiting")
        return None

    if not CAMERA_AVAILABLE:
        update_camera_status(False, "OpenCV camera support is unavailable")
        update_camera_diagnostics(last_error="opencv_unavailable")
        set_camera_alert(
            "⚠️ Camera support is unavailable on this system",
            level="error",
            details={"reason": "opencv_unavailable", "detections": []},
            log=True,
        )
        return None

    if os.getenv("DISABLE_CAMERA") == "1":
        update_camera_status(False, "Camera disabled in configuration")
        update_camera_diagnostics(last_error="camera_disabled")
        set_camera_alert(
            "⚠️ Camera is disabled in configuration",
            level="info",
            details={"reason": "disabled", "detections": []},
            log=False,
        )
        return None

    try:
        capture_source = coerce_camera_capture_source(active_profile["source"])
        cam = cv2.VideoCapture(capture_source)
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        ok, _ = cam.read()
        if not ok:
            cam.release()
            update_camera_status(False, f"{describe_camera_profile(active_profile)} is unavailable")
            update_camera_diagnostics(last_error=f"camera_read_failed:{active_profile['source_display']}")
            set_camera_alert(
                "⚠️ Camera feed is unavailable",
                level="error",
                details={
                    "reason": "camera_read_failed",
                    "camera": active_profile,
                    "detections": [],
                },
                log=True,
            )
            return None

        update_camera_status(True, f"Server AI camera ready: {describe_camera_profile(active_profile)}")
        update_camera_diagnostics(last_error=None)
        return cam
    except Exception as exc:
        update_camera_status(False, f"Camera error: {exc}")
        update_camera_diagnostics(last_error=str(exc))
        set_camera_alert(
            "⚠️ Camera error while starting the feed",
            level="error",
            details={"reason": str(exc), "detections": []},
            log=True,
        )
        return None


def process_frame_and_alerts(frame):
    frame, yolo_result = run_yolo_on_frame(frame)
    face_result = run_face_recognition_on_frame(frame)
    apply_frame_alerts(face_result, yolo_result, frame=frame)
    return frame


def decode_mobile_frame_for_ai(frame_bytes):
    if not (CAMERA_AVAILABLE and cv2 is not None and np is not None):
        return None

    try:
        byte_array = np.frombuffer(frame_bytes, dtype=np.uint8)
        if byte_array.size == 0:
            return None
        return cv2.imdecode(byte_array, cv2.IMREAD_COLOR)
    except Exception:
        return None


def generate_mobile_frames(profile):
    frame_counter = 0
    device_id = get_mobile_camera_id_from_source(profile.get("source")) or "default"
    update_camera_profile_state(profile)
    update_camera_diagnostics(stream_source="mobile")

    while True:
        frame_bytes, frame_time, last_error = get_latest_mobile_camera_frame(device_id)
        if not frame_bytes:
            update_camera_status(False, f"Waiting for mobile camera stream: {profile['name']}")
            update_camera_diagnostics(
                stream_source="mobile",
                last_error=last_error or "mobile_frame_timeout",
            )
            set_camera_alert(
                "⚠️ Mobile camera stream is waiting for frames",
                level="info",
                details={"reason": "mobile_frame_timeout", "camera": profile, "detections": []},
                log=False,
            )
            yield stream_frame_bytes(gen_empty_frame())
            time.sleep(CAMERA_FALLBACK_DELAY)
            continue

        frame_counter += 1
        update_camera_status(True, f"Mobile camera feed running: {profile['name']}", last_frame_at=frame_time or now_iso())
        update_camera_diagnostics(
            frames_processed=frame_counter,
            stream_source="mobile",
            last_error=None,
        )

        processed_bytes = frame_bytes
        frame = decode_mobile_frame_for_ai(frame_bytes)
        if frame is not None:
            processed = process_frame_and_alerts(frame)
            ok, buffer = cv2.imencode(".jpg", processed)
            if ok:
                processed_bytes = buffer.tobytes()

        yield stream_frame_bytes(processed_bytes)
        time.sleep(0.02)


# Initialize YOLO model
def init_yolo():
    global YOLO_MODEL, YOLO_ENABLED, YOLO_STATUS_MESSAGE
    if not YOLO_AVAILABLE:
        YOLO_ENABLED = False
        YOLO_STATUS_MESSAGE = "Ultralytics package not installed"
        print("⚠️ Ultralytics is not installed, running without YOLO")
        return

    if os.getenv("DISABLE_YOLO", "0") == "1":
        YOLO_ENABLED = False
        YOLO_STATUS_MESSAGE = "YOLO disabled from environment"
        print("⚠️ YOLO disabled from environment")
        return

    if not os.path.exists(YOLO_MODEL_PATH):
        YOLO_ENABLED = False
        YOLO_STATUS_MESSAGE = f"YOLO model missing at {YOLO_MODEL_PATH}"
        print(f"⚠️ YOLO model file not found: {YOLO_MODEL_PATH}")
        return

    try:
        YOLO_MODEL = YOLO(YOLO_MODEL_PATH)
        YOLO_ENABLED = True
        YOLO_STATUS_MESSAGE = f"YOLO ready with model {os.path.basename(YOLO_MODEL_PATH)}"
        print(f"✅ YOLO loaded: {YOLO_MODEL_PATH}")
    except Exception as e:
        YOLO_ENABLED = False
        YOLO_STATUS_MESSAGE = f"YOLO load error: {e}"
        print(f"⚠️ YOLO model load error, running without YOLO: {e}")


init_yolo()
init_face_recognition()


def run_yolo_on_frame(frame):
    """Run YOLO on a single frame, annotate it, and return alert metadata."""
    result = {
        "detections": [],
        "objects": [],
        "message": "✅ No alerts",
        "level": "info",
        "log": False,
    }

    if not YOLO_ENABLED or YOLO_MODEL is None:
        return frame, result

    try:
        results = YOLO_MODEL(frame, verbose=False)
        res = results[0] if results else None

        labels_detected = Counter()
        alert_hits = Counter()
        object_rows = []
        frame_height = int(getattr(frame, "shape", [0, 0])[0] or 0)
        frame_width = int(getattr(frame, "shape", [0, 0])[1] or 0)

        if hasattr(res, "boxes") and res.boxes is not None:
            for box in res.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                if conf < YOLO_CONFIDENCE:
                    continue
                label = get_yolo_label(cls_id)
                labels_detected[label] += 1
                normalized_label = label.lower()
                if normalized_label in ALERT_OBJECTS:
                    alert_hits[label] += 1

                x1, y1, x2, y2 = map(int, box.xyxy[0])
                color = (0, 0, 255) if normalized_label in ALERT_OBJECTS else (0, 255, 0)
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                text = f"{label} {conf:.2f}"
                cv2.putText(
                    frame,
                    text,
                    (x1, max(20, y1 - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    color,
                    2,
                    cv2.LINE_AA,
                )
                cx = int((x1 + x2) / 2)
                cy = int((y1 + y2) / 2)
                object_rows.append(
                    {
                        "label": label,
                        "confidence": round(conf, 3),
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "cx": cx,
                        "cy": cy,
                        "nx": round(cx / frame_width, 4) if frame_width else None,
                        "ny": round(cy / frame_height, 4) if frame_height else None,
                    }
                )

        detections = [
            {"label": label, "count": count}
            for label, count in labels_detected.most_common()
        ]
        result["detections"] = detections
        result["objects"] = object_rows[:120]

        person_count = sum(
            count for label, count in labels_detected.items() if label.lower() == "person"
        )

        # Update human behavior tracking
        now = time.time()
        if person_count > 0:
            if not human_behavior["person_present"]:
                human_behavior["person_present"] = True
                human_behavior["person_last_seen"] = now
                human_behavior["person_count"] += 1
                human_behavior["last_activity"] = "entered"
                human_behavior["interaction_count"] += 1
            else:
                human_behavior["person_duration"] = now - human_behavior["person_last_seen"]
                if human_behavior["person_duration"] > 300:  # 5 minutes
                    human_behavior["last_activity"] = "staying_long"
                elif human_behavior["person_duration"] > 60:  # 1 minute
                    human_behavior["last_activity"] = "active"
                else:
                    human_behavior["last_activity"] = "just_arrived"
        else:
            if human_behavior["person_present"]:
                human_behavior["person_present"] = False
                human_behavior["last_activity"] = "left"
                human_behavior["person_duration"] = 0
            else:
                human_behavior["last_activity"] = "idle"

        critical_hits = Counter()
        for label, count in alert_hits.items():
            if str(label or "").strip().lower() in DEFENSE_TRIGGER_OBJECTS:
                critical_hits[label] = count

        if critical_hits:
            result.update(
                {
                    "message": f"🚨 Critical objects detected: {format_detection_summary(critical_hits)}",
                    "level": "error",
                    "log": True,
                }
            )
        elif alert_hits:
            result.update(
                {
                    "message": f"⚠️ Alert objects detected: {format_detection_summary(alert_hits)}",
                    "level": "warning",
                    "log": True,
                }
            )
        elif person_count > 0:
            result.update(
                {
                    "message": f"⚠️ Person detected on camera ({person_count})",
                    "level": "warning",
                    "log": True,
                }
            )
        elif labels_detected:
            result.update(
                {
                    "message": f"ℹ️ Objects detected: {format_detection_summary(labels_detected)}",
                    "level": "info",
                    "log": False,
                }
            )
    except Exception as exc:
        result.update(
            {
                "message": "⚠️ YOLO detection error",
                "level": "error",
                "log": True,
                "reason": str(exc),
            }
        )

    return frame, result


def apply_frame_alerts(face_result, yolo_result, frame=None):
    details = {
        "detections": list(yolo_result.get("detections", [])),
        "objects": list(yolo_result.get("objects", [])),
        "faces": list(face_result.get("faces", [])),
    }

    try:
        active_camera = get_active_camera_profile(refresh=False)
        vision_telemetry.appendleft(
            {
                "time": now_iso(),
                "camera_id": active_camera.get("id"),
                "camera_name": active_camera.get("name"),
                "objects": details["objects"][:120],
            }
        )
    except Exception:
        pass

    if yolo_result.get("reason"):
        details["reason"] = yolo_result["reason"]
        update_camera_diagnostics(last_error=yolo_result["reason"])

    if details["detections"] or details["faces"]:
        update_camera_diagnostics(
            active_objects=[*details["detections"], *details["faces"]],
            last_detection_at=now_iso(),
            last_error=None,
        )
    else:
        update_camera_diagnostics(active_objects=[], last_error=None)

    if face_result.get("unknown_count", 0) > 0:
        count = face_result["unknown_count"]
        noun = "face" if count == 1 else "faces"
        payload = set_camera_alert(
            f"⚠️ Unknown {noun} detected ({count})",
            level="warning",
            details=details,
            log=True,
        )
        camera_risk, risk_reasons = build_camera_risk_snapshot()
        details.update(
            {
                "risk_level": camera_risk,
                "risk_score": compute_risk_score(camera_risk, risk_reasons),
                "risk_reasons": risk_reasons[:6],
                "tags": ["intrusion", "face"],
            }
        )
        maybe_auto_create_incident(
            f"Unknown visitor detected ({count})",
            severity="warning",
            source="camera",
            camera_id=get_active_camera_profile(refresh=False).get("id"),
            details=details,
            frame=frame,
        )
        return

    if face_result.get("recognized_count", 0) > 0:
        prefix = "ℹ️ Recognized face: "
        if face_result["recognized_count"] > 1 or len(face_result.get("faces", [])) > 1:
            prefix = "ℹ️ Recognized faces: "
        set_camera_alert(
            f"{prefix}{format_face_summary(face_result['faces'])}",
            level="info",
            details=details,
            log=False,
        )
        return

    payload = set_camera_alert(
        yolo_result.get("message", "✅ No alerts"),
        level=yolo_result.get("level", "info"),
        details=details,
        log=yolo_result.get("log", False),
    )

    camera_risk, risk_reasons = build_camera_risk_snapshot()
    details.update(
        {
            "risk_level": camera_risk,
            "risk_score": compute_risk_score(camera_risk, risk_reasons),
            "risk_reasons": risk_reasons[:6],
            "recommendations": build_ai_recommendations(camera_risk, risk_reasons),
        }
    )

    if str(payload.get("level") or "").lower() in {"error"}:
        maybe_auto_create_incident(
            payload.get("message") or "Critical camera event",
            severity="critical",
            source="camera",
            camera_id=get_active_camera_profile(refresh=False).get("id"),
            details=details,
            frame=frame,
        )


def generate_frames():
    intelligence = get_camera_intelligence_state()
    if intelligence.get("privacy_mode"):
        update_camera_status(False, "Privacy mode is active")
        update_camera_diagnostics(stream_source="privacy", active_objects=[], last_error=None)
        set_camera_alert(
            "Privacy mode active. Camera monitoring is paused.",
            level="info",
            details={"privacy_mode": True, "detections": [], "faces": [], "objects": []},
            log=False,
        )
        while True:
            yield stream_frame_bytes(gen_privacy_frame())
            time.sleep(CAMERA_FALLBACK_DELAY)

    active_profile = get_active_camera_profile()
    if active_profile.get("transport") == "mobile" or is_mobile_camera_source(active_profile.get("source")):
        yield from generate_mobile_frames(active_profile)
        return

    frame_counter = 0
    cam = init_camera()
    if cam is None:
        yield from stream_placeholder_frames()
        return

    try:
        while True:
            success, frame = cam.read()
            if not success:
                update_camera_status(False, "Camera feed interrupted")
                update_camera_diagnostics(last_error="stream_read_failed")
                set_camera_alert(
                    "⚠️ Camera feed interrupted",
                    level="error",
                    details={"reason": "stream_read_failed", "detections": []},
                    log=True,
                )
                break

            frame_time = now_iso()
            update_camera_status(True, "Server AI camera feed running", last_frame_at=frame_time)
            frame_counter += 1
            update_camera_diagnostics(frames_processed=frame_counter)

            frame = process_frame_and_alerts(frame)

            ret, buffer = cv2.imencode(".jpg", frame)
            if not ret:
                continue

            yield stream_frame_bytes(buffer.tobytes())
    finally:
        cam.release()

    yield from stream_placeholder_frames()


@app.route("/camera_feed")
@app.route("/camera-feed")
@login_required
def camera_feed():
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------
# Endpoint to return latest camera alert (polled by UI)
# ---------------------------------------------------------
@app.route("/get_alert")
@login_or_local_voice_required
def get_alert():
    payload = dict(latest_camera_alert_payload)
    active_camera = get_active_camera_profile()
    intelligence = get_camera_intelligence_state()
    automation_snapshot = evaluate_automation(source="camera-poll")
    payload["camera_available"] = camera_state["available"]
    payload["camera_status"] = camera_state["message"]
    payload["active_camera"] = active_camera
    payload["intelligence"] = intelligence
    payload["privacy_mode"] = bool(intelligence.get("privacy_mode"))
    payload["camera_count"] = len(list_camera_profiles())
    payload["yolo_enabled"] = YOLO_ENABLED
    payload["face_recognition_enabled"] = FACE_RECOGNITION_ENABLED
    payload["face_recognition_status"] = face_state["message"]
    payload["known_people"] = face_state["known_people"]
    payload["detections"] = latest_detections
    payload["faces"] = latest_faces
    payload["objects"] = latest_detection_objects
    payload["current_user_role"] = current_user_role() if session.get("user") else "system"
    payload["automation"] = automation_snapshot
    with mobile_camera_lock:
        mobile_updated_ts = float(mobile_camera_state.get("updated_ts") or 0.0)
        mobile_age_ms = int(max(0.0, (time.time() - mobile_updated_ts) * 1000)) if mobile_updated_ts else None
        mobile_snapshot = {
            "device_id": mobile_camera_state.get("device_id"),
            "frame_count": int(mobile_camera_state.get("frame_count") or 0),
            "last_error": mobile_camera_state.get("last_error"),
            "last_frame_age_ms": mobile_age_ms,
        }

    payload["camera_diagnostics"] = {
        "frames_processed": camera_state["frames_processed"],
        "last_detection_at": camera_state["last_detection_at"],
        "last_error": camera_state["last_error"],
        "stream_source": camera_state["stream_source"],
        "active_objects": camera_state["active_objects"],
        "object_count": camera_state["object_count"],
        "yolo_status": YOLO_STATUS_MESSAGE,
        "model_path": YOLO_MODEL_PATH,
        "active_camera_id": active_camera["id"],
        "active_camera_name": active_camera["name"],
        "active_camera_source": active_camera["source_display"],
        "privacy_mode": bool(intelligence.get("privacy_mode")),
        "mobile": mobile_snapshot,
    }

    # include quick system stats so UI can show CPU/memory alongside alerts
    try:
        payload["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        payload["memory_percent"] = psutil.virtual_memory().percent
    except Exception:
        payload["cpu_percent"] = None
        payload["memory_percent"] = None

    return jsonify(payload)


@app.route("/alerts")
@login_required
def alerts():
    items = build_alert_feed()
    level_summary = Counter(item.get("level", "info") for item in items)
    return jsonify(
        {
            "items": items,
            "latest_camera_alert": latest_camera_alert_payload,
            "camera_status": camera_state,
            "face_status": face_state,
            "automation": build_automation_snapshot(),
            "summary": {
                "total": len(items),
                "warning": level_summary.get("warning", 0),
                "error": level_summary.get("error", 0),
                "info": level_summary.get("info", 0),
            },
        }
    )


@app.route("/behavior")
@login_or_local_voice_required
def get_behavior():
    return jsonify(human_behavior)


def format_face_skip_reason(reason):
    labels = {
        "no_face": "No face found in image",
        "multiple_faces": "Image contains multiple faces",
        "invalid_face": "Face crop could not be processed",
        "unsupported_extension": "Unsupported image format",
        "empty": "Empty upload",
        "too_large": "Image file is too large",
        "unreadable": "Image could not be decoded",
        "opencv_unavailable": "OpenCV image decoding is unavailable",
        "detector_unavailable": "Face detector is unavailable",
    }
    return labels.get(reason, str(reason or "unknown_error").replace("_", " ").strip())


def build_camera_registry_snapshot():
    profiles = [attach_camera_status(profile) for profile in list_camera_profiles()]
    active_profile = get_active_camera_profile(refresh=False)
    active_profile = next((profile for profile in profiles if profile["id"] == active_profile["id"]), active_profile)
    return {
        "active_camera": active_profile,
        "active_camera_id": active_profile["id"],
        "cameras": profiles,
        "camera_count": len(profiles),
        "camera_status": camera_state["message"],
        "camera_available": camera_state["available"],
        "intelligence": get_camera_intelligence_state(),
    }


def build_face_registry_snapshot():
    return {
        "people": list_known_face_people(),
        "known_people": face_state["known_people"],
        "face_recognition_enabled": FACE_RECOGNITION_ENABLED,
        "face_recognition_status": face_state["message"],
        "latest_faces": latest_faces,
        "known_faces_dir": KNOWN_FACES_DIR,
    }


def decode_mobile_frame_bytes(payload, raw_body):
    image_data = (
        payload.get("image")
        or payload.get("frame")
        or payload.get("frame_data")
    )
    if isinstance(image_data, str):
        encoded = image_data.strip()
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1] if "," in encoded else ""
        if not encoded:
            return None, "Frame data is empty"
        try:
            return base64.b64decode(encoded, validate=True), None
        except Exception:
            return None, "Frame data is not valid base64"

    if raw_body:
        return raw_body, None

    return None, "Frame data is required"


def build_mobile_camera_api_response(profile, include_link=True):
    snapshot = build_camera_registry_snapshot()
    snapshot.update(
        {
            "ok": True,
            "camera": profile,
            "camera_source": profile["source"],
            "device_id": get_mobile_camera_id_from_source(profile["source"]),
        }
    )
    if include_link:
        snapshot.update(build_mobile_camera_urls(snapshot["device_id"]))
    return snapshot


@app.route("/api/mobile-camera/link", methods=["POST"])
@login_required
def api_mobile_camera_link():
    payload = request.get_json(silent=True) or {}
    device_id = normalize_mobile_camera_id(payload.get("device_id") or payload.get("device") or "default")
    name = payload.get("name") or f"Mobile {device_id}"
    set_active = coerce_bool_flag(payload.get("set_active"), True)

    profile = register_mobile_camera_profile(
        device_id=device_id,
        name=name,
        set_active=set_active,
    )
    log_activity(
        "mobile_camera_link_generated",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="camera",
        target_name=profile["name"],
        source="camera",
        details={"camera_id": profile["id"], "source": profile["source_display"]},
    )
    return jsonify(build_mobile_camera_api_response(profile, include_link=True))


@app.route("/api/mobile-camera/frame", methods=["POST"])
def api_mobile_camera_frame():
    raw_body = request.get_data(cache=True) or b""
    payload = request.get_json(silent=True) or {}
    if not is_mobile_camera_upload_authorized(payload):
        return jsonify({"error": "authentication required"}), 401

    frame_bytes, decode_error = decode_mobile_frame_bytes(payload, raw_body)
    if decode_error:
        mark_mobile_camera_error(decode_error)
        return jsonify({"error": decode_error}), 400
    if len(frame_bytes) > MOBILE_CAMERA_FRAME_MAX_BYTES:
        mark_mobile_camera_error("frame_too_large")
        return jsonify({"error": f"Frame exceeds {MOBILE_CAMERA_FRAME_MAX_BYTES} bytes"}), 413

    if CAMERA_AVAILABLE and cv2 is not None and np is not None:
        decoded = decode_mobile_frame_for_ai(frame_bytes)
        if decoded is None:
            mark_mobile_camera_error("invalid_image_data")
            return jsonify({"error": "Uploaded frame is not a valid image"}), 400

    device_id = normalize_mobile_camera_id(
        payload.get("device_id")
        or payload.get("device")
        or request.args.get("device_id")
        or request.args.get("device")
        or "default"
    )
    frame_meta = update_mobile_camera_frame(device_id, frame_bytes)
    active_camera = get_active_camera_profile()
    if get_mobile_camera_id_from_source(active_camera.get("source")) == device_id:
        update_camera_status(True, f"Mobile camera connected: {active_camera['name']}", last_frame_at=frame_meta["updated_at"])
        update_camera_diagnostics(stream_source="mobile", last_error=None)

    return jsonify({"ok": True, **frame_meta})


@app.route("/api/cameras", methods=["GET"])
@app.route("/api/camera", methods=["GET"])
@login_required
def api_list_cameras():
    return jsonify(build_camera_registry_snapshot())


@app.route("/api/cameras/check", methods=["POST"])
@app.route("/api/camera/check", methods=["POST"])
@login_required
def api_check_cameras():
    refresh_camera_registry()
    results = [check_camera_profile(profile) for profile in camera_registry]
    active_profile = get_active_camera_profile(refresh=False)
    active_result = next((item for item in results if item and item["camera_id"] == active_profile["id"]), None)
    if active_result:
        update_camera_status(
            active_result["available"],
            active_result["message"],
            last_frame_at=active_result["checked_at"] if active_result["available"] else camera_state.get("last_frame_at"),
        )
        update_camera_diagnostics(
            stream_source=active_result["transport"],
            last_error=None if active_result["available"] else active_result.get("reason"),
        )

    snapshot = build_camera_registry_snapshot()
    snapshot.update({"ok": True, "checks": results, "checked_at": now_iso()})
    return jsonify(snapshot)


@app.route("/api/camera-intelligence", methods=["GET"])
@app.route("/api/cameras/intelligence", methods=["GET"])
@login_required
def api_get_camera_intelligence():
    return jsonify(
        {
            "ok": True,
            "intelligence": get_camera_intelligence_state(),
            "active_camera": get_active_camera_profile(),
        }
    )


@app.route("/api/camera-intelligence", methods=["POST"])
@app.route("/api/cameras/intelligence", methods=["POST"])
@login_required
def api_update_camera_intelligence():
    payload = request.get_json(silent=True) or {}
    previous_state = get_camera_intelligence_state()
    state = update_camera_intelligence_state(payload)

    if state.get("privacy_mode"):
        update_camera_status(False, "Privacy mode is active")
        update_camera_diagnostics(stream_source="privacy", active_objects=[], last_error=None)
        set_camera_alert(
            "Privacy mode active. Camera monitoring is paused.",
            level="info",
            details={"privacy_mode": True, "detections": [], "faces": [], "objects": []},
            log=False,
        )

    log_activity(
        "camera_intelligence_updated",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="camera",
        target_name=get_active_camera_profile().get("name"),
        source="camera",
        details={
            "privacy_changed": previous_state.get("privacy_mode") != state.get("privacy_mode"),
            "privacy_mode": state.get("privacy_mode"),
            "sensitivity": state.get("sensitivity"),
            "patrol_enabled": state.get("patrol", {}).get("enabled"),
            "zone_count": len(state.get("activity_zones") or []),
        },
    )
    return jsonify(
        {
            "ok": True,
            "intelligence": state,
            "active_camera": get_active_camera_profile(),
        }
    )


def resolve_camera_profile(camera_id):
    lookup = str(camera_id or "").strip().lower()
    if not lookup:
        return None
    for profile in list_camera_profiles():
        if (
            str(profile.get("id") or "").strip().lower() == lookup
            or str(profile.get("name") or "").strip().lower() == lookup
        ):
            return profile
    return None


@app.route("/api/cameras/<path:camera_id>/snapshot", methods=["GET"])
@app.route("/api/camera/<path:camera_id>/snapshot", methods=["GET"])
@login_required
def api_camera_snapshot(camera_id):
    profile = resolve_camera_profile(camera_id)
    if profile is None:
        return jsonify({"error": "Camera profile not found"}), 404
    if get_camera_intelligence_state().get("privacy_mode"):
        response = Response(gen_privacy_frame(), mimetype="image/jpeg")
        response.headers["Cache-Control"] = "no-store"
        return response

    max_width = get_env_int("CAMERA_SNAPSHOT_MAX_WIDTH", 520)
    try:
        max_width = int(request.args.get("w") or max_width)
    except Exception:
        max_width = max_width
    max_width = max(120, min(1280, int(max_width)))

    frame = None
    if profile.get("transport") == "mobile" or is_mobile_camera_source(profile.get("source")):
        device_id = get_mobile_camera_id_from_source(profile.get("source")) or "default"
        frame_bytes, _, _ = get_latest_mobile_camera_frame(device_id)
        if frame_bytes:
            frame = decode_mobile_frame_for_ai(frame_bytes)
    else:
        if CAMERA_AVAILABLE and cv2 is not None:
            try:
                capture_source = coerce_camera_capture_source(profile.get("source"))
                cap = cv2.VideoCapture(capture_source)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
                ok, grabbed = cap.read()
                cap.release()
                if ok:
                    frame = grabbed
            except Exception:
                frame = None

    if frame is None:
        payload = gen_empty_frame()
        return Response(payload, mimetype="image/jpeg")

    if CAMERA_AVAILABLE and cv2 is not None:
        try:
            height, width = frame.shape[:2]
            if width and width > max_width:
                scale = max_width / float(width)
                frame = cv2.resize(frame, (max_width, int(height * scale)))
        except Exception:
            pass

    ok, buffer = cv2.imencode(".jpg", frame) if CAMERA_AVAILABLE and cv2 is not None else (False, None)
    if not ok or buffer is None:
        return Response(gen_empty_frame(), mimetype="image/jpeg")

    response = Response(buffer.tobytes(), mimetype="image/jpeg")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/cameras/<path:camera_id>/check", methods=["POST"])
@app.route("/api/camera/<path:camera_id>/check", methods=["POST"])
@login_required
def api_check_camera(camera_id):
    profile = resolve_camera_profile(camera_id)
    if profile is None:
        return jsonify({"error": "Camera profile not found"}), 404

    result = check_camera_profile(profile)
    active_profile = get_active_camera_profile(refresh=False)
    if profile["id"] == active_profile["id"]:
        update_camera_status(
            result["available"],
            result["message"],
            last_frame_at=result["checked_at"] if result["available"] else camera_state.get("last_frame_at"),
        )
        update_camera_diagnostics(
            stream_source=result["transport"],
            last_error=None if result["available"] else result.get("reason"),
        )

    snapshot = build_camera_registry_snapshot()
    snapshot.update({"ok": True, "check": result})
    return jsonify(snapshot)


@app.route("/api/cameras", methods=["POST"])
@app.route("/api/camera", methods=["POST"])
@login_required
def api_add_camera():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    source = str(payload.get("source") or "").strip()
    camera_type = str(payload.get("type") or "").strip().lower() or "network"

    if not name or not source:
        return jsonify({"error": "Camera name and source are required"}), 400

    profile = build_camera_profile(
        profile_id=f"cam-{int(time.time())}",
        name=name,
        source=source,
        camera_type=camera_type,
        enabled=True,
    )

    with camera_registry_lock:
        existing = load_camera_profiles_from_disk()
        existing_sources = {p["source_display"].lower() for p in existing if isinstance(p, dict)}
        existing_names = {p["name"].lower() for p in existing if isinstance(p, dict)}

        if profile["source_display"].lower() in existing_sources or profile["name"].lower() in existing_names:
            return jsonify({"error": "A camera with the same name or source already exists"}), 400

        existing.append(profile)
        save_camera_profiles_to_disk(existing)
        refresh_camera_registry()

    snapshot = build_camera_registry_snapshot()
    snapshot.update({"ok": True, "camera": profile})
    return jsonify(snapshot)


@app.route("/api/cameras/mobile", methods=["POST"])
@app.route("/api/camera/mobile", methods=["POST"])
@login_required
def api_register_mobile_camera():
    payload = request.get_json(silent=True) or {}
    device_id = normalize_mobile_camera_id(payload.get("device_id") or payload.get("device") or "default")
    name = payload.get("name") or f"Mobile {device_id}"
    set_active = coerce_bool_flag(payload.get("set_active"), False)
    profile = register_mobile_camera_profile(device_id=device_id, name=name, set_active=set_active)
    return jsonify(build_mobile_camera_api_response(profile, include_link=True))


@app.route("/api/cameras/<path:camera_id>", methods=["DELETE"])
@app.route("/api/camera/<path:camera_id>", methods=["DELETE"])
@login_required
def api_delete_camera(camera_id):
    lookup = str(camera_id or "").strip().lower()
    if not lookup:
        return jsonify({"error": "Camera identifier is required"}), 400

    with camera_registry_lock:
        existing = load_camera_profiles_from_disk()
        remaining = []
        deleted = None

        for profile in existing:
            if not isinstance(profile, dict):
                continue
            profile_matches = {
                str(profile.get("id", "")).strip().lower(),
                str(profile.get("name", "")).strip().lower(),
                str(profile.get("source_display", profile.get("source", ""))).strip().lower(),
            }
            if deleted is None and lookup in profile_matches:
                deleted = profile
                continue
            remaining.append(profile)

        if deleted is None:
            return jsonify({"error": "Camera profile not found"}), 404

        save_camera_profiles_to_disk(remaining)
        refresh_camera_registry()

    log_activity(
        "camera_deleted",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="camera",
        target_name=deleted["name"],
        source="camera",
        details={"camera_id": deleted["id"], "source": deleted["source_display"]},
    )
    snapshot = build_camera_registry_snapshot()
    snapshot.update({"ok": True, "deleted": deleted["name"]})
    return jsonify(snapshot)


@app.route("/api/cameras/active", methods=["POST"])
@app.route("/api/camera/active", methods=["POST"])
@login_required
def api_set_active_camera():
    payload = request.get_json(silent=True) or {}
    direction = str(payload.get("direction", "") or "").strip().lower()
    camera_id = payload.get("camera_id") or payload.get("id")

    if direction in {"next", "forward"}:
        profile = cycle_active_camera(1)
    elif direction in {"previous", "prev", "back"}:
        profile = cycle_active_camera(-1)
    else:
        profile = set_active_camera_profile(camera_id)

    if profile is None:
        return jsonify({"error": "Camera profile not found"}), 404

    log_activity(
        "camera_selected",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="camera",
        target_name=profile["name"],
        source="camera",
        details={"camera_id": profile["id"], "source": profile["source_display"]},
    )
    snapshot = build_camera_registry_snapshot()
    snapshot["ok"] = True
    return jsonify(snapshot)


@app.route("/api/faces", methods=["GET"])
@login_required
def api_list_faces():
    return jsonify(build_face_registry_snapshot())


@app.route("/api/faces", methods=["POST"])
@login_required
def api_register_face():
    person_name = normalize_person_name(request.form.get("name") or request.form.get("person_name"))
    files = request.files.getlist("images")
    saved_files, skipped, error_message = save_known_face_uploads(person_name, files)

    if error_message:
        return jsonify(
            {
                "error": error_message,
                "saved_files": [],
                "skipped": {key: int(value) for key, value in skipped.items()},
            }
        ), 400

    if not saved_files:
        return jsonify(
            {
                "error": "No valid face images were uploaded.",
                "skipped": {key: int(value) for key, value in skipped.items()},
                "skip_messages": [format_face_skip_reason(reason) for reason in skipped],
            }
        ), 400

    init_face_recognition()
    log_activity(
        "face_registered",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="face",
        target_name=person_name,
        source="camera",
        details={
            "saved_files": len(saved_files),
            "skipped": {key: int(value) for key, value in skipped.items()},
        },
    )

    payload = build_face_registry_snapshot()
    payload.update(
        {
            "ok": True,
            "person": person_name,
            "saved_files": saved_files,
            "skipped": {key: int(value) for key, value in skipped.items()},
            "skip_messages": [format_face_skip_reason(reason) for reason in skipped],
        }
    )
    return jsonify(payload)


@app.route("/api/faces/<path:name>", methods=["DELETE"])
@login_required
def api_delete_face(name):
    person_name, person_dir = resolve_known_face_person_dir(name)
    if not person_dir:
        return jsonify({"error": "Known person not found"}), 404

    shutil.rmtree(person_dir, ignore_errors=True)
    init_face_recognition()
    log_activity(
        "face_deleted",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="face",
        target_name=person_name,
        source="camera",
    )
    payload = build_face_registry_snapshot()
    payload.update({"ok": True, "deleted": person_name})
    return jsonify(payload)


@app.route("/api/faces/retrain", methods=["POST"])
@login_required
def api_retrain_faces():
    init_face_recognition()
    log_activity(
        "face_retrained",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="face",
        target_name="known_faces",
        source="camera",
    )
    payload = build_face_registry_snapshot()
    payload["ok"] = True
    return jsonify(payload)


# ---------------------------------------------------------
# Profile APIs
# ---------------------------------------------------------
@app.route("/api/profile", methods=["GET"])
@login_required
def api_profile():
    profile = build_user_profile(session.get("user"))
    if profile is None:
        return jsonify({"error": "Profile not found"}), 404
    return jsonify({"profile": profile})


@app.route("/api/profile", methods=["POST"])
@login_required
def api_update_profile():
    payload = request.get_json(silent=True) or {}
    full_name = normalize_person_name(payload.get("full_name"))
    if not full_name:
        return jsonify({"error": "Full name is required"}), 400

    profile = update_user_profile_db(session.get("user"), payload)
    session["user_full_name"] = profile.get("full_name", "")
    log_activity(
        "profile_updated",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="profile",
        target_name=session.get("user"),
        source="profile",
        details={
            "full_name": full_name,
            "profile_visibility": profile.get("profile_visibility"),
            "activity_visibility": profile.get("activity_visibility"),
        },
    )
    return jsonify({"ok": True, "profile": build_user_profile(session.get("user"))})


@app.route("/api/profile/photo", methods=["POST"])
@login_required
def api_upload_profile_photo():
    profile, error_message = save_profile_photo(session.get("user"), request.files.get("photo"))
    if error_message:
        return jsonify({"error": error_message}), 400

    log_activity(
        "profile_photo_updated",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="profile",
        target_name=session.get("user"),
        source="profile",
    )
    return jsonify({"ok": True, "profile": profile})


@app.route("/api/profile/photo", methods=["DELETE"])
@login_required
def api_delete_profile_photo():
    profile, error_message = delete_profile_photo(session.get("user"))
    if error_message:
        return jsonify({"error": error_message}), 404

    log_activity(
        "profile_photo_deleted",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="profile",
        target_name=session.get("user"),
        source="profile",
    )
    return jsonify({"ok": True, "profile": profile})


@app.route("/api/profile", methods=["DELETE"])
@login_required
def api_delete_profile():
    actor_email = session.get("user")
    actor_role = current_user_role()
    deleted, error_message = delete_user_account_db(actor_email)
    if not deleted:
        return jsonify({"error": error_message or "Unable to delete profile"}), 400

    log_activity(
        "profile_deleted",
        actor_email=actor_email,
        actor_role=actor_role,
        target_type="profile",
        target_name=actor_email,
        source="profile",
    )
    session.clear()
    return jsonify({"ok": True, "deleted": actor_email, "redirect": auth_redirect_target()})


@app.route("/api/automation", methods=["GET"])
@login_required
def api_get_automation():
    return jsonify(evaluate_automation(source="automation-read"))


@app.route("/api/automation", methods=["POST"])
@login_required
def api_update_automation():
    payload = request.get_json(silent=True) or {}
    state = get_automation_state()
    previous_mode = state["mode"]

    if "mode" in payload:
        state["mode"] = normalize_choice(payload.get("mode"), VALID_CONTROL_MODES, state["mode"])

    environment_payload = payload.get("environment") or {}
    if isinstance(environment_payload, dict):
        environment = state["environment"]
        if "temperature_c" in environment_payload:
            environment["temperature_c"] = clamp_number(environment_payload.get("temperature_c"), environment["temperature_c"], 0.0, 60.0)
        if "ambient_light" in environment_payload:
            environment["ambient_light"] = clamp_number(environment_payload.get("ambient_light"), environment["ambient_light"], 0.0, 100.0)
        if "humidity" in environment_payload:
            environment["humidity"] = clamp_number(environment_payload.get("humidity"), environment["humidity"], 0.0, 100.0)
        if "security_risk" in environment_payload:
            environment["security_risk"] = normalize_choice(environment_payload.get("security_risk"), VALID_SECURITY_RISK, environment["security_risk"])

    threshold_payload = payload.get("thresholds") or {}
    if isinstance(threshold_payload, dict):
        thresholds = state["thresholds"]
        if "temperature_high_c" in threshold_payload:
            thresholds["temperature_high_c"] = clamp_number(threshold_payload.get("temperature_high_c"), thresholds["temperature_high_c"], 18.0, 45.0)
        if "temperature_low_c" in threshold_payload:
            thresholds["temperature_low_c"] = clamp_number(threshold_payload.get("temperature_low_c"), thresholds["temperature_low_c"], 10.0, 35.0)
        if "ambient_light_low" in threshold_payload:
            thresholds["ambient_light_low"] = clamp_number(threshold_payload.get("ambient_light_low"), thresholds["ambient_light_low"], 0.0, 100.0)
        if "ambient_light_high" in threshold_payload:
            thresholds["ambient_light_high"] = clamp_number(threshold_payload.get("ambient_light_high"), thresholds["ambient_light_high"], 0.0, 100.0)

    defense_payload = payload.get("defense") or {}
    if isinstance(defense_payload, dict):
        defense = state["defense"]
        if "armed" in defense_payload:
            defense["armed"] = coerce_bool_flag(defense_payload.get("armed"), defense["armed"])
        if "auto_alarm" in defense_payload:
            defense["auto_alarm"] = coerce_bool_flag(defense_payload.get("auto_alarm"), defense["auto_alarm"])
        if "auto_defense" in defense_payload:
            defense["auto_defense"] = coerce_bool_flag(defense_payload.get("auto_defense"), defense["auto_defense"])

    save_automation_state(state)
    snapshot = evaluate_automation(
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        source="automation-update",
    )
    log_activity(
        "automation_updated",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="automation",
        target_name=snapshot["mode"],
        source="automation",
        details={
            "mode_changed": previous_mode != snapshot["mode"],
            "mode": snapshot["mode"],
            "environment": snapshot["environment"],
            "thresholds": snapshot["thresholds"],
        },
    )
    return jsonify({"ok": True, **snapshot})


# ---------------------------------------------------------
# Admin APIs
# ---------------------------------------------------------
def build_admin_snapshot(activity_limit=25):
    users = list_users_db()
    devices = list_devices_db()
    activity = list_activity_logs(limit=activity_limit)
    active_devices = [item for item in devices if item["state"] == "ON"]

    return {
        "current_user": session.get("user"),
        "current_user_role": current_user_role(),
        "user_count": len(users),
        "admin_count": sum(1 for item in users if item["role"] == "admin"),
        "device_count": len(devices),
        "active_device_count": len(active_devices),
        "users": users,
        "activity": activity,
        "database_path": get_db_path(),
        "profile": build_user_profile(session.get("user")),
    }


@app.route("/api/admin/summary")
@admin_required
def admin_summary():
    return jsonify(build_admin_snapshot())


@app.route("/api/admin/users/<path:email>/role", methods=["POST"])
@admin_required
def admin_update_user_role(email):
    payload = request.get_json(silent=True) or {}
    normalized_email = normalize_email(email)
    normalized_role = str(payload.get("role", "") or "").strip().lower()
    target_user = get_user_by_email(normalized_email)

    if normalized_role not in VALID_USER_ROLES:
        return jsonify({"error": "Invalid role"}), 400
    if target_user is None:
        return jsonify({"error": "User not found"}), 404

    current_target_role = get_user_role(normalized_email)
    current_actor = normalize_email(session.get("user"))

    if current_target_role == "admin" and normalized_role != "admin" and count_admin_users() <= 1:
        return jsonify({"error": "At least one admin account must remain."}), 400
    if current_actor == normalized_email and normalized_role != "admin":
        return jsonify({"error": "Create another admin before removing your own admin access."}), 400

    update_user_role_db(normalized_email, normalized_role)
    if current_actor == normalized_email:
        session["user_role"] = normalized_role

    log_activity(
        "user_role_updated",
        actor_email=current_actor,
        actor_role=current_user_role(),
        target_type="user",
        target_name=normalized_email,
        source="admin",
        details={"role": normalized_role},
    )
    return jsonify({"ok": True, "user": serialize_user_row(get_user_by_email(normalized_email))})


# ---------------------------------------------------------
# Enterprise monitoring APIs (incidents, notifications, analytics)
# ---------------------------------------------------------
@app.route("/api/incidents", methods=["GET"])
@login_required
def api_list_incidents():
    status = request.args.get("status")
    severity = request.args.get("severity")
    limit = request.args.get("limit", 50)
    items = list_incidents(limit=limit, status=status, severity=severity)
    return jsonify({"items": items, "total": len(items)})


@app.route("/api/incidents", methods=["POST"])
@login_required
def api_create_incident():
    payload = request.get_json(silent=True) or {}
    title = payload.get("title") or payload.get("message") or payload.get("name") or ""
    severity = payload.get("severity") or payload.get("level") or "warning"
    camera_id = payload.get("camera_id") or payload.get("camera") or ""
    tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    risk_level = details.get("risk_level") or payload.get("risk_level")
    if risk_level:
        details["risk_level"] = normalize_choice(risk_level, VALID_SECURITY_RISK, "normal")
        details["risk_score"] = compute_risk_score(details["risk_level"], details.get("risk_reasons"))

    incident = create_incident(
        title,
        severity=severity,
        status="open",
        source="manual",
        camera_id=camera_id,
        tags=tags,
        details=details,
        actor_email=session.get("user"),
    )
    return jsonify({"ok": True, "incident": incident})


@app.route("/api/incidents/<int:incident_id>/status", methods=["POST"])
@login_required
def api_update_incident_status(incident_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status") or payload.get("state") or ""
    updated = update_incident_status(incident_id, status, actor_email=session.get("user"))
    if updated is None:
        return jsonify({"error": "incident not found"}), 404
    return jsonify({"ok": True, "incident": updated})


@app.route("/api/notifications", methods=["GET"])
@login_required
def api_list_notifications():
    unread_only = str(request.args.get("unread") or "").strip() in {"1", "true", "yes", "on"}
    limit = request.args.get("limit", 40)
    items = list_notifications(session.get("user"), limit=limit, unread_only=unread_only)
    unread_count = sum(1 for item in items if not item.get("read_at"))
    return jsonify({"items": items, "unread": unread_count, "total": len(items)})


@app.route("/api/notifications/<int:notification_id>/read", methods=["POST"])
@login_required
def api_mark_notification_read(notification_id):
    ok = mark_notification_read(notification_id, actor_email=session.get("user"))
    return jsonify({"ok": bool(ok), "id": int(notification_id)})


@app.route("/api/event-logs", methods=["GET"])
@admin_required
def api_event_logs():
    limit = request.args.get("limit", 80)
    query = str(request.args.get("q") or "").strip().lower()
    items = list_activity_logs(limit=limit)
    if query:
        filtered = []
        for item in items:
            haystack = " ".join(
                [
                    str(item.get("actor_email") or ""),
                    str(item.get("actor_role") or ""),
                    str(item.get("action") or ""),
                    str(item.get("target_type") or ""),
                    str(item.get("target_name") or ""),
                    str(item.get("source") or ""),
                    json.dumps(item.get("details") or {}, ensure_ascii=True),
                ]
            ).lower()
            if query in haystack:
                filtered.append(item)
        items = filtered
    return jsonify({"items": items, "total": len(items)})


@app.route("/api/analytics/summary", methods=["GET"])
@login_required
def api_analytics_summary():
    incident_items = list_incidents(limit=200)
    status_counts = Counter((item or {}).get("status", "open") for item in incident_items if item)
    severity_counts = Counter((item or {}).get("severity", "warning") for item in incident_items if item)
    open_incidents = [item for item in incident_items if item and item.get("status") == "open"]

    camera_risk, risk_reasons = build_camera_risk_snapshot()
    risk_score = compute_risk_score(camera_risk, risk_reasons)

    devices = list_devices_db()
    users = list_users_db()
    active_devices = [item for item in devices if item.get("state") == "ON"]

    person_count = sum(
        int(item.get("count", 0) or 0)
        for item in latest_detections
        if str(item.get("label") or "").strip().lower() == "person"
    )

    crowd_density = "low"
    if person_count >= 8:
        crowd_density = "high"
    elif person_count >= 3:
        crowd_density = "medium"

    return jsonify(
        {
            "time": now_iso(),
            "risk": {
                "level": camera_risk,
                "score": risk_score,
                "reasons": risk_reasons[:6],
                "recommendations": build_ai_recommendations(camera_risk, risk_reasons),
            },
            "incidents": {
                "total": len(incident_items),
                "open": status_counts.get("open", 0),
                "acknowledged": status_counts.get("acknowledged", 0),
                "resolved": status_counts.get("resolved", 0),
                "severity": {
                    "info": severity_counts.get("info", 0),
                    "warning": severity_counts.get("warning", 0),
                    "error": severity_counts.get("error", 0),
                    "critical": severity_counts.get("critical", 0),
                },
                "latest_open": open_incidents[:6],
            },
            "operations": {
                "users": len(users),
                "admins": sum(1 for item in users if item.get("role") == "admin"),
                "devices": len(devices),
                "devices_active": len(active_devices),
                "camera_count": len(list_camera_profiles()),
            },
            "vision": {
                "objects": latest_detections,
                "faces": latest_faces,
                "crowd_density": crowd_density,
                "person_count": person_count,
            },
        }
    )


def tokenize_discover_query(query):
    tokens = re.findall(r"[a-zA-Z0-9_+-]+", str(query or "").lower())
    stop_words = {"show", "find", "the", "and", "or", "at", "in", "on", "for", "me", "all", "video", "videos", "camera"}
    return [token for token in tokens if token not in stop_words][:12]


def text_matches_tokens(text, tokens):
    haystack = str(text or "").lower()
    if not tokens:
        return True
    return all(token in haystack for token in tokens)


def build_discover_results(query, limit=12):
    tokens = tokenize_discover_query(query)
    limit_value = bounded_int(limit, 12, maximum=50)
    results = []

    for incident in list_incidents(limit=160):
        if not incident:
            continue
        haystack = " ".join(
            [
                str(incident.get("title") or ""),
                str(incident.get("severity") or ""),
                str(incident.get("status") or ""),
                str(incident.get("source") or ""),
                json.dumps(incident.get("tags") or [], ensure_ascii=True),
                json.dumps(incident.get("details") or {}, ensure_ascii=True),
            ]
        )
        if not text_matches_tokens(haystack, tokens):
            continue
        results.append(
            {
                "type": "incident",
                "title": incident.get("title"),
                "subtitle": f"{incident.get('severity')} • {incident.get('status')}",
                "time": incident.get("created_at"),
                "camera_id": incident.get("camera_id"),
                "severity": incident.get("severity"),
                "snapshot_url": incident.get("snapshot_url"),
                "source_id": incident.get("id"),
            }
        )

    for event in (get_access_control_state().get("events") or [])[:120]:
        haystack = json.dumps(event, ensure_ascii=True)
        if not text_matches_tokens(haystack, tokens):
            continue
        results.append(
            {
                "type": "access",
                "title": f"{event.get('event_type', 'access').replace('_', ' ').title()} at {event.get('door_name')}",
                "subtitle": event.get("actor") or "Access event",
                "time": event.get("created_at"),
                "camera_id": event.get("camera_id"),
                "severity": event.get("severity", "info"),
                "source_id": event.get("id"),
            }
        )

    for telemetry in list(vision_telemetry)[:180]:
        objects = telemetry.get("objects") or []
        labels = ", ".join(str(item.get("label") or "") for item in objects)
        haystack = f"{telemetry.get('camera_name')} {labels}"
        if not text_matches_tokens(haystack, tokens):
            continue
        results.append(
            {
                "type": "vision",
                "title": labels or "Vision telemetry",
                "subtitle": telemetry.get("camera_name") or "Camera",
                "time": telemetry.get("time"),
                "camera_id": telemetry.get("camera_id"),
                "severity": "info",
                "object_count": len(objects),
            }
        )

    for camera in list_camera_profiles():
        haystack = json.dumps(camera, ensure_ascii=True)
        if not text_matches_tokens(haystack, tokens):
            continue
        results.append(
            {
                "type": "camera",
                "title": camera.get("name"),
                "subtitle": camera.get("label") or camera.get("transport"),
                "time": now_iso(),
                "camera_id": camera.get("id"),
                "severity": "info",
            }
        )

    severity_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    results.sort(key=lambda item: (severity_order.get(item.get("severity"), 4), str(item.get("time") or "")), reverse=False)
    clipped = results[:limit_value]
    return {
        "query": str(query or "").strip(),
        "tokens": tokens,
        "total": len(results),
        "items": clipped,
        "summary": f"{len(results)} matching security records found.",
        "recommendations": [
            "Open matching incidents first when severity is warning or critical.",
            "Use journey tracking for people, vehicles or unknown activity across cameras.",
            "Link access events with camera snapshots before closing a case.",
        ][:3],
    }


def build_journey_snapshot(target, limit=20):
    target_value = str(target or "person").strip().lower()
    rows = []
    for telemetry in list(vision_telemetry)[:bounded_int(limit, 20, maximum=120)]:
        matches = []
        for item in telemetry.get("objects") or []:
            label = str(item.get("label") or "").strip().lower()
            if target_value in label or label in target_value:
                matches.append(item)
        if not matches:
            continue
        rows.append(
            {
                "time": telemetry.get("time"),
                "camera_id": telemetry.get("camera_id"),
                "camera_name": telemetry.get("camera_name"),
                "matches": matches[:12],
            }
        )

    if not rows and target_value in {"face", "unknown", "visitor", "person"}:
        for face in latest_faces:
            rows.append(
                {
                    "time": now_iso(),
                    "camera_id": get_active_camera_profile(refresh=False).get("id"),
                    "camera_name": get_active_camera_profile(refresh=False).get("name"),
                    "matches": [face],
                }
            )

    return {
        "target": target_value,
        "total": len(rows),
        "path": rows,
        "summary": f"{len(rows)} timeline point(s) found for {target_value}.",
    }


def build_video_agent_snapshot(prefer_hindi=False):
    active_camera = get_active_camera_profile(refresh=False)
    detection_summary = format_detection_summary(build_detection_counter(latest_detections))
    object_count = sum(int(item.get("count", 1) or 1) for item in latest_detections)
    recognized_faces = [item for item in latest_faces if item.get("recognized")]
    unknown_count = sum(int(item.get("count", 0) or 0) for item in latest_faces if not item.get("recognized"))
    camera_risk, risk_reasons = build_camera_risk_snapshot()
    risk_score = compute_risk_score(camera_risk, risk_reasons)

    behavior_label = str(human_behavior.get("last_activity") or "idle")
    if unknown_count:
        behavior_label = "unknown_visitor"
    elif human_behavior.get("person_present"):
        behavior_label = "person_present"
    elif object_count:
        behavior_label = "activity_detected"

    if prefer_hindi:
        if unknown_count:
            scene_text = f"Camera me {unknown_count} unknown visitor dikh raha hai."
        elif recognized_faces:
            scene_text = f"Known face visible hai: {format_face_summary(recognized_faces)}."
        elif detection_summary:
            scene_text = f"Camera me objects detect hue: {detection_summary}."
        else:
            scene_text = "Camera scene abhi clear hai. Koi major activity detect nahi hui."

        behavior_text = (
            "Person camera frame me present hai."
            if human_behavior.get("person_present")
            else "Human movement abhi active nahi hai."
        )
        speech_text = f"{scene_text} {behavior_text} Risk {camera_risk} hai."
    else:
        if unknown_count:
            scene_text = f"I can see {unknown_count} unknown visitor on the active camera."
        elif recognized_faces:
            scene_text = f"I can see a known face: {format_face_summary(recognized_faces)}."
        elif detection_summary:
            scene_text = f"I can see these objects: {detection_summary}."
        else:
            scene_text = "The camera scene looks clear. No major activity is visible."

        behavior_text = (
            "A person is currently present in the frame."
            if human_behavior.get("person_present")
            else "No active human movement is visible right now."
        )
        speech_text = f"{scene_text} {behavior_text} Current risk is {camera_risk}."

    should_speak = camera_risk in {"suspicious", "high", "critical"} or bool(unknown_count)
    if behavior_label in {"person_present", "activity_detected"}:
        should_speak = True

    return {
        "time": now_iso(),
        "camera": active_camera,
        "camera_status": camera_state["message"],
        "camera_available": camera_state["available"],
        "scene_text": scene_text,
        "behavior_text": behavior_text,
        "speech_text": speech_text,
        "behavior_label": behavior_label,
        "should_speak": should_speak,
        "risk": {
            "level": camera_risk,
            "score": risk_score,
            "reasons": risk_reasons[:6],
            "recommendations": build_ai_recommendations(camera_risk, risk_reasons),
        },
        "detections": latest_detections,
        "faces": latest_faces,
        "human_behavior": dict(human_behavior),
    }


@app.route("/api/video-agent/status", methods=["GET"])
@login_or_local_voice_required
def api_video_agent_status():
    lang = str(request.args.get("lang") or "").strip().lower()
    prefer_hindi = lang.startswith("hi") or lang in {"hinglish", "hindi"}
    return jsonify(build_video_agent_snapshot(prefer_hindi=prefer_hindi))


@app.route("/api/discover", methods=["GET", "POST"])
@login_required
def api_discover():
    payload = request.get_json(silent=True) or {}
    query = request.args.get("q") or payload.get("q") or payload.get("query") or ""
    limit = request.args.get("limit") or payload.get("limit") or 12
    return jsonify(build_discover_results(query, limit=limit))


@app.route("/api/journeys", methods=["GET"])
@login_required
def api_journeys():
    target = request.args.get("target") or request.args.get("q") or "person"
    limit = request.args.get("limit", 20)
    return jsonify(build_journey_snapshot(target, limit=limit))


@app.route("/api/access-control", methods=["GET"])
@login_required
def api_access_control():
    state = get_access_control_state()
    return jsonify(
        {
            "doors": state["doors"],
            "events": state["events"][:20],
            "lockdown": state["lockdown"],
            "updated_at": state["updated_at"],
            "total_events": len(state["events"]),
        }
    )


@app.route("/api/access-control/events", methods=["POST"])
@login_required
def api_access_control_event():
    payload = request.get_json(silent=True) or {}
    event, state = record_access_event(
        payload,
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        source="access",
    )
    return jsonify({"ok": True, "event": event, "events": state["events"][:20], "lockdown": state["lockdown"]})


@app.route("/api/incidents/<int:incident_id>/report", methods=["GET"])
@login_required
def api_incident_report(incident_id):
    incident = get_incident(incident_id)
    if incident is None:
        return jsonify({"error": "incident not found"}), 404
    risk_level = (incident.get("details") or {}).get("risk_level") or incident.get("severity") or "warning"
    reasons = (incident.get("details") or {}).get("risk_reasons") or [incident.get("title")]
    return jsonify(
        {
            "incident": incident,
            "report": {
                "title": f"Incident report #{incident_id}",
                "summary": incident.get("title"),
                "severity": incident.get("severity"),
                "status": incident.get("status"),
                "evidence": {
                    "snapshot_url": incident.get("snapshot_url"),
                    "camera_id": incident.get("camera_id"),
                    "created_at": incident.get("created_at"),
                },
                "recommendations": build_ai_recommendations(risk_level, reasons),
                "generated_at": now_iso(),
            },
        }
    )


@app.route("/api/telemetry/vision", methods=["GET"])
@login_required
def api_vision_telemetry():
    limit = request.args.get("limit", 60)
    items = list(vision_telemetry)[:bounded_int(limit, 60, maximum=500)]
    camera_risk, risk_reasons = build_camera_risk_snapshot()
    return jsonify(
        {
            "time": now_iso(),
            "risk_level": camera_risk,
            "risk_score": compute_risk_score(camera_risk, risk_reasons),
            "risk_reasons": risk_reasons[:6],
            "objects": latest_detection_objects,
            "telemetry": items,
        }
    )


@app.route("/api/search", methods=["GET"])
@login_required
def api_smart_search():
    query = str(request.args.get("q") or "").strip()
    if not query:
        return jsonify({"q": "", "results": {}, "total": 0})
    needle = query.lower()

    incidents = [item for item in list_incidents(limit=120) if item and needle in str(item.get("title") or "").lower()]
    notifications = [item for item in list_notifications(session.get("user"), limit=80) if needle in str(item.get("message") or "").lower()]
    activity = []
    if current_user_role() == "admin":
        activity = [
            item
            for item in list_activity_logs(limit=120)
            if needle
            in " ".join(
                [
                    str(item.get("actor_email") or ""),
                    str(item.get("action") or ""),
                    str(item.get("target_name") or ""),
                    str(item.get("source") or ""),
                ]
            ).lower()
        ]
    devices = [item for item in list_devices_db() if needle in str(item.get("name") or "").lower()]
    cameras = [item for item in list_camera_profiles() if needle in str(item.get("name") or "").lower()]
    faces = [item for item in list_known_face_people() if needle in str(item.get("name") or "").lower()]

    results = {
        "incidents": incidents[:12],
        "notifications": notifications[:12],
        "activity": activity[:12],
        "devices": devices[:12],
        "cameras": cameras[:12],
        "faces": faces[:12],
    }
    total = sum(len(value) for value in results.values())
    return jsonify({"q": query, "results": results, "total": total})


@app.route("/api/security/status", methods=["GET"])
@login_required
def api_security_status():
    security = get_user_security_settings_db(session.get("user")) or {}
    return jsonify(
        {
            "current_user": session.get("user"),
            "two_factor_available": bool(SECURITY_ENABLE_2FA),
            "two_factor_enabled": bool(security.get("two_factor_enabled")),
            "two_factor_method": security.get("two_factor_method") or "",
            "session_issued_at": session.get("issued_at"),
            "session_revoked_at": security.get("session_revoked_at"),
            "last_login_ip": security.get("last_login_ip") or "",
            "last_login_user_agent": security.get("last_login_user_agent") or "",
            "login_audit": list_login_audit(limit=18, email=session.get("user")),
        }
    )


@app.route("/api/security/2fa", methods=["POST"])
@login_required
def api_set_two_factor():
    if not SECURITY_ENABLE_2FA:
        return jsonify({"error": "2FA is disabled by configuration"}), 400
    payload = request.get_json(silent=True) or {}
    enabled = coerce_bool_flag(payload.get("enabled"), False)
    method = str(payload.get("method") or "email").strip().lower() or "email"
    set_two_factor_state_db(session.get("user"), enabled=enabled, method=method)
    log_activity(
        "security_2fa_updated",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="security",
        target_name="2fa",
        source="security",
        details={"enabled": bool(enabled), "method": method},
    )
    security = get_user_security_settings_db(session.get("user")) or {}
    return jsonify({"ok": True, **security})


@app.route("/api/security/sessions/revoke", methods=["POST"])
@login_required
def api_revoke_sessions():
    revoked_at = revoke_sessions_db(session.get("user"))
    log_activity(
        "security_sessions_revoked",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="security",
        target_name="sessions",
        source="security",
        details={"revoked_at": revoked_at},
    )
    session.clear()
    return jsonify({"ok": True, "revoked_at": revoked_at, "redirect": auth_redirect_target()})


@app.route("/api/enterprise/snapshot", methods=["GET"])
@login_required
def api_enterprise_snapshot():
    """Single command-center snapshot for the enterprise dashboard UI."""
    cameras = list_camera_profiles()
    devices = list_devices_db()
    incidents = list_incidents(limit=80)
    notifications = list_notifications(session.get("user"), limit=30)
    profile = build_user_profile(session.get("user")) or {}
    users = list_users_db() if current_user_role() == "admin" else [profile]
    active_devices = [item for item in devices if item.get("state") == "ON"]
    open_incidents = [item for item in incidents if item.get("status") == "open"]
    camera_risk, risk_reasons = build_camera_risk_snapshot()
    risk_score = compute_risk_score(camera_risk, risk_reasons)

    known_faces = []
    try:
        known_faces = list_known_face_people()
    except Exception:
        known_faces = []

    seed_faces = [
        {"name": "Unknown Visitor", "group": "Unknown Faces", "last_seen": "2 min ago", "camera": "Main Entry", "status": "review"},
        {"name": "Watchlist Person", "group": "Watchlist", "last_seen": "8 min ago", "camera": "Lobby", "status": "watchlist"},
        {"name": "Blacklisted Subject", "group": "Blacklist", "last_seen": "18 min ago", "camera": "Perimeter", "status": "blacklist"},
        {"name": "Visitor Pass 104", "group": "Visitors", "last_seen": "31 min ago", "camera": "Reception", "status": "visitor"},
    ]
    face_cards = [
        {
            "name": item.get("name") if isinstance(item, dict) else str(item),
            "group": "Known Faces",
            "last_seen": "Today",
            "camera": "Active camera",
            "status": "known",
        }
        for item in known_faces[:8]
    ] + seed_faces

    alert_items = []
    for item in notifications[:8]:
        alert_items.append({
            "title": item.get("message") or "Security notification",
            "detail": item.get("created_at") or "Recent",
            "level": item.get("level") or "info",
        })
    for incident in open_incidents[:6]:
        alert_items.append({
            "title": incident.get("title") or "Open incident",
            "detail": incident.get("created_at") or "Open case",
            "level": incident.get("severity") or "warning",
        })
    if not alert_items:
        alert_items.append({"title": "All monitored sites operational", "detail": now_string(), "level": "info"})

    trend_seed = max(12, int(risk_score or 0))
    threat_trend = [min(95, max(8, trend_seed + offset)) for offset in [-12, -4, 6, 14, 3, 21, 10]]

    return jsonify({
        "generated_at": db_now_iso(),
        "site_profile": {
            "name": "Smart Surveillance System AI Powered",
            "deployment_targets": [
                "Factories", "Hospitals", "Schools", "Colleges", "Airports", "Railways",
                "Government Facilities", "Smart Cities", "Corporate Offices",
            ],
        },
        "security": {
            "rbac": True,
            "two_factor": bool(profile.get("two_factor_enabled")),
            "session_management": True,
            "audit_logs": current_user_role() == "admin",
            "api_authentication": True,
            "encryption": "session-cookie and HTTPS ready",
            "ip_tracking": True,
            "login_monitoring": True,
        },
        "ai": {
            "health": "Operational" if YOLO_ENABLED or FACE_RECOGNITION_ENABLED else "Simulation ready",
            "yolo": YOLO_STATUS_MESSAGE,
            "face_recognition": face_state["message"],
            "activity": [
                {"name": "YOLO Object Detection", "count": len(latest_detections), "confidence": 94},
                {"name": "Face Recognition", "count": len(face_cards), "confidence": 91},
                {"name": "Motion Detection", "count": 18, "confidence": 96},
                {"name": "Intrusion Detection", "count": len(open_incidents), "confidence": 89},
                {"name": "Fire Detection", "count": 0, "confidence": 98},
                {"name": "Weapon Detection", "count": 0, "confidence": 97},
                {"name": "Crowd Density", "count": 3, "confidence": 92},
                {"name": "Vehicle Detection", "count": 7, "confidence": 95},
            ],
        },
        "operations": {
            "cameras_online": sum(1 for item in cameras if item.get("enabled") is not False),
            "cameras_offline": sum(1 for item in cameras if item.get("enabled") is False),
            "devices_online": len(active_devices),
            "open_incidents": len(open_incidents),
            "active_alerts": len(alert_items),
            "threat_level": camera_risk,
            "threat_score": risk_score,
            "risk_reasons": risk_reasons,
        },
        "alerts": alert_items,
        "faces": face_cards,
        "users": users,
        "automation_rules": [
            {"condition": "Fire Detected", "action": "Activate Alarm", "enabled": True},
            {"condition": "Intrusion Detected", "action": "Lock Doors", "enabled": True},
            {"condition": "Unknown Face", "action": "Notify Security", "enabled": True},
            {"condition": "High Threat", "action": "Enable Defense Mode", "enabled": True},
        ],
        "supported_devices": ["Lights", "Sirens", "Alarms", "Door Locks", "Sensors", "Relays", "Smart Switches"],
        "trends": {
            "threat": threat_trend,
            "incident": [len(open_incidents), len(incidents), max(1, len(cameras)), len(active_devices)],
        },
    })


@app.route("/api/enterprise/action", methods=["POST"])
@login_required
def api_enterprise_action():
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    if action == "defense_mode":
        state = get_automation_state()
        state["mode"] = "sentinel"
        state["defense"]["armed"] = True
        save_automation_state(state)
        automation = evaluate_automation(
            actor_email=session.get("user"),
            actor_role=current_user_role(),
            source="enterprise-action",
        )
        log_system_alert(
            "Defense mode enabled from command center",
            level="critical",
            details={"automation_mode": automation.get("mode")},
        )
        create_incident(
            "Defense mode enabled",
            severity="critical",
            status="open",
            source="automation",
            tags=["defense", "automation"],
            details={
                "ai_summary": "Sentinel defense mode was activated by an operator.",
                "suggested_actions": ["Verify perimeter cameras", "Confirm door lock state"],
            },
            actor_email=session.get("user"),
        )
        return jsonify({"ok": True, "action": action, "automation": automation})
    return jsonify({"error": "unsupported enterprise action"}), 400


@app.route("/api/reports/incidents.csv", methods=["GET"])
@login_required
def api_export_incidents_csv():
    import csv
    from io import StringIO

    status = request.args.get("status")
    severity = request.args.get("severity")
    items = list_incidents(limit=1000, status=status, severity=severity)

    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "id",
            "title",
            "severity",
            "status",
            "source",
            "camera_id",
            "risk_score",
            "snapshot_url",
            "created_at",
            "updated_at",
        ]
    )
    for item in items:
        if not item:
            continue
        writer.writerow(
            [
                item.get("id"),
                item.get("title"),
                item.get("severity"),
                item.get("status"),
                item.get("source"),
                item.get("camera_id"),
                item.get("risk_score"),
                item.get("snapshot_url"),
                item.get("created_at"),
                item.get("updated_at"),
            ]
        )

    response = Response(buffer.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = "attachment; filename=incidents.csv"
    return response


@app.route("/api/reports/event-logs.csv", methods=["GET"])
@admin_required
def api_export_event_logs_csv():
    import csv
    from io import StringIO

    items = list_activity_logs(limit=1000)
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "id",
            "created_at",
            "actor_email",
            "actor_role",
            "action",
            "target_type",
            "target_name",
            "source",
            "details",
        ]
    )
    for item in items:
        writer.writerow(
            [
                item.get("id"),
                item.get("created_at"),
                item.get("actor_email"),
                item.get("actor_role"),
                item.get("action"),
                item.get("target_type"),
                item.get("target_name"),
                item.get("source"),
                json.dumps(item.get("details") or {}, ensure_ascii=True),
            ]
        )

    response = Response(buffer.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = "attachment; filename=event-logs.csv"
    return response


@app.route("/api/backup/system.zip", methods=["GET"])
@admin_required
def api_system_backup():
    import zipfile
    from io import BytesIO

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        db_path = get_db_path()
        if db_path and os.path.exists(db_path):
            bundle.write(db_path, arcname="db/app.db")
        if CAMERA_CONFIG_PATH and os.path.exists(CAMERA_CONFIG_PATH):
            bundle.write(CAMERA_CONFIG_PATH, arcname="config/cameras.json")
        bundle.writestr("meta/created_at.txt", now_iso())
        bundle.writestr("meta/readme.txt", "SmartAI backup bundle (db + config).")

    archive.seek(0)
    response = Response(archive.read(), mimetype="application/zip")
    response.headers["Content-Disposition"] = "attachment; filename=smartai-backup.zip"
    return response

# ---------------------------------------------------------
# Gemini Voice assistant
# ---------------------------------------------------------
def ask_gemini(prompt: str, prefer_hindi=False) -> str:
    if not GEMINI_API_KEY or not GEMINI_AVAILABLE or not GEMINI_CLIENT:
        if prefer_hindi:
            return "Gemini AI configure nahi hai. Kripya .env mein GEMINI_API_KEY add kijiye."
        return "Gemini AI not configured. Add GEMINI_API_KEY in .env."

    try:
        response = GEMINI_CLIENT.models.generate_content(
            model=GEMINI_MODEL_NAME,
            contents=prompt,
        )
        text = getattr(response, "text", None)
        if not text:
            if prefer_hindi:
                return "Gemini se koi jawab nahi mila."
            return "No response from Gemini."
        return text
    except Exception as e:
        if prefer_hindi:
            return f"Gemini error aaya: {e}"
        return f"Gemini Error: {e}"


def resolve_assistant_mode(payload):
    requested_mode = str(payload.get("mode", "") or "hybrid").strip().lower()
    if requested_mode in VALID_ASSISTANT_MODES:
        return requested_mode
    return "hybrid"


def build_local_health_summary(prefer_hindi=False):
    cpu = round(psutil.cpu_percent(interval=0.1), 2)
    memory = round(psutil.virtual_memory().percent, 2)
    disk = round(psutil.disk_usage("/").percent, 2)

    if prefer_hindi:
        return (
            f"System summary: CPU {cpu}%, memory {memory}%, disk {disk}%, "
            f"uptime {get_uptime()} aur processes {safe_psutil_pids_count()} hain."
        )
    return (
        f"System summary: CPU {cpu}%, memory {memory}%, disk {disk}%, "
        f"uptime {get_uptime()}, and {safe_psutil_pids_count()} running processes."
    )


def build_local_device_summary(prefer_hindi=False):
    devices = list_devices_db()
    if not devices:
        return "Koi device configured nahi hai." if prefer_hindi else "No devices are configured."

    summary = ", ".join(f"{item['name']} {item['state']}" for item in devices)
    if prefer_hindi:
        return f"Devices ka status: {summary}."
    return f"Device status: {summary}."


def build_local_alert_summary(prefer_hindi=False):
    latest_message = latest_camera_alert_payload.get("message") or latest_camera_alert_payload.get("alert") or "No alerts"
    if prefer_hindi:
        return f"Latest camera alert: {latest_message}. Camera status: {camera_state['message']}."
    return f"Latest camera alert: {latest_message}. Camera status: {camera_state['message']}."


def build_detection_counter(items, field_name="label"):
    counts = Counter()
    for item in items or []:
        label = str(item.get(field_name, "") or "").strip()
        if not label:
            continue
        counts[label] += int(item.get("count", 1) or 1)
    return counts


def build_local_automation_summary(prefer_hindi=False):
    snapshot = build_automation_snapshot()
    environment = snapshot["environment"]
    active_devices = ", ".join(snapshot["active_devices"]) if snapshot["active_devices"] else "none"
    if prefer_hindi:
        return (
            f"Current mode {snapshot['mode_label']} hai. Risk {snapshot['runtime_risk']} hai. "
            f"Temperature {environment['temperature_c']:.1f}C, ambient light {environment['ambient_light']:.0f}%, "
            f"aur active devices {active_devices} hain."
        )
    return (
        f"Current mode is {snapshot['mode_label']}. Risk is {snapshot['runtime_risk']}. "
        f"Temperature is {environment['temperature_c']:.1f}C, ambient light is {environment['ambient_light']:.0f}%, "
        f"and active devices are {active_devices}."
    )


def build_local_face_summary(prefer_hindi=False):
    if not FACE_RECOGNITION_ENABLED:
        if prefer_hindi:
            return f"Face recognition abhi ready nahi hai. Status: {face_state['message']}."
        return f"Face recognition is not ready right now. Status: {face_state['message']}."

    recognized_faces = [item for item in latest_faces if item.get("recognized")]
    unknown_count = sum(int(item.get("count", 0) or 0) for item in latest_faces if not item.get("recognized"))

    if not recognized_faces and unknown_count == 0:
        if prefer_hindi:
            return f"Face recognition active hai. Filhal camera me koi face detect nahi hua. Known roster: {face_state['known_people']}."
        return f"Face recognition is active. No faces are currently visible. Known roster size: {face_state['known_people']}."

    details = []
    if recognized_faces:
        recognized_text = format_face_summary(recognized_faces)
        details.append(
            f"known faces: {recognized_text}" if not prefer_hindi else f"known faces: {recognized_text}"
        )
    if unknown_count:
        details.append(
            f"{unknown_count} unknown visitor(s)" if not prefer_hindi else f"{unknown_count} unknown visitor(s)"
        )

    if prefer_hindi:
        return f"Face watch: {', '.join(details)}."
    return f"Face watch: {', '.join(details)}."


def build_local_camera_summary(prefer_hindi=False):
    active_camera = get_active_camera_profile()
    detection_summary = format_detection_summary(build_detection_counter(latest_detections))
    face_summary = format_face_summary([item for item in latest_faces if item.get("recognized")])
    unknown_count = sum(int(item.get("count", 0) or 0) for item in latest_faces if not item.get("recognized"))

    parts = [
        f"Active camera {active_camera['name']}",
        f"status {camera_state['message']}",
        f"latest alert {latest_camera_alert_payload.get('message') or 'No alerts'}",
    ]
    if detection_summary:
        parts.append(f"objects {detection_summary}")
    if face_summary:
        parts.append(f"recognized faces {face_summary}")
    if unknown_count:
        parts.append(f"unknown visitors {unknown_count}")

    sentence = ". ".join(parts) + "."
    if prefer_hindi:
        return sentence
    return sentence


def build_local_profile_summary(actor_email, prefer_hindi=False):
    profile = build_user_profile(actor_email)
    if profile is None:
        return "Profile is unavailable." if not prefer_hindi else "Profile unavailable hai."

    if prefer_hindi:
        return (
            f"Aap {profile['display_name']} hain. Role {profile['role']} hai. "
            f"Activity count {profile['activity_count']} aur devices {profile['device_count']} configured hain."
        )
    return (
        f"You are {profile['display_name']} with the {profile['role']} role. "
        f"Activity count is {profile['activity_count']} and {profile['device_count']} devices are configured."
    )


def build_local_admin_summary(prefer_hindi=False):
    active_devices = sum(1 for item in list_devices_db() if item["state"] == "ON")
    if prefer_hindi:
        return (
            f"Admin snapshot: users {count_users()}, admins {count_admin_users()}, "
            f"devices {len(list_devices_db())}, active devices {active_devices}."
        )
    return (
        f"Admin snapshot: {count_users()} users, {count_admin_users()} admins, "
        f"{len(list_devices_db())} devices, and {active_devices} active devices."
    )


def build_assistant_capability_summary(prefer_hindi=False):
    if prefer_hindi:
        return (
            "Main health, camera alerts, known/unknown faces, device control, "
            "profile summary, admin snapshot, camera switching, aur self monitoring status me help kar sakta hoon."
        )
    return (
        "I can help with health, camera alerts, known versus unknown faces, device control, "
        "profile summaries, admin status, camera switching, and self monitoring status."
    )


def is_gemini_ready():
    return bool(GEMINI_API_KEY and GEMINI_AVAILABLE)


def is_gemini_error_reply(reply):
    normalized = str(reply or "").strip().lower()
    return (
        normalized.startswith("gemini error")
        or normalized.startswith("gemini error aaya")
        or normalized == "no response from gemini."
        or normalized == "gemini se koi jawab nahi mila."
    )


def build_research_mode_reply(query, prefer_hindi=False):
    devices = list_devices_db()
    active_devices = [item["name"] for item in devices if item["state"] == "ON"]
    active_text = ", ".join(active_devices) if active_devices else ("koi bhi device on nahi hai" if prefer_hindi else "no devices are currently on")

    if prefer_hindi:
        return (
            f"Research mode summary for '{query}': {build_local_health_summary(True)} "
            f"{build_local_camera_summary(True)} Active devices: {active_text}. "
            f"Users {count_users()} aur admins {count_admin_users()} configured hain."
        )
    return (
        f"Research mode summary for '{query}': {build_local_health_summary(False)} "
        f"{build_local_camera_summary(False)} Active devices: {active_text}. "
        f"There are {count_users()} users and {count_admin_users()} admin accounts configured."
    )


def find_camera_profile_for_query(query_lower):
    active_profiles = list_camera_profiles()
    for index, profile in enumerate(active_profiles, start=1):
        profile_name = profile["name"].lower()
        if profile_name and profile_name in query_lower:
            return profile

        profile_id = profile["id"].lower()
        if profile_id.isdigit() and f"camera {profile_id}" in query_lower:
            return profile
        if f"camera {index}" in query_lower:
            return profile
    return None


def build_non_gemini_assistant_reply(query, mode, prefer_hindi=False, actor_email=None):
    query_lower = str(query or "").strip().lower()

    if any(token in query_lower for token in ("hello", "hi", "hey", "namaste", "hello smartai")):
        if prefer_hindi:
            return (
                "Namaste. Gemini AI configure nahi hai, lekin SmartAI local control mode me active hai. "
                f"{build_assistant_capability_summary(True)}"
            )
        return (
            "Hello. Gemini AI not configured, but SmartAI is active in local control mode. "
            f"{build_assistant_capability_summary(False)}"
        )

    if any(token in query_lower for token in ("help", "kya kar sakte", "what can you do", "capabilities")):
        capability_text = build_assistant_capability_summary(prefer_hindi)
        if prefer_hindi:
            return f"Gemini AI configure nahi hai. {capability_text}"
        return f"Gemini AI not configured. {capability_text}"

    summary_parts = [
        build_local_health_summary(prefer_hindi),
        build_local_camera_summary(prefer_hindi),
        build_local_face_summary(prefer_hindi),
    ]
    if actor_email:
        summary_parts.append(build_local_profile_summary(actor_email, prefer_hindi))

    if prefer_hindi:
        return (
            "Gemini AI configure nahi hai. Main local summary share kar raha hoon: "
            + " ".join(summary_parts)
        )
    return "Gemini AI not configured. Here is the local SmartAI summary: " + " ".join(summary_parts)


def detect_device_action(query_lower):
    action_map = {
        "on": ("turn on", "switch on", "start", "enable", "chalu", "chaalu", "on karo", "चालू"),
        "off": ("turn off", "switch off", "stop", "disable", "band", "off karo", "बंद"),
        "toggle": ("toggle", "flip", "switch", "change state"),
    }

    matched_action = None
    for action, phrases in action_map.items():
        if any(phrase in query_lower for phrase in phrases):
            matched_action = action
            break

    if matched_action is None:
        return None, None

    for device in list_devices_db():
        if device["name"].lower() in query_lower:
            return device["name"], matched_action

    return None, matched_action


def build_manual_mode_help(prefer_hindi=False):
    if prefer_hindi:
        return (
            "Manual mode time, CPU, health summary, latest alerts, face watch, device list, "
            "camera switching, self monitoring mode status, aur commands jaise 'turn on light', 'enable self monitoring', ya 'next camera' handle karta hai."
        )
    return (
        "Manual mode can answer time, CPU, health summaries, latest alerts, face watch, device lists, "
        "camera switching, self monitoring status, and commands like 'turn on light', 'enable self monitoring', or 'next camera'."
    )


def handle_manual_assistant_query(query, prefer_hindi=False, source="ui", actor_email=None, actor_role=None):
    q_lower = str(query or "").strip().lower()
    if not q_lower:
        return None

    if any(token in q_lower for token in ("hello", "hi", "hey", "namaste")):
        if prefer_hindi:
            return {
                "reply": (
                    f"Namaste. {'Gemini AI configure nahi hai. ' if not is_gemini_ready() else ''}"
                    f"{build_assistant_capability_summary(True)}"
                ),
                "handled_locally": True,
                "actions": [],
            }
        return {
            "reply": (
                f"Hello. {'Gemini AI not configured. ' if not is_gemini_ready() else ''}"
                f"{build_assistant_capability_summary(False)}"
            ),
            "handled_locally": True,
            "actions": [],
        }

    if any(token in q_lower for token in ("help", "what can you do", "capabilities", "madad")):
        return {"reply": build_manual_mode_help(prefer_hindi), "handled_locally": True, "actions": []}

    if any(token in q_lower for token in ("time", "samay", "waqt", "kitne baje")):
        if prefer_hindi:
            return {"reply": f"Abhi samay {datetime.datetime.now().strftime('%H:%M:%S')} hai.", "handled_locally": True, "actions": []}
        return {"reply": f"The time is {datetime.datetime.now().strftime('%H:%M:%S')}", "handled_locally": True, "actions": []}

    if any(token in q_lower for token in ("cpu", "processor")):
        cpu_usage = round(psutil.cpu_percent(interval=0.1), 2)
        if prefer_hindi:
            return {"reply": f"Abhi CPU usage {cpu_usage}% hai.", "handled_locally": True, "actions": []}
        return {"reply": f"CPU usage: {cpu_usage}%", "handled_locally": True, "actions": []}

    if any(token in q_lower for token in ("self monitoring", "self-monitoring", "automation", "manual operating", "manual mode", "sentinel")):
        state = get_automation_state()
        switched = False
        target_mode = None
        if any(token in q_lower for token in ("enable self monitoring", "activate self monitoring", "self monitoring on", "self-monitoring on")):
            target_mode = "self_monitoring"
        elif any(token in q_lower for token in ("enable sentinel", "activate sentinel", "sentinel mode", "switch to sentinel")):
            target_mode = "sentinel"
        elif any(token in q_lower for token in ("manual operating", "manual mode", "switch to manual", "manual control")):
            target_mode = "manual"

        if target_mode and target_mode != state["mode"]:
            state["mode"] = target_mode
            save_automation_state(state)
            switched = True

        snapshot = evaluate_automation(
            actor_email=actor_email,
            actor_role=actor_role,
            source=f"assistant-{source}",
        )
        reply = build_local_automation_summary(prefer_hindi)
        if switched:
            if prefer_hindi:
                reply = f"System mode ab {snapshot['mode_label']} hai. {reply}"
            else:
                reply = f"System mode is now {snapshot['mode_label']}. {reply}"
        return {"reply": reply, "handled_locally": True, "actions": snapshot.get("last_actions", [])}

    if actor_email and any(token in q_lower for token in ("my profile", "profile", "my name", "mera profile", "mera naam")):
        return {"reply": build_local_profile_summary(actor_email, prefer_hindi), "handled_locally": True, "actions": []}

    if any(token in q_lower for token in ("face", "faces", "visitor", "visitors", "unknown", "recognized", "person on camera", "who is there")):
        return {"reply": build_local_face_summary(prefer_hindi), "handled_locally": True, "actions": []}

    if any(token in q_lower for token in ("video agent", "scene", "what do you see", "camera read", "read video", "kya dikh", "kya dikh raha", "video me")):
        snapshot = build_video_agent_snapshot(prefer_hindi=prefer_hindi)
        return {
            "reply": snapshot["speech_text"],
            "handled_locally": True,
            "actions": [{"type": "video_agent", "behavior": snapshot["behavior_label"], "risk": snapshot["risk"]["level"]}],
        }

    camera_switch_request = None
    if "camera" in q_lower and any(token in q_lower for token in ("switch", "change", "next", "previous", "prev", "cycle")):
        camera_switch_request = "next" if any(token in q_lower for token in ("next", "cycle", "forward")) else None
        if camera_switch_request is None and any(token in q_lower for token in ("previous", "prev", "back")):
            camera_switch_request = "previous"

    if camera_switch_request == "next":
        profile = cycle_active_camera(1)
        return {
            "reply": f"Switched to {profile['name']}." if not prefer_hindi else f"Camera switch ho gaya. Ab {profile['name']} active hai.",
            "handled_locally": True,
            "actions": [{"type": "camera", "name": profile["name"], "camera_id": profile["id"]}],
        }

    if camera_switch_request == "previous":
        profile = cycle_active_camera(-1)
        return {
            "reply": f"Switched to {profile['name']}." if not prefer_hindi else f"Camera switch ho gaya. Ab {profile['name']} active hai.",
            "handled_locally": True,
            "actions": [{"type": "camera", "name": profile["name"], "camera_id": profile["id"]}],
        }

    if "camera" in q_lower and any(token in q_lower for token in ("switch", "change", "select", "show")):
        matched_camera = find_camera_profile_for_query(q_lower)
        if matched_camera:
            profile = set_active_camera_profile(matched_camera["id"])
            return {
                "reply": f"Switched to {profile['name']}." if not prefer_hindi else f"Camera switch ho gaya. Ab {profile['name']} active hai.",
                "handled_locally": True,
                "actions": [{"type": "camera", "name": profile["name"], "camera_id": profile["id"]}],
            }

    if any(token in q_lower for token in ("device", "devices", "switches", "controls")):
        return {"reply": build_local_device_summary(prefer_hindi), "handled_locally": True, "actions": []}

    if any(token in q_lower for token in ("camera", "stream", "feed", "monitor")):
        return {"reply": build_local_camera_summary(prefer_hindi), "handled_locally": True, "actions": []}

    if any(token in q_lower for token in ("alert", "alerts", "warning", "warnings")):
        return {"reply": build_local_alert_summary(prefer_hindi), "handled_locally": True, "actions": []}

    if any(token in q_lower for token in ("memory", "ram", "disk", "uptime", "health", "system")):
        return {
            "reply": f"{build_local_health_summary(prefer_hindi)} {build_local_camera_summary(prefer_hindi)}",
            "handled_locally": True,
            "actions": [],
        }

    if any(token in q_lower for token in ("admin", "role", "current user", "who am i")) and session.get("user"):
        if any(token in q_lower for token in ("admin", "users", "dashboard summary", "admin summary")):
            return {
                "reply": build_local_admin_summary(prefer_hindi),
                "handled_locally": True,
                "actions": [],
            }
        if prefer_hindi:
            return {
                "reply": f"Aap {session.get('user')} ke roop mein login hain aur aapka role {current_user_role()} hai.",
                "handled_locally": True,
                "actions": [],
            }
        return {
            "reply": f"You are signed in as {session.get('user')} with the {current_user_role()} role.",
            "handled_locally": True,
            "actions": [],
        }

    device_name, device_action = detect_device_action(q_lower)
    if device_action and device_name:
        updated_device, error_message = perform_device_action(
            device_name,
            device_action,
            source=f"assistant-{source}",
            actor_email=actor_email,
            actor_role=actor_role,
        )
        if error_message:
            return {"reply": error_message, "handled_locally": True, "actions": []}

        if prefer_hindi:
            reply = f"{updated_device['name']} ab {updated_device['state']} hai."
        else:
            reply = f"{updated_device['name']} is now {updated_device['state']}."
        return {
            "reply": reply,
            "handled_locally": True,
            "actions": [{"type": "device", "name": updated_device["name"], "state": updated_device["state"]}],
        }

    if device_action and not device_name:
        if prefer_hindi:
            return {"reply": "Mujhe action samajh aaya, lekin device naam clear nahi mila.", "handled_locally": True, "actions": []}
        return {"reply": "I understood the action, but I could not match a device name.", "handled_locally": True, "actions": []}

    return None


@app.route("/assistant", methods=["POST"])
@login_or_local_voice_required
def assistant():
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query", "")).strip()
    mode = resolve_assistant_mode(payload)
    source = str(payload.get("source", "") or "ui").strip().lower() or "ui"
    actor_email = session.get("user")
    actor_role = current_user_role() if actor_email else "system"

    if mode in {"manual", "self_monitoring", "sentinel"}:
        target_mode = "manual" if mode == "manual" else ("sentinel" if mode == "sentinel" else "self_monitoring")
        control_state = get_automation_state()
        if control_state["mode"] != target_mode:
            control_state["mode"] = target_mode
            if target_mode == "sentinel":
                control_state["environment"]["risk"] = "critical"
                control_state["settings"]["defense_armed"] = True
                control_state["settings"]["auto_alarm"] = True
            save_automation_state(control_state)
            evaluate_automation(actor_email=actor_email, actor_role=actor_role, source=f"assistant-{source}")

    if not query:
        return jsonify({"reply": "Please speak something.", "mode": mode, "handled_locally": True, "actions": []})

    prefer_hindi = prefers_hindi_response(payload, query)
    manual_result = handle_manual_assistant_query(
        query,
        prefer_hindi=prefer_hindi,
        source=source,
        actor_email=actor_email,
        actor_role=actor_role,
    )

    if manual_result is not None:
        manual_result["mode"] = mode
        log_activity(
            "assistant_query",
            actor_email=actor_email,
            actor_role=actor_role,
            target_type="assistant",
            target_name=mode,
            source=source,
            details={
                "mode": mode,
                "handled_locally": True,
                "query": query[:160],
                "actions": manual_result.get("actions", []),
            },
        )
        return jsonify(manual_result)

    if mode == "manual":
        reply = build_manual_mode_help(prefer_hindi)
        log_activity(
            "assistant_query",
            actor_email=actor_email,
            actor_role=actor_role,
            target_type="assistant",
            target_name=mode,
            source=source,
            details={"mode": mode, "handled_locally": True, "query": query[:160]},
        )
        return jsonify({"reply": reply, "mode": mode, "handled_locally": True, "actions": []})

    # Add camera and behavior context to the query
    voice_style = (
        "Reply in short, natural, spoken Hindi. Use Devanagari where possible, keep the tone warm, "
        "and mix only simple English technical words when needed."
        if prefer_hindi
        else "Reply in the user's language, keep it short, warm, and natural for a spoken assistant."
    )

    if mode == "research":
        handled_locally = True
        if is_gemini_ready():
            context = f"""
Local SmartAI research snapshot:
- System: {build_local_health_summary(False)}
- Camera: {build_local_camera_summary(False)}
- Devices: {build_local_device_summary(False)}
- Faces: {build_local_face_summary(False)}
- Automation: {build_local_automation_summary(False)}
- Human behavior: {human_behavior}
- Users: {count_users()} total, {count_admin_users()} admins

User question: {query}

Respond like an operations analyst. Summarize observations, risks, and next best actions in concise language.
{voice_style}
"""
            reply = ask_gemini(context, prefer_hindi=prefer_hindi)
            handled_locally = is_gemini_error_reply(reply)
            if handled_locally:
                reply = build_research_mode_reply(query, prefer_hindi=prefer_hindi)
        else:
            reply = build_research_mode_reply(query, prefer_hindi=prefer_hindi)
        log_activity(
            "assistant_query",
            actor_email=actor_email,
            actor_role=actor_role,
            target_type="assistant",
            target_name=mode,
            source=source,
            details={"mode": mode, "handled_locally": handled_locally, "query": query[:160]},
        )
        return jsonify(
            {
                "reply": reply,
                "mode": mode,
                "handled_locally": handled_locally,
                "actions": [],
            }
        )

    context = f"""
Current camera observations: {build_local_camera_summary(False)}
Human behavior status: {human_behavior}
Recognized faces: {latest_faces}
Detected objects: {latest_detections}
Current devices: {list_devices_db()}
Automation status: {build_local_automation_summary(False)}
Current user role: {actor_role}
Assistant mode: {mode}

User query: {query}

Please respond as a friendly, human-like AI assistant that is aware of the camera feed and human behavior. Be conversational, proactive, and helpful. If you see someone in the camera, acknowledge them naturally. Keep responses concise but warm.
{voice_style}
"""

    handled_locally = False
    if is_gemini_ready():
        reply = ask_gemini(context, prefer_hindi=prefer_hindi)
        if is_gemini_error_reply(reply):
            handled_locally = True
            reply = build_non_gemini_assistant_reply(
                query,
                mode,
                prefer_hindi=prefer_hindi,
                actor_email=actor_email,
            )
    else:
        handled_locally = True
        reply = build_non_gemini_assistant_reply(
            query,
            mode,
            prefer_hindi=prefer_hindi,
            actor_email=actor_email,
        )

    log_activity(
        "assistant_query",
        actor_email=actor_email,
        actor_role=actor_role,
        target_type="assistant",
        target_name=mode,
        source=source,
        details={"mode": mode, "handled_locally": handled_locally, "query": query[:160]},
    )
    return jsonify({"reply": reply, "mode": mode, "handled_locally": handled_locally, "actions": []})


# ---------------------------------------------------------
# Device controls
# ---------------------------------------------------------
@app.route("/toggle/<device>", methods=["POST"])
@login_required
def toggle_device(device):
    updated_device, error_message = perform_device_action(
        device,
        "toggle",
        source="controls",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
    )
    if error_message:
        return jsonify({"error": error_message}), 404
    return jsonify({updated_device["name"]: updated_device["state"]})


@app.route('/api/devices', methods=['GET'])
@login_required
def api_list_devices():
    refresh_device_state_cache()
    return jsonify({'devices': list_devices_db()})


@app.route('/api/devices', methods=['POST'])
@login_required
def api_add_device():
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    if not name:
        return jsonify({'error': 'missing name'}), 400
    name = normalize_device_name(name)
    if not name:
        return jsonify({'error': 'missing name'}), 400
    if resolve_device_name(name) is not None:
        return jsonify({'error': 'device already exists'}), 400
    try:
        set_device_state_db(name, 'OFF')
        refresh_device_state_cache()
        log_activity(
            "device_created",
            actor_email=session.get("user"),
            actor_role=current_user_role(),
            target_type="device",
            target_name=name,
            source="controls",
            details={"state": "OFF"},
        )
        return jsonify({'name': name, 'state': 'OFF'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/devices/<path:name>', methods=['DELETE'])
@login_required
def api_delete_device(name):
    deleted_name = delete_device_db(name)
    if deleted_name is None:
        return jsonify({'error': 'device not found'}), 404

    log_activity(
        "device_deleted",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="device",
        target_name=deleted_name,
        source="controls",
    )
    log_system_alert(
        f"{deleted_name.capitalize()} removed from dashboard",
        level="info",
        details={"device": deleted_name, "action": "delete"},
    )
    return jsonify({'ok': True, 'name': deleted_name})


@app.route('/register', methods=['POST'])
def register():
    data = request.form or request.get_json(silent=True) or {}
    email = normalize_email(data.get('email'))
    full_name = normalize_person_name(data.get("full_name"))
    password = data.get('password')
    validation_error = validate_auth_form(email, password)
    if validation_error:
        return auth_error_response(validation_error, status=400, page="register")
    if len(password) < AUTH_MIN_PASSWORD_LENGTH:
        return auth_error_response(
            f"Password must be at least {AUTH_MIN_PASSWORD_LENGTH} characters.",
            status=400,
            page="register",
        )
    ok, error_message = create_user(email, password, full_name=full_name)
    if not ok:
        return auth_error_response(
            error_message or "Unable to create user right now.",
            status=400,
            page="register",
        )
    role = set_authenticated_session(email)
    log_activity(
        "user_registered",
        actor_email=email,
        actor_role=role,
        target_type="user",
        target_name=email,
        source="register",
        details={"role": role, "full_name": full_name},
    )
    return auth_success_response('/')


@app.route('/login', methods=['POST'])
def login_post():
    data = request.form or request.get_json(silent=True) or {}
    email = normalize_email(data.get('email'))
    password = data.get('password')
    if not has_users():
        return auth_error_response(
            'No account found yet. Please create your first account.',
            status=400,
            redirect_to='/register',
        )
    validation_error = validate_auth_form(email, password)
    if validation_error:
        return auth_error_response(validation_error, status=400, page="login")
    if verify_user(email, password):
        try:
            audit_login_attempt(email, True)
        except Exception:
            pass
        update_last_login(email)
        try:
            update_user_login_metadata(email)
        except Exception:
            pass
        role = set_authenticated_session(email)
        log_activity(
            "user_logged_in",
            actor_email=email,
            actor_role=role,
            target_type="auth",
            target_name=email,
            source="login",
        )
        return auth_success_response('/')
    try:
        audit_login_attempt(email, False)
    except Exception:
        pass
    return auth_error_response(
        'Invalid email or password.',
        status=401,
        page="login",
    )


@app.route('/logout')
@login_required
def logout():
    log_activity(
        "user_logged_out",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="auth",
        target_name=session.get("user"),
        source="logout",
    )
    session.clear()
    return redirect('/')


if __name__ == "__main__":
    run_server()
