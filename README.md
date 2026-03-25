# HexoTeams Backend

FastAPI API for HexoTeams (workspaces, projects, tasks, chat, auth, background jobs).

**Stack:** Python 3.13+, PostgreSQL, Redis (for cache/Celery). Copy **`example.env`** to **`.env`** and fill in values before running.

Default port: **`APP_PORT`** in `.env` (often **8002**). Interactive docs: **`http://localhost:8002/docs`**.

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
