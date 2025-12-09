import os
import re
import shutil
import struct
import subprocess
import time

try:
    import pyaudio
except Exception:
    pyaudio = None

try:
    import pvporcupine
except Exception:
    pvporcupine = None

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

try:
    import requests
except Exception:
    requests = None

try:
    import speech_recognition as sr
except Exception:
    sr = None
from dotenv import load_dotenv

load_dotenv()


def get_env_value(*names, default=""):
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return str(default).strip()


def derive_backend_base_url(url):
    normalized = str(url or "").rstrip("/")
    if normalized.endswith("/assistant"):
        return normalized.rsplit("/", 1)[0]
    return normalized


def build_backend_candidate_urls():
    default_port = get_env_value("VOICE_ASSISTANT_PORT", "PORT", default="5000") or "5000"
    configured_url = get_env_value(
        "SMARTAI_BACKEND_URL",
        "ASSISTANT_URL",
        default=f"http://127.0.0.1:{default_port}/assistant",
    ).rstrip("/")
    candidates = [configured_url]

    for port in (default_port, "5000", "5001", "5002", "5003"):
        for host in ("127.0.0.1", "localhost"):
            candidates.append(f"http://{host}:{port}/assistant")

    unique_candidates = []
    for candidate in candidates:
        normalized = str(candidate or "").rstrip("/")
        if normalized and normalized not in unique_candidates:
            unique_candidates.append(normalized)
    return unique_candidates


def assistant_url_for(candidate_url):
    return f"{derive_backend_base_url(candidate_url)}/assistant"


WAKE_WORD = get_env_value("WAKE_WORD", default="computer") or "computer"
PORCUPINE_ACCESS_KEY = get_env_value(
    "PORCUPINE_ACCESS_KEY",
    "PORCUPINE_KEY",
    "PICOVOICE_ACCESS_KEY",
)
BACKEND_CANDIDATE_URLS = build_backend_candidate_urls()
BACKEND_URL = assistant_url_for(BACKEND_CANDIDATE_URLS[0])
BACKEND_BASE_URL = derive_backend_base_url(BACKEND_URL)
VOICE_ASSISTANT_EMAIL = get_env_value("VOICE_ASSISTANT_EMAIL")
VOICE_ASSISTANT_PASSWORD = get_env_value("VOICE_ASSISTANT_PASSWORD")
SAY_COMMAND = shutil.which("say")
VOICE_ENGINE = os.getenv("VOICE_ENGINE", "say" if SAY_COMMAND else "pyttsx3").strip().lower()
HINDI_TTS_VOICE = os.getenv("HINDI_TTS_VOICE", "Lekha").strip() or "Lekha"
ENGLISH_TTS_VOICE = os.getenv("ENGLISH_TTS_VOICE", "Rishi").strip() or "Rishi"
TTS_RATE = str(os.getenv("TTS_RATE", "175")).strip() or "175"
COMMAND_TIMEOUT_SECONDS = 6
COMMAND_PHRASE_TIME_LIMIT = 8
WAKE_PHRASE_TIME_LIMIT = 3
WAKE_WORD_ALIASES = {
    WAKE_WORD.lower(),
    "computer",
    "कंप्यूटर",
    "कम्प्यूटर",
}
VOICE_ASSISTANT_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "X-SmartAI-Voice-Assistant": "1",
    "User-Agent": "SmartAI-VoiceAssistant/1.0",
}


def build_backend_endpoint(path):
    return f"{BACKEND_BASE_URL}/{path.lstrip('/')}"


def set_active_backend_url(url):
    global BACKEND_URL, BACKEND_BASE_URL
    BACKEND_URL = assistant_url_for(url)
    BACKEND_BASE_URL = derive_backend_base_url(BACKEND_URL)


def iter_backend_urls():
    seen = []
    for candidate in [BACKEND_URL, *BACKEND_CANDIDATE_URLS]:
        normalized = str(candidate or "").rstrip("/")
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def response_matches_smartai_backend(response, path):
    if response.headers.get("X-SmartAI-Backend") == "1":
        return True

    payload = parse_json_safe(response)
    if path == "/assistant":
        return "reply" in payload
    if path == "/get_alert":
        return any(
            key in payload
            for key in ("alert", "camera_status", "face_recognition_enabled", "faces")
        )
    if path == "/behavior":
        return "person_present" in payload
    if path == "/login":
        return any(key in payload for key in ("success", "error", "redirect"))
    return False


def request_backend(session, method, path, **kwargs):
    last_response = None
    last_error = None
    # Set a default timeout of 10 seconds if not provided
    if "timeout" not in kwargs:
        kwargs["timeout"] = 10

    for candidate_url in iter_backend_urls():
        url = assistant_url_for(candidate_url) if path == "/assistant" else build_backend_endpoint_for(candidate_url, path)
        try:
            response = session.request(method, url, **kwargs)
            if response.status_code != 404 and response_matches_smartai_backend(response, path):
                set_active_backend_url(candidate_url)
                return response
            last_response = response
        except requests.RequestException as exc:
            last_error = exc

    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise requests.RequestException("No backend server candidate was reachable.")


def build_backend_endpoint_for(candidate_url, path):
    return f"{derive_backend_base_url(candidate_url)}/{path.lstrip('/')}"


def parse_json_safe(response):
    try:
        return response.json()
    except Exception:
        return {}


def contains_devanagari(text):
    return any("\u0900" <= char <= "\u097F" for char in str(text or ""))


def looks_hindi_text(text):
    normalized = str(text or "").strip().lower()
    if contains_devanagari(normalized):
        return True
    return any(
        token in normalized
        for token in (
            "namaste",
            "kaise",
            "kya",
            "nahi",
            "haan",
            "samay",
            "waqt",
            "boliye",
            "dhanyavaad",
            "madad",
        )
    )


def choose_tts_voice(text):
    return HINDI_TTS_VOICE if looks_hindi_text(text) else ENGLISH_TTS_VOICE


def sanitize_tts_text(text):
    clean = str(text or "").strip()
    clean = clean.replace("*", " ")
    clean = clean.replace("#", " ")
    clean = clean.replace("`", " ")
    clean = re.sub(r"\s+", " ", clean)
    return clean


def backend_login_required_message():
    return (
        "Backend login required hai. .env me VOICE_ASSISTANT_EMAIL aur "
        "VOICE_ASSISTANT_PASSWORD set kijiye."
    )


def create_porcupine():
    if pvporcupine is None:
        return None

    if not PORCUPINE_ACCESS_KEY:
        return None

    # Porcupine only supports specific keywords, not arbitrary words like "computer"
    # Use speech recognition fallback instead
    print("⚠️  Porcupine requires pre-built models. Using speech recognition fallback for custom wake words.")
    return None


def open_audio_stream(audio_handle, porcupine):
    """Re-open the microphone stream if the current one fails."""
    try:
        return audio_handle.open(
            rate=porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=porcupine.frame_length,
            input_device_index=None,
        )
    except Exception as exc:
        print("Mic open error:", exc)
        time.sleep(1)
        return open_audio_stream(audio_handle, porcupine)


def build_tts_engine():
    if VOICE_ENGINE == "say" and SAY_COMMAND:
        return {"mode": "say"}

    if pyttsx3 is None:
        return {"mode": "print"}

    engine = pyttsx3.init()
    try:
        engine.setProperty("rate", int(float(TTS_RATE)))
    except Exception:
        engine.setProperty("rate", 175)

    hindi_voice_id = None
    english_voice_id = None

    for voice in engine.getProperty("voices"):
        voice_name = str(getattr(voice, "name", "") or "")
        languages = str(getattr(voice, "languages", []) or "")
        lowered = f"{voice_name} {languages}".lower()

        if hindi_voice_id is None and ("hi" in lowered or "hindi" in lowered):
            hindi_voice_id = voice.id
        if english_voice_id is None and ("en" in lowered or "india" in lowered or "indian" in lowered):
            english_voice_id = voice.id

    return {
        "mode": "pyttsx3",
        "engine": engine,
        "hindi_voice_id": hindi_voice_id,
        "english_voice_id": english_voice_id,
    }


def speak(engine_bundle, text):
    clean_text = sanitize_tts_text(text)
    if not clean_text:
        return

    print("AI:", clean_text)

    try:
        if engine_bundle["mode"] == "say":
            voice = choose_tts_voice(clean_text)
            subprocess.run(
                [SAY_COMMAND, "-v", voice, "-r", TTS_RATE, clean_text],
                check=True,
            )
            return

        if engine_bundle["mode"] == "print":
            return

        engine = engine_bundle["engine"]
        voice_id = (
            engine_bundle.get("hindi_voice_id")
            if looks_hindi_text(clean_text)
            else engine_bundle.get("english_voice_id")
        )
        if voice_id:
            engine.setProperty("voice", voice_id)
        engine.say(clean_text)
        engine.runAndWait()
    except Exception as exc:
        print("TTS error:", exc)


def transcribe_audio(recognizer, audio, languages):
    if sr is None:
        return ""

    for language in languages:
        try:
            text = recognizer.recognize_google(audio, language=language)
            if text:
                return text.strip()
        except sr.UnknownValueError:
            continue
        except Exception as exc:
            print(f"Speech recognition error ({language}):", exc)
            continue
    return ""


def listen_command(recognizer):
    if sr is None:
        return ""

    with sr.Microphone() as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        print("Listening... boliye...")
        audio = recognizer.listen(
            source,
            timeout=COMMAND_TIMEOUT_SECONDS,
            phrase_time_limit=COMMAND_PHRASE_TIME_LIMIT,
        )

    text = transcribe_audio(recognizer, audio, ("hi-IN", "en-IN", "en-US"))
    if text:
        print("You said:", text)
    return text


def normalize_for_keyword(text):
    normalized = re.sub(r"[^0-9A-Za-z\u0900-\u097F ]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", normalized).strip()


def wake_word_matches(text):
    normalized = normalize_for_keyword(text)
    return any(alias in normalized for alias in WAKE_WORD_ALIASES)


def extract_command_after_wake_word(text):
    raw = str(text or "").strip()
    patterns = [
        rf"{re.escape(WAKE_WORD)}[\s,:-]+(.+)",
        r"computer[\s,:-]+(.+)",
        r"कंप्यूटर[\s,:-]+(.+)",
        r"कम्प्यूटर[\s,:-]+(.+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match and match.group(1):
            return match.group(1).strip()

    return ""


def wait_for_wake_word_with_porcupine(audio_stream, porcupine):
    while True:
        try:
            pcm_data = audio_stream.read(
                porcupine.frame_length,
                exception_on_overflow=False,
            )
        except Exception:
            print("Mic overflow, restarting stream")
            raise

        pcm = struct.unpack_from("h" * porcupine.frame_length, pcm_data)
        keyword_index = porcupine.process(pcm)

        if keyword_index >= 0:
            print(f"Wake word detected: {WAKE_WORD}")
            return ""

        time.sleep(0.01)


def wait_for_wake_word_with_speech(recognizer):
    if sr is None:
        print("SpeechRecognition is not installed.")
        return None

    print(f"🎤 Wake-word listening active. Say '{WAKE_WORD}' to activate assistant...")

    with sr.Microphone() as source:
        recognizer.adjust_for_ambient_noise(source, duration=1)

        while True:
            try:
                audio = recognizer.listen(
                    source,
                    timeout=None,
                    phrase_time_limit=WAKE_PHRASE_TIME_LIMIT,
                )
            except sr.RequestError as exc:
                print(f"❌ Microphone error: {exc}")
                time.sleep(1)
                continue
            except Exception as exc:
                print(f"⚠️  Wake-listen error: {exc}")
                time.sleep(0.5)
                continue

            transcript = transcribe_audio(recognizer, audio, ("en-IN", "hi-IN", "en-US"))
            if transcript:
                print(f"🎧 Heard: '{transcript}'")

            if wake_word_matches(transcript):
                print(f"✅ Wake word detected: {WAKE_WORD}")
                return extract_command_after_wake_word(transcript)


def create_backend_session():
    if requests is None:
        raise RuntimeError("The requests package is not installed.")
    session = requests.Session()
    session.headers.update(VOICE_ASSISTANT_HEADERS)
    return session


def login_backend_session(session):
    if not VOICE_ASSISTANT_EMAIL or not VOICE_ASSISTANT_PASSWORD:
        print("Backend login credentials not set. Assistant will use local loopback access.")
        return False

    try:
        response = request_backend(
            session,
            "POST",
            "/login",
            json={
                "email": VOICE_ASSISTANT_EMAIL,
                "password": VOICE_ASSISTANT_PASSWORD,
            },
            timeout=10,
        )
        payload = parse_json_safe(response)
        if response.ok:
            print(f"Backend login successful for {VOICE_ASSISTANT_EMAIL}")
            return True

        print("Backend login failed:", payload.get("error") or response.status_code)
        return False
    except Exception as exc:
        print("Backend login error:", exc)
        return False


def ask_backend(session, text):
    try:
        response = request_backend(
            session,
            "POST",
            "/assistant",
            json={
                "query": text,
                "source": "voice",
                "preferred_language": "hi-IN",
            },
            timeout=20,
        )
        data = parse_json_safe(response)
        if response.status_code == 401:
            return backend_login_required_message()
        response.raise_for_status()
        return data.get("reply", "Koi uttar nahin mila.")
    except Exception as exc:
        print("Backend error:", exc)
        return "Backend se connection nahin ho paya."


def check_camera_activity(session):
    """Check for interesting camera activity and return proactive message if any."""
    try:
        response = request_backend(
            session,
            "GET",
            "/get_alert",
            timeout=5,
        )
        if response.status_code == 401:
            return None
        if response.status_code == 200:
            data = parse_json_safe(response)
            faces = data.get("faces") or []
            alert = str(data.get("alert", "") or "")

            if any(not item.get("recognized", False) for item in faces):
                return "मैंने कैमरे में किसी अनजान व्यक्ति को देखा है। क्या आप देखना चाहेंगे?"
            if any(item.get("recognized", False) for item in faces):
                return "कैमरे में कोई known person दिखाई दे रहा है।"
            if "Person detected" in alert or "Unknown" in alert:
                return "मैंने कैमरे में किसी को देखा है। क्या आप हैं या कोई अतिथि आया है?"
            if "alert" in alert.lower() and "no" not in alert.lower():
                return f"कैमरे में कुछ activity hai: {alert}"
    except Exception as exc:
        print("Camera activity check error:", exc)
    return None


def suggest_action_based_on_behavior(session):
    """Suggest actions based on observed human behavior."""
    try:
        response = request_backend(
            session,
            "GET",
            "/behavior",
            timeout=5,
        )
        if response.status_code == 401:
            return None
        if response.status_code == 200:
            behavior = parse_json_safe(response)
            if behavior.get("person_present"):
                duration = behavior.get("person_duration", 0)
                activity = behavior.get("last_activity", "")
                if duration > 300:
                    return "आप काफी देर से यहाँ हैं, क्या मैं कोई music ya entertainment start करूँ?"
                if activity == "just_arrived":
                    return "नमस्ते, आपका स्वागत है। क्या मैं किसी काम में मदद करूँ?"
                if activity == "active":
                    return "आप busy लग रहे हैं। क्या कोई specific task hai?"
    except Exception as exc:
        print("Behavior check error:", exc)
    return None


def wants_exit(command):
    lowered = str(command or "").lower()
    return any(
        token in lowered
        for token in ("band", "stop", "close", "goodbye", "bye", "shutdown")
    )


def main():
    if requests is None:
        print("requests package missing. Install requirements before running voice_assistant.py.")
        return
    if sr is None:
        print("SpeechRecognition package missing. Install requirements before running voice_assistant.py.")
        return

    backend_session = create_backend_session()
    login_backend_session(backend_session)

    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = True
    recognizer.pause_threshold = 0.7
    recognizer.non_speaking_duration = 0.4
    engine = build_tts_engine()

    porcupine = None
    audio_handle = None
    audio_stream = None

    if PORCUPINE_ACCESS_KEY:
        try:
            porcupine = create_porcupine()
            if porcupine is not None:
                audio_handle = pyaudio.PyAudio()
                audio_stream = open_audio_stream(audio_handle, porcupine)
                print(f"🔔 Using Porcupine hardware acceleration for wake word: '{WAKE_WORD.upper()}'")
        except Exception as exc:
            print(f"❌ Porcupine initialization failed: {exc}")
            print("🔄 Switching to speech recognition fallback...")
            porcupine = None

    if porcupine is None:
        print(f"🎤 Voice Assistant ready in speech recognition mode.")
        print(f"   Say '{WAKE_WORD.upper()}' to activate the assistant.")

    try:
        while True:
            try:
                if porcupine is not None:
                    wake_command = wait_for_wake_word_with_porcupine(audio_stream, porcupine)
                else:
                    wake_command = wait_for_wake_word_with_speech(recognizer)
            except Exception:
                if porcupine is not None and audio_handle is not None:
                    audio_stream = open_audio_stream(audio_handle, porcupine)
                continue

            command = str(wake_command or "").strip()
            if not command:
                speak(engine, "जी, बोलिए।")
                command = listen_command(recognizer)
            if not command:
                speak(engine, "माफ़ कीजिए, मैं आपकी बात ठीक से समझ नहीं पाई।")
                continue

            if wants_exit(command):
                speak(engine, "ठीक है, मैं अब बंद हो रही हूँ।")
                break

            reply = ask_backend(backend_session, command)
            speak(engine, reply)

            proactive_msg = check_camera_activity(backend_session)
            if proactive_msg:
                time.sleep(1)
                speak(engine, proactive_msg)

                action_suggestion = suggest_action_based_on_behavior(backend_session)
                if action_suggestion:
                    time.sleep(0.5)
                    speak(engine, action_suggestion)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down voice assistant...")
    finally:
        try:
            if audio_stream is not None:
                audio_stream.close()
        except Exception:
            pass
        try:
            if audio_handle is not None:
                audio_handle.terminate()
        except Exception:
            pass
        try:
            if porcupine is not None:
                porcupine.delete()
        except Exception:
            pass


if __name__ == "__main__":
    main()
