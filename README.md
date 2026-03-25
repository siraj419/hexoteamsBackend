# HexoTeams Backend

FastAPI API for HexoTeams (workspaces, projects, tasks, chat, auth, background jobs).

**Stack:** Python 3.13+, PostgreSQL, Redis (for cache/Celery).

Default port: **`APP_PORT`** in `.env` (often **8002**). Interactive docs: **`http://localhost:8002/docs`**.

---

## Environment variables (`.env`)

1. In the **`hexoteamsBackend`** folder, copy **`example.env`** to **`.env`**.
2. Edit **`.env`** with real secrets and URLs. The app loads these automatically on startup (do not commit **`.env`** to git).

**App:** Set `APP_PORT` for the HTTP port. Set `FRONTEND_URL` to the public URL of the web app (emails and links use this).

**Database:** Set `DATABASE_URL` to your PostgreSQL URL. Follow the `postgresql+asyncpg://user:password@host:port/database` pattern in `example.env`.

**JWT:** Set `JWT_SECRET_KEY` to a long random value (at least 32 characters). The other `JWT_*` variables control token lifetimes; the defaults in `example.env` are usually enough to start.

**AWS / S3:** Set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, and `S3_BUCKET_NAME` for uploads and presigned URLs. Use `S3_ENDPOINT_URL` if you use MinIO or another S3-compatible endpoint.

**SMTP:** Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_USE_STARTTLS`, `FROM_EMAIL`, and `FROM_NAME` for outgoing mail (verification, password reset, invitations).

**Redis:** Set `REDIS_URL` (for example `redis://localhost:6379/0`) for caching, Celery, and realtime helpers.

**Optional:** Values such as `EMAIL_TEMPLATES_PATH`, pagination limits, invitation expiry, and comment/subtask depth are in `example.env`; change them only if you need different behavior.

For every key and placeholder, see **`example.env`** in this repo.

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
