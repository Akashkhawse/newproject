# backend/app.py
import base64
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import time
from collections import Counter, deque
from functools import wraps
from types import SimpleNamespace

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
    print(f"⚠️ Recommended to set environment variables: {', '.join(_missing_env)}. A default will be used if missing.")


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


def resolve_project_path(value):
    if os.path.isabs(value):
        return value
    return os.path.join(PROJECT_ROOT, value)


def normalize_camera_name(value):
    return " ".join(str(value or "").strip().split())


def camera_source_display(value):
    source = str(value if value is not None else "").strip()
    return source or str(CAMERA_INDEX)


def normalize_camera_source(value):
    raw = str(value if value is not None else "").strip()
    if not raw:
        return raw

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
    if transport == "network":
        camera_type_value = "network"
    kind = "Network" if transport == "network" else "USB"
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
DISABLE_FACE_RECOGNITION = get_env_bool("DISABLE_FACE_RECOGNITION", False)
KNOWN_FACES_DIR = resolve_project_path(os.getenv("KNOWN_FACES_DIR", "known_faces"))
FACE_RECOGNITION_THRESHOLD = get_env_float("FACE_RECOGNITION_THRESHOLD", 70)
FACE_IMAGE_SIZE = (160, 160)
FACE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
FACE_UPLOAD_MAX_FILES = max(1, get_env_int("FACE_UPLOAD_MAX_FILES", 6))
FACE_UPLOAD_MAX_BYTES = max(128 * 1024, get_env_int("FACE_UPLOAD_MAX_BYTES", 5 * 1024 * 1024))
AUTH_MIN_PASSWORD_LENGTH = 8

try:
    from ultralytics import YOLO

    YOLO_AVAILABLE = True
except Exception:
    YOLO_AVAILABLE = False

# ------------ Gemini AI (Google Generative AI) -------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")

try:
    import google.generativeai as genai

    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    GEMINI_AVAILABLE = True
except Exception:
    GEMINI_AVAILABLE = False

app = Flask(
    __name__,
    template_folder=os.path.join(PROJECT_ROOT, "templates"),
    static_folder=os.path.join(PROJECT_ROOT, "static"),
)
# Session security configuration
app.secret_key = os.getenv("FLASK_SECRET")
if not app.secret_key:
    print("⚠️ WARNING: FLASK_SECRET environment variable is not set. Using an insecure default for development only.")
    print("⚠️ Set FLASK_SECRET in .env for production use!")
    app.secret_key = "dev-secret-change-in-production"

app.config.update(
    SESSION_COOKIE_SECURE=get_env_bool("SESSION_COOKIE_SECURE", False),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=1),
)
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "app.db")
DEFAULT_DEVICE_STATE = {"light": "OFF", "fan": "OFF", "ac": "OFF", "tv": "OFF"}
VALID_USER_ROLES = {"admin", "user"}
VALID_ASSISTANT_MODES = {"hybrid", "manual", "ai", "research"}


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
    port = int(os.getenv("PORT", "5000"))
    host = "0.0.0.0" if os.getenv("HOST_PUBLIC", "0") == "1" else "127.0.0.1"
    selected_port = choose_start_port(port)

    if selected_port != port:
        print(f"Port {port} busy hai, SmartAI {selected_port} par start ho raha hai.")

    print(f"Starting SmartAI Flask Server on {host}:{selected_port}")
    app.run(debug=FLASK_DEBUG, host=host, port=selected_port)


@app.after_request
def add_smartai_backend_header(response):
    response.headers["X-SmartAI-Backend"] = "1"
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

# --- Simple SQLite persistence (users, devices, activity) ---
def db_now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def normalize_device_name(value):
    return " ".join(str(value or "").split())


def normalize_person_name(value):
    return " ".join(str(value or "").strip().split())


def get_db_path():
    return os.getenv("APP_DB_PATH", DEFAULT_DB_PATH)


def get_db_conn():
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
    ensure_column(conn, "users", "full_name", "full_name TEXT")
    ensure_column(conn, "users", "role", "role TEXT NOT NULL DEFAULT 'user'")
    ensure_column(conn, "users", "created_at", "created_at TEXT")
    ensure_column(conn, "users", "last_login_at", "last_login_at TEXT")
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
        "role": (row["role"] or "user").lower(),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def get_user_by_email(email):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return None

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, email, password, full_name, role, created_at, last_login_at
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


def create_user(email: str, password: str, full_name: str | None = None):
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        hashed = generate_password_hash(password)
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
        SELECT email, full_name, role, created_at, last_login_at
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


def update_user_profile_db(email, full_name):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return None

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET full_name = ?
        WHERE email = ?
        """,
        (normalize_person_name(full_name), normalized_email),
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
    return serialized


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
        (max(1, int(limit)),),
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


def perform_device_action(device_name, action, source="ui", actor_email=None, actor_role=None):
    resolved_name = resolve_device_name(device_name)
    if not resolved_name:
        return None, "Device not found"

    current_state = get_device_state_db(resolved_name) or "OFF"
    normalized_action = str(action or "").strip().lower()
    if normalized_action == "toggle":
        new_state = "ON" if current_state == "OFF" else "OFF"
    elif normalized_action == "on":
        new_state = "ON"
    elif normalized_action == "off":
        new_state = "OFF"
    else:
        return None, "Unsupported device action"

    set_device_state_db(resolved_name, new_state)
    refresh_device_state_cache()

    message = f"{resolved_name.capitalize()} turned {new_state}"
    log_system_alert(
        message,
        level="info",
        details={"device": resolved_name, "state": new_state, "action": normalized_action},
    )
    log_activity(
        "device_updated",
        actor_email=actor_email,
        actor_role=actor_role,
        target_type="device",
        target_name=resolved_name,
        source=source,
        details={"state": new_state, "action": normalized_action},
    )
    return {"name": resolved_name, "state": new_state}, None


# Initialize DB on startup
init_db()
ensure_default_devices_seeded()
refresh_device_state_cache()
latest_detections = []
latest_faces = []
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
FACE_RECOGNIZER = None
FACE_DETECTOR = None
FACE_RECOGNITION_ENABLED = False
FACE_LABELS = {}
camera_registry_lock = threading.Lock()
camera_registry = []
active_camera_id = None


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
    global latest_alert, latest_camera_alert_payload, latest_detections, latest_faces

    payload = build_alert_payload(message, "camera", level=level, details=details)
    latest_alert = message
    latest_detections = list((details or {}).get("detections", []))
    latest_faces = list((details or {}).get("faces", []))
    latest_camera_alert_payload = payload

    if log:
        log_alert(payload)

    return payload


def log_system_alert(message, level="warning", details=None):
    payload = build_alert_payload(message, "system", level=level, details=details)
    log_alert(payload)
    return payload


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

    face_module = getattr(cv2, "face", None)
    if face_module is None or not hasattr(face_module, "LBPHFaceRecognizer_create"):
        update_face_status(False, "Install opencv-contrib-python-headless for face recognition")
        return

    detector, error = build_face_detector()
    if error:
        update_face_status(False, error)
        return

    if not os.path.isdir(KNOWN_FACES_DIR):
        update_face_status(False, "No known faces directory found")
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
        remote_addr in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        and assistant_header == "1"
    )


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if session.get("user"):
            return view(*args, **kwargs)
        return auth_required_response()

    return wrapped_view


def login_or_local_voice_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if session.get("user") or is_local_voice_assistant_request():
            return view(*args, **kwargs)
        return auth_required_response()

    return wrapped_view


def normalize_email(value):
    return str(value or "").strip().lower()


def current_user_role():
    cached_role = str(session.get("user_role", "") or "").strip().lower()
    if cached_role in VALID_USER_ROLES:
        return cached_role

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
    return session["user_role"]


def forbidden_response(message="admin access required"):
    if expects_json_response():
        return jsonify({"error": message}), 403
    return redirect("/")


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("user"):
            return auth_required_response()
        if current_user_role() != "admin":
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

# ---------------------------------------------------------
# Home route - Dashboard
# ---------------------------------------------------------
@app.route("/")
@login_required
def home():
    role = current_user_role()
    profile = build_user_profile(session.get("user")) or {}
    return render_template(
        "dashboard.html",
        current_user=session.get("user"),
        current_user_name=profile.get("display_name", session.get("user")),
        current_user_role=role,
        is_admin=role == "admin",
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
    cpu = psutil.cpu_percent(interval=0.5)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    devices = list_devices_db()
    active_camera = get_active_camera_profile()

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
        "processes": len(psutil.pids()),
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
    }
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
    transport = "network" if active_profile.get("transport") == "network" else "local"
    return f"{active_profile['name']} ({transport})"


def init_camera():
    """Try to open the configured camera."""
    update_camera_diagnostics(stream_source="server")
    active_profile = get_active_camera_profile()
    update_camera_profile_state(active_profile)

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

        detections = [
            {"label": label, "count": count}
            for label, count in labels_detected.most_common()
        ]
        result["detections"] = detections

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

        if person_count > 0:
            result.update({
                "message": f"⚠️ Person detected on camera ({person_count})",
                "level": "warning",
                "log": True,
            })
        elif alert_hits:
            result.update({
                "message": f"⚠️ Alert objects detected: {format_detection_summary(alert_hits)}",
                "level": "warning",
                "log": True,
            })
        elif labels_detected:
            result.update({
                "message": f"ℹ️ Objects detected: {format_detection_summary(labels_detected)}",
                "level": "info",
                "log": False,
            })
    except Exception as exc:
        result.update({
            "message": "⚠️ YOLO detection error",
            "level": "error",
            "log": True,
            "reason": str(exc),
        })

    return frame, result


def apply_frame_alerts(face_result, yolo_result):
    details = {
        "detections": list(yolo_result.get("detections", [])),
        "faces": list(face_result.get("faces", [])),
    }

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
        set_camera_alert(
            f"⚠️ Unknown {noun} detected ({count})",
            level="warning",
            details=details,
            log=True,
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

    set_camera_alert(
        yolo_result.get("message", "✅ No alerts"),
        level=yolo_result.get("level", "info"),
        details=details,
        log=yolo_result.get("log", False),
    )


def generate_frames():
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

            frame, yolo_result = run_yolo_on_frame(frame)
            face_result = run_face_recognition_on_frame(frame)
            apply_frame_alerts(face_result, yolo_result)

            ret, buffer = cv2.imencode(".jpg", frame)
            if not ret:
                continue

            yield stream_frame_bytes(buffer.tobytes())
    finally:
        cam.release()

    yield from stream_placeholder_frames()


@app.route("/camera_feed")
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
    payload["camera_available"] = camera_state["available"]
    payload["camera_status"] = camera_state["message"]
    payload["active_camera"] = active_camera
    payload["camera_count"] = len(list_camera_profiles())
    payload["yolo_enabled"] = YOLO_ENABLED
    payload["face_recognition_enabled"] = FACE_RECOGNITION_ENABLED
    payload["face_recognition_status"] = face_state["message"]
    payload["known_people"] = face_state["known_people"]
    payload["detections"] = latest_detections
    payload["faces"] = latest_faces
    payload["current_user_role"] = current_user_role() if session.get("user") else "system"
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
    items = list(alert_history)
    level_summary = Counter(item.get("level", "info") for item in items)
    return jsonify(
        {
            "items": items,
            "latest_camera_alert": latest_camera_alert_payload,
            "camera_status": camera_state,
            "face_status": face_state,
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
    profiles = list_camera_profiles()
    active_profile = get_active_camera_profile(refresh=False)
    return {
        "active_camera": active_profile,
        "active_camera_id": active_profile["id"],
        "cameras": profiles,
        "camera_count": len(profiles),
        "camera_status": camera_state["message"],
        "camera_available": camera_state["available"],
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


@app.route("/api/cameras", methods=["GET"])
@login_required
def api_list_cameras():
    return jsonify(build_camera_registry_snapshot())


@app.route("/api/cameras", methods=["POST"])
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

    return jsonify({"ok": True, "camera": profile, "cameras": list_camera_profiles()})


@app.route("/api/cameras/active", methods=["POST"])
@login_required
def api_set_active_camera():
    payload = request.get_json(silent=True) or {}
    direction = str(payload.get("direction", "") or "").strip().lower()
    camera_id = payload.get("camera_id")

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

    profile = update_user_profile_db(session.get("user"), full_name)
    session["user_full_name"] = profile.get("full_name", "")
    log_activity(
        "profile_updated",
        actor_email=session.get("user"),
        actor_role=current_user_role(),
        target_type="profile",
        target_name=session.get("user"),
        source="profile",
        details={"full_name": full_name},
    )
    return jsonify({"ok": True, "profile": build_user_profile(session.get("user"))})


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
# Gemini Voice assistant
# ---------------------------------------------------------
def ask_gemini(prompt: str, prefer_hindi=False) -> str:
    if not GEMINI_API_KEY or not GEMINI_AVAILABLE:
        if prefer_hindi:
            return "Gemini AI configure nahi hai. Kripya .env mein GEMINI_API_KEY add kijiye."
        return "Gemini AI not configured. Add GEMINI_API_KEY in .env."

    try:
        # 👉 Model name ab variable se aa raha hai
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        response = model.generate_content(prompt)
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
            f"uptime {get_uptime()} aur processes {len(psutil.pids())} hain."
        )
    return (
        f"System summary: CPU {cpu}%, memory {memory}%, disk {disk}%, "
        f"uptime {get_uptime()}, and {len(psutil.pids())} running processes."
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
            "profile summary, admin snapshot, aur camera switching me help kar sakta hoon."
        )
    return (
        "I can help with health, camera alerts, known versus unknown faces, device control, "
        "profile summaries, admin status, and camera switching."
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
            "camera switching, aur commands jaise 'turn on light' ya 'next camera' handle karta hai."
        )
    return (
        "Manual mode can answer time, CPU, health summaries, latest alerts, face watch, device lists, "
        "camera switching, and commands like 'turn on light' or 'next camera'."
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

    if actor_email and any(token in q_lower for token in ("my profile", "profile", "my name", "mera profile", "mera naam")):
        return {"reply": build_local_profile_summary(actor_email, prefer_hindi), "handled_locally": True, "actions": []}

    if any(token in q_lower for token in ("face", "faces", "visitor", "visitors", "unknown", "recognized", "person on camera", "who is there")):
        return {"reply": build_local_face_summary(prefer_hindi), "handled_locally": True, "actions": []}

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
        update_last_login(email)
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
    session.pop('user', None)
    session.pop('user_role', None)
    return redirect('/')


if __name__ == "__main__":
    run_server()
