# HexoTeams Backend

FastAPI service for HexoTeams: organizations, projects, tasks, chat, files (S3), auth (JWT), and background jobs (Celery). The HTTP API is mounted under **`/api`** (for example, **`/api/v1/auth/login`**).

## Requirements

- **Python** 3.13+ (see `.python-version` if you use pyenv/uv)
- **PostgreSQL** (with extensions as needed; see migrations)
- **Redis** (caching, Celery broker, realtime notification fan-out)
- **SMTP** and **AWS S3** credentials for full mail and upload flows (see `example.env`)

## Project layout

```
hexoteamsBackend/
├── main.py                 # FastAPI app entry (uvicorn target: main:app)
├── alembic.ini             # Alembic config
├── alembic/                # Migration env + versions/
├── scripts/                # Operational scripts (e.g. wipe_all_data.py)
├── app/
│   ├── core/               # Settings, Celery app, security/JWT, email, S3 helpers
│   ├── db/                 # SQLAlchemy Base, async engine, sync session, Alembic URL helpers
│   ├── models/             # ORM models (Postgres)
│   ├── schemas/            # Pydantic request/response models by domain
│   ├── routers/            # FastAPI routes (v1 API)
│   ├── services/           # Business logic (used by routers)
│   ├── tasks/              # Celery task definitions
│   ├── templates/          # HTML email templates
│   └── utils/              # Redis cache, WebSockets, pagination helpers, etc.
├── example.env             # Copy to .env and fill in values
├── requirements.txt        # pip install (kept in sync with pyproject.toml)
└── pyproject.toml          # uv / PEP 621 dependencies
```

## Configuration

1. Copy `example.env` to **`.env`** in this directory (same folder as `main.py`).
2. Set at least **`DATABASE_URL`**, **`JWT_SECRET_KEY`** (32+ characters), AWS/S3, SMTP, and **`REDIS_URL`** as needed.
3. **`DATABASE_URL`** must be async-style for the app, e.g. `postgresql+asyncpg://user:pass@host:5432/dbname`. Legacy `postgres://` URLs are normalized on startup.

The API listens on **`APP_PORT`** (default **8002**).

---

## Run with pip

From the **`hexoteamsBackend`** directory:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt
```

Run the API:

```bash
python main.py
# or
uvicorn main:app --host 0.0.0.0 --port 8002 --reload
```

Base URL: `http://localhost:8002`  
OpenAPI docs: `http://localhost:8002/docs`  
Health: `http://localhost:8002/health`

---

## Run with uv

From the **`hexoteamsBackend`** directory:

```bash
uv venv
uv sync
# or: uv pip install -r requirements.txt
```

Activate the venv if needed, then:

```bash
uv run python main.py
# or
uv run uvicorn main:app --host 0.0.0.0 --port 8002 --reload
```

If you use a lockfile, run **`uv lock`** after changing `pyproject.toml`.

---

## Database migrations (Alembic)

Alembic uses the **sync** Postgres URL derived from **`DATABASE_URL`** (same as `SyncSessionLocal`). Run commands from **`hexoteamsBackend`** with **`.env` loaded** (or variables exported) so `Settings` resolves.

**PostgreSQL UUID defaults:** If migrations reference `uuid_generate_v4()`, enable the extension once on the database:

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

(On PostgreSQL 13+ you can alternatively standardize on `gen_random_uuid()` in migrations/models.)

Common commands:

```bash
# Create a new revision from model changes
alembic revision --autogenerate -m "describe change"

# Apply all pending migrations
alembic upgrade head

# Roll back one revision
alembic downgrade -1

# Show current revision
alembic current
```

If the database already matches the models and you only need Alembic’s history aligned:

```bash
alembic stamp head
```

---

## Celery (background tasks)

Celery uses **`REDIS_URL`** from settings as **broker and result backend** (`app/core/celery.py`). Email notifications, signup verification, password reset emails, and similar work are queued as tasks in **`app/tasks/tasks.py`**.

Start a worker from **`hexoteamsBackend`** (same venv and env as the API):

```bash
celery -A app.core.celery worker --loglevel=info
```

Useful checks (broker must be reachable):

```bash
celery -A app.core.celery inspect registered
```

Restart workers after code changes. Without a running worker and Redis, queued jobs will not execute.

---

## Optional scripts

- **`scripts/wipe_all_data.py`** — Truncates all ORM-mapped tables (destructive; dev/staging only). See the script docstring for flags.

---

## API prefix

Routes are registered as:

- `main.py` → `app.include_router(base_router, prefix="/api")`
- `app/routers/__init__.py` → v1 router at **`/v1`**

So versioned endpoints live under **`/api/v1/...`**. Configure the frontend **`VITE_API_URL`** (or equivalent) to include `/api` if your client expects that base path (e.g. `http://localhost:8002/api`).
