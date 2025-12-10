# app.py
import os
import datetime
import psutil
import platform

from flask import Flask, render_template, jsonify, request, Response

# ------------ Optional imports: Camera + YOLO -------------
try:
    import cv2
    CAMERA_AVAILABLE = True
except Exception:
    CAMERA_AVAILABLE = False

# YOLO (Ultralytics)
YOLO_AVAILABLE = False
YOLO_MODEL = None
YOLO_ENABLED = False
YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "yolo11n.pt")  # default model

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except Exception:
    YOLO_AVAILABLE = False

# ------------ Gemini AI (Google Generative AI) -------------
from dotenv import load_dotenv
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# 👉 Model name ab .env se configurable hai
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-1.5-flash")

try:
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    GEMINI_AVAILABLE = True
except Exception:
    GEMINI_AVAILABLE = False

app = Flask(__name__)

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
# Global alert variable (camera/health)
# ---------------------------------------------------------
latest_alert = "✅ No alerts"

# ---------------------------------------------------------
# Home route - Dashboard
# ---------------------------------------------------------
@app.route("/")
def home():
    return render_template("dashboard.html")


# ---------------------------------------------------------
# Health route (auto-refresh + alerts)
# ---------------------------------------------------------
@app.route("/health")
def health():
    cpu = psutil.cpu_percent(interval=0.5)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent

    # simple alert logic
    alert = "✅ Normal"
    if cpu > 85:
        alert = f"⚠️ High CPU usage: {cpu}%"
    elif memory > 90:
        alert = f"⚠️ High Memory usage: {memory}%"
    elif disk > 90:
        alert = f"⚠️ Low Disk Space: {disk}% used"

    sent_mb, recv_mb = get_network_usage_mb()

    data = {
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cpu_percent": cpu,
        "memory": memory,
        "disk": disk,
        "os": platform.platform(),
        "uptime": get_uptime(),
        "processes": len(psutil.pids()),
        "net_sent": sent_mb,
        "net_recv": recv_mb,
        "alert": alert,
    }
    return jsonify(data)


# ---------------------------------------------------------
# Camera stream generator (YOLO + fallback)
# ---------------------------------------------------------
def gen_empty_frame():
    """1px black jpg to avoid broken image if camera disabled."""
    import io
    from PIL import Image

    img = Image.new("RGB", (640, 480), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def init_camera():
    """Try to open default camera on startup."""
    global CAMERA_AVAILABLE
    if not CAMERA_AVAILABLE or os.getenv("DISABLE_CAMERA") == "1":
        CAMERA_AVAILABLE = False
        return None

    try:
        camera_index = int(os.getenv("CAMERA_INDEX", "0"))
        cam = cv2.VideoCapture(camera_index)
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        ok, _ = cam.read()
        if not ok:
            CAMERA_AVAILABLE = False
            cam.release()
            return None
        return cam
    except Exception:
        CAMERA_AVAILABLE = False
        return None


# Initialize YOLO model
def init_yolo():
    global YOLO_MODEL, YOLO_ENABLED
    if not YOLO_AVAILABLE or os.getenv("DISABLE_YOLO", "0") == "1":
        return

    try:
        YOLO_MODEL = YOLO(YOLO_MODEL_PATH)
        YOLO_ENABLED = True
        print(f"✅ YOLO loaded: {YOLO_MODEL_PATH}")
    except Exception as e:
        YOLO_ENABLED = False
        print(f"⚠️ YOLO model load error, running without YOLO: {e}")


init_yolo()


def run_yolo_on_frame(frame):
    """Run YOLO on a single frame and draw detections."""
    global latest_alert
    if not YOLO_ENABLED or YOLO_MODEL is None:
        return frame

    try:
        results = YOLO_MODEL(frame, verbose=False)
        if not results:
            return frame
        res = results[0]

        labels_detected = []
        persons = 0

        if hasattr(res, "boxes") and res.boxes is not None:
            for box in res.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                if conf < 0.45:
                    continue
                label = YOLO_MODEL.names.get(cls_id, str(cls_id))
                labels_detected.append(label)

                # bbox
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                text = f"{label} {conf:.2f}"
                cv2.putText(
                    frame,
                    text,
                    (x1, max(20, y1 - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )
                if label.lower() == "person":
                    persons += 1

        if persons > 0:
            latest_alert = f"⚠️ Person detected on camera ({persons})"
        elif labels_detected:
            short = ", ".join(sorted(set(labels_detected))[:4])
            latest_alert = f"⚠️ Objects detected: {short}"
        else:
            latest_alert = "✅ No alerts"
    except Exception:
        # If YOLO fails once, don't crash video
        pass

    return frame


def generate_frames():
    global latest_alert

    if not CAMERA_AVAILABLE:
        empty = gen_empty_frame()
        while True:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + empty + b"\r\n"
            )
    else:
        cam = init_camera()
        if cam is None:
            empty = gen_empty_frame()
            while True:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + empty + b"\r\n"
                )
        else:
            while True:
                success, frame = cam.read()
                if not success:
                    break

                # YOLO object detection (person, phone, charger, etc.)
                if YOLO_ENABLED and YOLO_MODEL is not None:
                    frame = run_yolo_on_frame(frame)

                # Encode frame as JPEG
                ret, buffer = cv2.imencode(".jpg", frame)
                if not ret:
                    continue
                frame_bytes = buffer.tobytes()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                )

            cam.release()


@app.route("/camera_feed")
def camera_feed():
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------
# Endpoint to return latest camera alert (polled by UI)
# ---------------------------------------------------------
@app.route("/get_alert")
def get_alert():
    global latest_alert
    return jsonify({"alert": latest_alert})


# ---------------------------------------------------------
# Gemini Voice assistant
# ---------------------------------------------------------
def ask_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY or not GEMINI_AVAILABLE:
        return "Gemini AI not configured. Add GEMINI_API_KEY in .env."

    try:
        # 👉 Model name ab variable se aa raha hai
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        response = model.generate_content(prompt)
        text = getattr(response, "text", None)
        if not text:
            return "No response from Gemini."
        return text
    except Exception as e:
        # yaha se jo exact error aayega, wahi UI me dikhega
        return f"Gemini Error: {e}"


@app.route("/assistant", methods=["POST"])
def assistant():
    payload = request.get_json() or {}
    query = str(payload.get("query", "")).strip()

    if not query:
        return jsonify({"reply": "Please speak something."})

    q_lower = query.lower()

    # Simple local info
    if "time" in q_lower:
        return jsonify(
            {"reply": f"The time is {datetime.datetime.now().strftime('%H:%M:%S')}"}
        )
    if "cpu" in q_lower:
        return jsonify({"reply": f"CPU usage: {psutil.cpu_percent()}%"})

    reply = ask_gemini(query)
    return jsonify({"reply": reply})


# ---------------------------------------------------------
# Dummy device controls (Light, Fan, AC, TV)
# ---------------------------------------------------------
device_state = {"light": "OFF", "fan": "OFF", "ac": "OFF", "tv": "OFF"}


@app.route("/toggle/<device>", methods=["POST"])
def toggle_device(device):
    if device not in device_state:
        return jsonify({"error": "Device not found"}), 404
    device_state[device] = "ON" if device_state[device] == "OFF" else "OFF"
    return jsonify({device: device_state[device]})


# ---------------------------------------------------------
# Start server
# ---------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=true)
    host = "0.0.0.0" if os.getenv("HOST_PUBLIC", "0") == "1" else "127.0.0.1"

    print(
        f"✅ Starting SmartAI Flask Server on {host}:{port} "
        f"(camera available: {CAMERA_AVAILABLE}, YOLO: {YOLO_ENABLED})"
    )
    app.run(debug=True, host=host, port=port)
