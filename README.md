# HexoTeams Backend

FastAPI API for HexoTeams (workspaces, projects, tasks, chat, auth, background jobs).

**Stack:** Python 3.13+, PostgreSQL, Redis (for cache/Celery).

Default port: **`APP_PORT`** in `.env` (often **8002**). Interactive docs: **`http://localhost:8002/docs`**.

---

## Environment variables (`.env`)

1. In the **`hexoteamsBackend`** folder, copy **`example.env`** to **`.env`**.
2. Edit **`.env`** with real secrets and URLs. The app loads these automatically on startup (do not commit **`.env`** to git).

| Area | What to set |
|------|-------------|
| **App** | `APP_PORT` — HTTP port for the API. `FRONTEND_URL` — public URL of the web app (used in emails and links). |
| **Database** | `DATABASE_URL` — PostgreSQL connection string. Use the `postgresql+asyncpg://user:password@host:port/database` form shown in `example.env`. |
| **JWT** | `JWT_SECRET_KEY` — long random string (at least 32 characters). Other `JWT_*` keys control token lifetimes; defaults in `example.env` are fine to start. |
| **AWS / S3** | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `S3_BUCKET_NAME` — file uploads and presigned URLs. Optional `S3_ENDPOINT_URL` for MinIO or similar. |
| **SMTP** | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_USE_STARTTLS`, `FROM_EMAIL`, `FROM_NAME` — outgoing mail (signup verification, password reset, invitations). |
| **Redis** | `REDIS_URL` — e.g. `redis://localhost:6379/0` for caching, Celery, and realtime helpers. |
| **Optional** | `EMAIL_TEMPLATES_PATH`, pagination limits, invitation expiry, comment/subtask depth — usually leave as in `example.env` unless you need to change behavior. |

For a full list of keys and placeholders, open **`example.env`** in this repo.

---

## Run with pip

From this folder (`hexoteamsBackend`):

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt
python main.py
```

Or:

```bash
uvicorn main:app --host 0.0.0.0 --port 8002 --reload
```

---

## Run with uv

From this folder:

```bash
uv venv
uv sync
uv run python main.py
```

Or:

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8002 --reload
```

After changing `pyproject.toml`, run **`uv lock`** if you use a lockfile.

---

## Database migrations

Run from this folder with `.env` available (same as when you start the app):

```bash
alembic upgrade head
alembic revision --autogenerate -m "your message"
alembic downgrade -1
alembic current
```

---

## Celery worker

From this folder, with the same venv and `.env` as the API:

```bash
celery -A app.core.celery worker --loglevel=info
```

Redis must be running if you use `REDIS_URL` from `.env`. Restart the worker after code changes.
