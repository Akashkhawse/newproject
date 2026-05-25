# Railway Deployment Guide

This repository is ready for deployment on Railway as a Python web service.

## 1. Prepare the repository

1. Ensure your repo is pushed to GitHub.
2. Keep `PROCFILE` and `runtime.txt` in the project root.
3. Verify `requirements.txt` includes `gunicorn`.
4. Ensure `.env.example` includes all required environment variable names.

## 2. Railway setup

1. Open https://railway.app and sign in.
2. Create a new project and choose "Deploy from GitHub".
3. Select this repository.

## 3. Set the service type

Railway should detect a Python project.
If it does not, choose the `Python` service manually.

## 4. Configure the service

In Railway, configure:

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app --worker-class gthread --workers 1 --threads 8 --timeout 0 --bind 0.0.0.0:$PORT`

Railway also supports the `Procfile`, so it may use the service automatically.

> Use `--timeout 0` when you have long-lived streaming responses such as `/camera_feed`.

> Note: the `/camera_feed` endpoint is a long-lived streaming route, so an unlimited timeout is used. Keep one worker because the camera registry, stream frames, and mobile camera state are process-local; threads allow concurrent dashboard requests.

Use only the core dependencies in `requirements.txt` for Railway. Do not install optional voice or camera packages during the build.

## 5. Set environment variables

Create environment variables in Railway's settings using the values below.

Required:

- `FLASK_SECRET` — a strong random secret of at least 32 characters
- `APP_ENV=production`
- `PORT` — automatically set by Railway, but keep `5000` in `.env.example`
- `SESSION_COOKIE_SECURE=1`
- `ALLOW_LOCAL_VOICE_BYPASS=0`
- `DISABLE_CAMERA=1`
- `DISABLE_YOLO=1`

Optional / deploy-safe:

- `DISABLE_FACE_RECOGNITION=1`
- `APP_DB_PATH=/data/app.db` when using a Railway volume mounted at `/data`
- `SQLITE_TIMEOUT_SECONDS=30`
- `GEMINI_API_KEY` (only if you want Gemini AI replies)
- `GEMINI_MODEL_NAME=gemini-2.5-flash`
- `SMARTAI_BACKEND_URL=http://127.0.0.1:5000/assistant`
- `PORCUPINE_ACCESS_KEY=`
- `VOICE_ENGINE=say`

## 6. Start the deployment

1. Trigger deployment from Railway.
2. Wait for the build logs to finish.
3. Confirm the application starts successfully.

## 7. Post-deploy check

Open the Railway URL and verify the app renders.

If the site shows an error:

- Check logs in Railway for Python import/build errors.
- Make sure dependencies installed successfully.
- Ensure `FLASK_SECRET` is set.
- Confirm the app is running on the Railway-assigned port.

## 8. Notes

- `DISABLE_CAMERA=1` and `DISABLE_YOLO=1` are recommended for Railway because server environments typically do not have access to local cameras or heavy YOLO model inference.
- The app uses SQLite by default. Attach a Railway volume and set `APP_DB_PATH=/data/app.db` for persistence; use a managed database before scaling to multiple application replicas.
- `gunicorn` is used for production readiness.

## Optional improvement

If you later want a separate frontend and API deployment, you can host the static UI on Vercel and the Flask backend on Railway.
