import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID as PyUUID

from fastapi import HTTPException, status
from pydantic import UUID4
from sqlalchemy import select

from app.core import settings
from app.core.s3 import S3_PRESIGNED_POST_EXPIRATION_SECONDS, S3ServiceException, s3_service
from app.db.sync_session import SyncSessionLocal
from app.models import File as FileModel, Profile
from app.schemas.files import FileBaseResponse
from app.utils import calculate_file_size
from app.utils.redis_cache import cache_service

logger = logging.getLogger(__name__)

UPLOAD_STATUS_PENDING = "pending"
UPLOAD_STATUS_UPLOADED = "uploaded"


class UploadsService:
    def request_presigned_post(
        self,
        filename: str,
        content_type: str,
        user_id: UUID4,
        org_id: UUID4,
    ) -> Dict[str, Any]:
        safe_name = os.path.basename(filename.strip()) or "unnamed"
        if not s3_service.validate_file_extension(safe_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid file extension",
            )
        if not s3_service.validate_mime_matches_extension(safe_name, content_type):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Content-Type is not allowed for this file type",
            )

        db = SyncSessionLocal()
        try:
            has_profile = (
                db.execute(
                    select(Profile.id).where(Profile.user_id == PyUUID(str(user_id)))
                ).first()
                is not None
            )
            if not has_profile:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User profile not found",
                )
        finally:
            db.close()

        file_extension = os.path.splitext(safe_name)[1]
        now = datetime.now(timezone.utc)
        row = FileModel(
            name=safe_name,
            size_bytes=0,
            content_type=content_type.strip(),
            uploaded_by=PyUUID(str(user_id)),
            org_id=PyUUID(str(org_id)),
            project_id=None,
            task_id=None,
            created_at=now,
            upload_status=UPLOAD_STATUS_PENDING,
        )
        db = SyncSessionLocal()
        try:
            db.add(row)
            db.commit()
            db.refresh(row)
        except Exception as e:
            db.rollback()
            logger.error("Failed to create pending file record: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create file record",
            )
        finally:
            db.close()

        file_id = row.id
        s3_key = f"{file_id}{file_extension}"
        try:
            post = s3_service.generate_presigned_post(
                key=s3_key,
                content_type=content_type.strip(),
            )
        except S3ServiceException as e:
            db = SyncSessionLocal()
            try:
                persisted = db.get(FileModel, file_id)
                if persisted is not None:
                    db.delete(persisted)
                    db.commit()
            except Exception as del_err:
                logger.error("Failed to roll back file row after presign error: %s", del_err)
                db.rollback()
            finally:
                db.close()
            logger.error("Presigned POST generation failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to prepare upload. Please try again later.",
            )

        max_bytes = settings.S3_MAX_FILE_SIZE_MB * 1024 * 1024
        return {
            "url": post["url"],
            "fields": post["fields"],
            "file_id": file_id,
            "s3_key": s3_key,
            "expires_in_seconds": S3_PRESIGNED_POST_EXPIRATION_SECONDS,
            "max_file_size_bytes": max_bytes,
        }

    def confirm_upload(self, file_id: UUID4, user_id: UUID4, org_id: UUID4) -> FileBaseResponse:
        fid = PyUUID(str(file_id))
        uid = PyUUID(str(user_id))
        oid = PyUUID(str(org_id))
        db = SyncSessionLocal()
        try:
            row = db.get(FileModel, fid)
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
            if row.uploaded_by != uid:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You cannot confirm this upload",
                )
            if row.org_id != oid:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="File does not belong to this organization",
                )
            if row.upload_status == UPLOAD_STATUS_UPLOADED:
                return FileBaseResponse(
                    id=row.id,
                    name=row.name,
                    size=calculate_file_size(row.size_bytes),
                    content_type=row.content_type or "",
                    uploaded_by=row.uploaded_by,
                    is_deleted=row.is_deleted,
                )
            if row.upload_status != UPLOAD_STATUS_PENDING:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="File is not awaiting upload confirmation",
                )

            ext = os.path.splitext(row.name)[1]
            s3_key = f"{row.id}{ext}"
            try:
                meta = s3_service.get_file_metadata(s3_key)
            except S3ServiceException as e:
                logger.error("HEAD failed for upload confirm %s: %s", s3_key, e)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Upload was not found in storage. It may have failed or expired.",
                )

            size_b = int(meta.get("size") or 0)
            if not s3_service.validate_file_size(size_b):
                try:
                    s3_service.delete_file(s3_key)
                except S3ServiceException:
                    pass
                db.delete(row)
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded file exceeds the maximum allowed size",
                )

            ct = (meta.get("content_type") or row.content_type or "").strip()
            row.size_bytes = size_b
            row.content_type = ct or row.content_type
            row.upload_status = UPLOAD_STATUS_UPLOADED
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(row)
            cache_service.invalidate_pattern("files:list:*")
            result = FileBaseResponse(
                id=row.id,
                name=row.name,
                size=calculate_file_size(row.size_bytes),
                content_type=row.content_type or "",
                uploaded_by=row.uploaded_by,
                is_deleted=row.is_deleted,
            )
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            logger.error("confirm_upload failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to finalize upload",
            )
        finally:
            db.close()

        return result
