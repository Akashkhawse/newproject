# voice_assistant.py

import os
import struct
import time

import pvporcupine
import pyaudio
import requests
import speech_recognition as sr
import pyttsx3
from dotenv import load_dotenv

# -------------------------------------------------
# Load env vars  (.env me PORCUPINE_KEY optional)
# -------------------------------------------------
load_dotenv()

WAKE_WORD = "computer"
PORCUPINE_KEY = os.getenv(
    "PORCUPINE_KEY",
    "ngTf/Ijs7meReDNUEUl/tboTxSGUEyJ/B+f5BFwYYONMDFM1zMDFVQ=="
)  # agar .env me ho to waha se lega

BACKEND_URL = os.getenv(
    "ASSISTANT_URL",
    "http://127.0.0.1:5000/assistant"
)

# -------------------------------------------------
# Init Porcupine wake-word
# -------------------------------------------------
porcupine = pvporcupine.create(
    access_key=PORCUPINE_KEY,
    keywords=[WAKE_WORD]
)

# -------------------------------------------------
# Audio (mic) setup
# -------------------------------------------------
pa = pyaudio.PyAudio()

# device_index = None → default mic; agar issue ho to number set karo
DEVICE_INDEX = None

audio_stream = pa.open(
    rate=porcupine.sample_rate,
    channels=1,
    format=pyaudio.paInt16,
    input=True,
    input_device_index=DEVICE_INDEX,
    frames_per_buffer=porcupine.frame_length
)

# -------------------------------------------------
# SpeechRecognition + Text-to-Speech
# -------------------------------------------------
recognizer = sr.Recognizer()
engine = pyttsx3.init()
engine.setProperty("rate", 175)


def speak(text: str):
    """AI ka reply bolna + terminal me print karna."""
    if not text:
        text = "Sorry, I have no response."
    print("AI:", text)
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        print("TTS error:", e)


def listen_command() -> str:
    """Wake word ke baad command sunna (Hindi + English mix)."""
    try:
        with sr.Microphone(device_index=DEVICE_INDEX) as source:
            print("🎤 Listening after wake word...")
            recognizer.adjust_for_ambient_noise(source, duration=0.6)
            audio = recognizer.listen(source)
        try:
            text = recognizer.recognize_google(audio, language="hi-IN")
            print("You said:", text)
            return text
        except Exception as e:
            print("Speech recognition error:", e)
            return ""
    except Exception as e:
        print("Mic error:", e)
        return ""


def ask_backend(text: str) -> str:
    """
    Flask backend ke /assistant route ko call karega.
    Yahi se Gemini / local logic ka reply aayega (app.py).
    """
    try:
        res = requests.post(
            BACKEND_URL,
            json={"query": text},
            timeout=20
        )
        res.raise_for_status()
        data = res.json()
        return data.get("reply", "No reply from backend.")
    except Exception as e:
        return f"Backend error: {e}"


print("🤖 Local Voice Agent ready... bolo 'Computer' to wake me up!")

try:
    while True:
        # Mic se raw audio read
        pcm = audio_stream.read(
            porcupine.frame_length,
            exception_on_overflow=False
        )
        pcm_unpacked = struct.unpack_from(
            "h" * porcupine.frame_length,
            pcm
        )

        # Wake-word detection
        keyword_index = porcupine.process(pcm_unpacked)
        if keyword_index >= 0:
            print("✅ Wake word detected: COMPUTER")
            speak("Yes Akash, I am listening.")
            command = listen_command()

            if not command:
                speak("Sorry, I did not catch that.")
                continue

            lower = command.lower()

            # Local simple stop command
            if "band" in lower or "stop listening" in lower or "shut down" in lower:
                speak("Okay, stopping voice assistant.")
                break

            # Backend (Flask + Gemini)
            reply = ask_backend(command)
            speak(reply)

        # Thoda chhota delay buffer overflow avoid ke liye
        time.sleep(0.01)

except KeyboardInterrupt:
    print("❌ Exiting voice assistant...")

finally:
    try:
        if audio_stream is not None:
            audio_stream.close()
    except Exception:
        pass
    try:
        pa.terminate()
    except Exception:
        pass
    try:
        porcupine.delete()
    except Exception:
        pass
    print("🔻 Clean exit.")