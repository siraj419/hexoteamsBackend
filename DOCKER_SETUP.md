# Docker Setup Guide

This guide explains how to run the backend using Docker with MinIO (S3-compatible storage) and Redis.

## Prerequisites

- Docker and Docker Compose installed
- A `.env` file with required configuration

## Services

The `docker-compose.yml` includes:
- **Backend**: FastAPI application
- **Celery Worker**: Background task processor for async operations (emails, notifications, etc.)
- **Redis**: Caching service and message broker (internal only, not exposed publicly)
- **MinIO**: S3-compatible object storage

## Environment Variables

Update your `.env` file with the following variables:

### Required Variables (must be set)

```env
# Supabase
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_key

# OAuth
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
GITHUB_CLIENT_ID=your_github_client_id
GITHUB_CLIENT_SECRET=your_github_client_secret

# Email Settings
SMTP_HOST=your_smtp_host
SMTP_PORT=587
SMTP_USERNAME=your_smtp_username
SMTP_PASSWORD=your_smtp_password
SMTP_USE_TLS=true
SMTP_USE_STARTTLS=true
FROM_EMAIL=your_email@example.com
FROM_NAME=Your Name
```

### Important: MinIO/S3 Variables in Docker

**DO NOT** set these variables in your `.env` file when using Docker, as they are automatically configured:

```env
# ❌ DO NOT ADD THESE TO .env WHEN USING DOCKER:
# REDIS_URL=... (will be overridden)
# S3_ENDPOINT_URL=... (will be overridden)
# AWS_ACCESS_KEY_ID=... (will be overridden)
# AWS_SECRET_ACCESS_KEY=... (will be overridden)
# S3_USE_SSL=... (will be overridden)
# S3_BUCKET_NAME=... (will be overridden)
# AWS_REGION=... (will be overridden)
```

These are automatically set by docker-compose.yml:
- `REDIS_URL=redis://redis:6379/0`
- `S3_ENDPOINT_URL=http://minio:9000`
- `AWS_ACCESS_KEY_ID=minioadmin`
- `AWS_SECRET_ACCESS_KEY=minioadmin`
- `S3_USE_SSL=false`
- `S3_BUCKET_NAME=team-management`
- `AWS_REGION=us-east-1`

**Note**: If you have these variables in your `.env` file with different values (e.g., real AWS credentials), they may cause connection issues with MinIO. Either remove them from `.env` or ensure they match the MinIO credentials above.

### For Local Development (without Docker)

If running locally without Docker, use:

```env
# Redis (local instance)
REDIS_URL=redis://localhost:6379/0

# MinIO (if running locally) or AWS S3
S3_ENDPOINT_URL=http://localhost:9000
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin
S3_USE_SSL=false
S3_BUCKET_NAME=team-management
AWS_REGION=us-east-1

# Or use AWS S3 (set S3_ENDPOINT_URL to empty/null)
# AWS_ACCESS_KEY_ID=your_aws_key
# AWS_SECRET_ACCESS_KEY=your_aws_secret
# S3_ENDPOINT_URL=
# S3_USE_SSL=true
```

## Running with Docker

1. **Build and start services:**
   ```powershell
   docker-compose up -d
   ```

2. **View logs:**
   ```powershell
   # View all services
   docker-compose logs -f
   
   # View specific service
   docker-compose logs -f backend
   docker-compose logs -f celery_worker
   ```

3. **Stop services:**
   ```powershell
   docker-compose down
   ```

4. **Stop and remove volumes (clean slate):**
   ```powershell
   docker-compose down -v
   ```

## Accessing Services

- **Backend API**: http://localhost:8002
- **MinIO Console**: http://localhost:9001 (username: `minioadmin`, password: `minioadmin`)
- **Redis**: Internal only (accessible from backend container at `redis:6379`)

## MinIO Setup

On first run, MinIO will start with default credentials:
- Access Key: `minioadmin`
- Secret Key: `minioadmin`

The backend will automatically create the bucket specified in `S3_BUCKET_NAME` if it doesn't exist.

To change MinIO credentials, update the `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` environment variables in `docker-compose.yml` and update your `.env` file accordingly.

## Notes

- Redis is **not exposed publicly** - it's only accessible within the Docker network
- Celery worker processes background tasks (emails, notifications) asynchronously
- MinIO data persists in a Docker volume
- Redis data persists in a Docker volume (used for both caching and Celery message broker)
- The backend and Celery worker containers mount the code directory for development (hot reload)

