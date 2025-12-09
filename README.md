# SmartAI Dashboard

SmartAI is a local Flask dashboard for:

- session-based login and registration
- admin/user roles backed by SQLite
- editable user profile data
- system health monitoring
- camera streaming with optional YOLO detection
- alert history
- add, toggle, and delete device controls stored in SQLite
- Gemini-backed assistant replies
- manual, hybrid, AI, and research assistant modes
- in-browser wake-word listening for `computer`
- admin activity logs and user role management

## Project structure

- `backend/` contains the Flask backend and API/session logic.
- `templates/` contains the HTML frontend views.
- `static/js/` contains browser-side frontend behavior.
- `static/css/` contains frontend styling.
- `.env.example` contains a working local environment template.

## Quick start

1. Create a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install app dependencies:

   ```bash
   python3 -m pip install --upgrade pip
   python3 -m pip install -r requirements.txt
   ```

3. Copy the example environment file:

   ```bash
   cp .env.example .env
   ```

4. Set at least:

   - `FLASK_SECRET` for session signing
   - `SESSION_COOKIE_SECURE=0` for local HTTP development unless you are serving over HTTPS
   - optional `GEMINI_API_KEY` if you want AI replies
   - optional `DISABLE_CAMERA=1` if you do not want local camera access
   - optional `DISABLE_YOLO=1` if you want a lighter startup
   - optional `DISABLE_FACE_RECOGNITION=1` if you want to skip known-face matching

5. Run the app:

   ```bash
   python3 app.py
   ```

6. Open `http://127.0.0.1:5000`

On the first run, the app redirects you to `/register`. After a user exists, unauthenticated access redirects to `/login`.
The first registered account becomes the admin account automatically.

## Environment notes

- `APP_DB_PATH` lets you point Flask at a different SQLite file. This is mainly useful for tests.
- `YOLO_MODEL_PATH` defaults to `yolo11n.pt`.
- `KNOWN_FACES_DIR` defaults to `known_faces`.
- `FACE_RECOGNITION_THRESHOLD` defaults to `70`.
- `SMARTAI_BACKEND_URL`, `PORCUPINE_ACCESS_KEY`, and `WAKE_WORD` are only used by `voice_assistant.py`.

## Camera and YOLO

- The server camera feed uses OpenCV.
- YOLO detection loads only when Ultralytics is installed, not disabled, and the model file exists.
- Face recognition trains from `known_faces/<person_name>/*.jpg|*.jpeg|*.png`.
- If no valid face training images are available, face recognition stays disabled without breaking the camera feed.
- If the camera is unavailable, the dashboard falls back to a placeholder stream instead of crashing.

## Assistant modes

- `Hybrid` uses local/manual commands first and falls back to Gemini when configured.
- `Manual` handles local commands such as time, system health, device list, and `turn on light`.
- `AI` sends conversational questions to Gemini with SmartAI system context.
- `Research` returns a structured local system summary and uses Gemini for a more analytical answer when available.
- The dashboard can keep a browser wake-word listener armed so saying `computer` triggers command capture.

## Admin features

- The first user is promoted to `admin`.
- Admins can open the `Admin` tab to view user accounts, recent activity, and promote users to admin.
- User roles and activity history are stored in SQLite alongside devices.

## Voice assistant script

`voice_assistant.py` is optional. Its Python dependencies are already listed in `requirements.txt`, but the microphone and local audio stack still need to be available on your machine.

Before running it, set:

- `PORCUPINE_ACCESS_KEY`
- optional `SMARTAI_BACKEND_URL`
- optional `WAKE_WORD`
- `VOICE_ASSISTANT_EMAIL` and `VOICE_ASSISTANT_PASSWORD` so the voice assistant can log in to the protected Flask backend
- optional `VOICE_ENGINE=say` on macOS for better voice quality
- optional `HINDI_TTS_VOICE=Lekha` and `ENGLISH_TTS_VOICE=Rishi`
- optional `TTS_RATE=175`

Then run:

```bash
python3 voice_assistant.py
```

Notes:

- By default the wake word is `computer`.
- If `PORCUPINE_ACCESS_KEY` is missing, the script falls back to speech-based wake word detection and still listens for `computer`.
- On macOS, `VOICE_ENGINE=say` usually sounds better than `pyttsx3` for Hindi and Indian English voices.

## Running tests

The smoke tests use Flask's `test_client`, a temporary SQLite DB, and disable camera/YOLO during test startup.

```bash
python3 -m unittest discover -s tests
```

## Troubleshooting

- If `python3 app.py` fails with missing packages, activate your virtual environment first.
- If the browser camera prompt does not appear, check browser permissions.
- If Gemini is not configured, the assistant endpoint returns a clear fallback message instead of failing.
