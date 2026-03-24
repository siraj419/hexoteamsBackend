from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import UUID4
from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError

from app.db.sync_session import SyncSessionLocal
from app.models import Attachment, File, ProjectMember, Task, TaskComment
from app.schemas.attachments import (
    AttachmentGetPaginatedResponse,
    AttachmentResponse,
    AttachmentType,
)
from app.services.files import FilesService
from app.utils import calculate_file_size
from app.utils.redis_cache import ProjectSummaryCache, cache_service
from app.utils.sa_pagination import apply_sa_limit_offset


class AttachmentService:
    CACHE_TTL_ATTACHMENTS = 180

    def __init__(self, files_service: FilesService):
        self.files_service = files_service

    def add_attachment(
        self,
        entity_type: AttachmentType,
        entity_id: UUID4,
        file_id: UUID4,
    ) -> AttachmentResponse:
        now = datetime.now(timezone.utc)
        try:
            db = SyncSessionLocal()
            try:
                row = Attachment(
                    entity_type=entity_type.value,
                    entity_id=UUID(str(entity_id)),
                    file_id=UUID(str(file_id)),
                    created_at=now,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
            except IntegrityError as e:
                db.rollback()
                code = getattr(e.orig, "pgcode", None)
                if code == "23503":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Failed to add attachment for file {file_id}, invalid file id",
                    )
                if code == "23505":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Failed to add attachment for {entity_type.value}: {entity_id}, file already attached",
                    )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to add attachment: {e!s}",
                )
            finally:
                db.close()
        except HTTPException:
            raise

        file_info = self.files_service.get_file(file_id)
        cache_service.invalidate_pattern(f"attachments:list:{entity_type.value}:{entity_id}:*")
        if entity_type == AttachmentType.PROJECT:
            ProjectSummaryCache.delete_summary(str(entity_id))

        return AttachmentResponse(
            id=row.id,
            file_id=file_id,
            file_name=file_info.name,
            file_size=file_info.size,
            content_type=file_info.content_type or "",
        )

    def get_attachment_file_url(self, attachment_id: UUID4) -> str:
        try:
            db = SyncSessionLocal()
            try:
                fid = db.execute(
                    select(Attachment.file_id).where(Attachment.id == UUID(str(attachment_id)))
                ).scalar_one_or_none()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get attachment file url: {e!s}",
            )

        if not fid:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Attachment not found",
            )

        return self.files_service.get_file_url(fid)

    def get_attachments(
        self,
        entity_type: AttachmentType,
        entity_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> AttachmentGetPaginatedResponse:
        cache_key = f"attachments:list:{entity_type.value}:{entity_id}:{limit}:{offset}"
        cached = cache_service.get(cache_key)
        if cached:
            return AttachmentGetPaginatedResponse(**cached)

        eid = UUID(str(entity_id))
        att_filter = and_(
            Attachment.entity_type == entity_type.value,
            Attachment.entity_id == eid,
        )

        try:
            db = SyncSessionLocal()
            try:
                total_count = int(
                    db.execute(select(func.count()).select_from(Attachment).where(att_filter)).scalar_one()
                )
                stmt = (
                    select(Attachment, File.name, File.size_bytes, File.content_type)
                    .join(File, File.id == Attachment.file_id)
                    .where(att_filter)
                    .order_by(Attachment.created_at.desc())
                )
                _, off, stmt = apply_sa_limit_offset(stmt, limit, offset)
                rows = db.execute(stmt).all()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get attachments: {e!s}",
            )

        attachments = [
            AttachmentResponse(
                id=att.id,
                file_id=att.file_id,
                file_name=fname,
                file_size=calculate_file_size(fsize),
                content_type=ctype or "",
            )
            for att, fname, fsize, ctype in rows
        ]

        result = AttachmentGetPaginatedResponse(
            attachments=attachments,
            total=total_count,
            offset=off,
            limit=limit,
        )
        cache_service.set(cache_key, result.model_dump(mode="json"), ttl=self.CACHE_TTL_ATTACHMENTS)
        return result

    def delete_attachment(self, attachment_id: UUID4) -> bool:
        attachment_data = None
        deleted = False
        try:
            db = SyncSessionLocal()
            try:
                row = db.execute(
                    select(Attachment.entity_id, Attachment.entity_type).where(
                        Attachment.id == UUID(str(attachment_id))
                    )
                ).first()
                if row:
                    attachment_data = {"entity_id": row[0], "entity_type": row[1]}
                r = db.execute(
                    delete(Attachment)
                    .where(Attachment.id == UUID(str(attachment_id)))
                    .returning(Attachment.id)
                )
                deleted = r.first() is not None
                db.commit()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete attachment: {e!s}",
            )

        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Attachment not found",
            )

        if attachment_data:
            cache_service.invalidate_pattern(
                f"attachments:list:{attachment_data['entity_type']}:{attachment_data['entity_id']}:*"
            )
            if attachment_data["entity_type"] == AttachmentType.PROJECT.value:
                ProjectSummaryCache.delete_summary(str(attachment_data["entity_id"]))

        return True

    def delete_all(self, entity_id: UUID4, entity_type: AttachmentType) -> bool:
        eid = UUID(str(entity_id))
        try:
            db = SyncSessionLocal()
            try:
                db.execute(
                    delete(Attachment).where(
                        Attachment.entity_id == eid,
                        Attachment.entity_type == entity_type.value,
                    )
                )
                db.commit()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all attachments: {e!s}",
            )

        cache_service.invalidate_pattern(f"attachments:list:{entity_type.value}:{entity_id}:*")
        if entity_type == AttachmentType.PROJECT:
            ProjectSummaryCache.delete_summary(str(entity_id))
        return True

    def get_comment_attachment_download_url(self, attachment_id: UUID4, user_id: UUID4) -> dict:
        try:
            db = SyncSessionLocal()
            try:
                att = db.execute(
                    select(Attachment).where(Attachment.id == UUID(str(attachment_id)))
                ).scalar_one_or_none()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get attachment: {e!s}",
            )

        if not att:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Attachment not found",
            )

        if att.entity_type != AttachmentType.COMMENT.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This endpoint is only for comment attachments",
            )

        try:
            db = SyncSessionLocal()
            try:
                task_id = db.execute(
                    select(TaskComment.task_id).where(TaskComment.id == att.entity_id)
                ).scalar_one_or_none()
                if not task_id:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Comment not found",
                    )
                project_id = db.execute(
                    select(Task.project_id).where(Task.id == task_id)
                ).scalar_one_or_none()
                if not project_id:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Task not found",
                    )
                is_member = (
                    db.execute(
                        select(ProjectMember.id).where(
                            ProjectMember.project_id == project_id,
                            ProjectMember.user_id == UUID(str(user_id)),
                        ).limit(1)
                    ).first()
                    is not None
                )
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify project membership: {e!s}",
            )

        if not is_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only project members can download comment attachments",
            )

        download_url = self.files_service.get_file_url(att.file_id)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        return {"download_url": download_url, "expires_at": expires_at}

    def get_task_attachment_download_url(self, attachment_id: UUID4, user_id: UUID4) -> dict:
        try:
            db = SyncSessionLocal()
            try:
                att = db.execute(
                    select(Attachment).where(Attachment.id == UUID(str(attachment_id)))
                ).scalar_one_or_none()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get attachment: {e!s}",
            )

        if not att:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Attachment not found",
            )

        if att.entity_type != AttachmentType.TASk.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This endpoint is only for task attachments",
            )

        try:
            db = SyncSessionLocal()
            try:
                project_id = db.execute(
                    select(Task.project_id).where(Task.id == att.entity_id)
                ).scalar_one_or_none()
                if not project_id:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Task not found",
                    )
                is_member = (
                    db.execute(
                        select(ProjectMember.id).where(
                            ProjectMember.project_id == project_id,
                            ProjectMember.user_id == UUID(str(user_id)),
                        ).limit(1)
                    ).first()
                    is not None
                )
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify project membership: {e!s}",
            )

        if not is_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only project members can download task attachments",
            )

        download_url = self.files_service.get_file_url(att.file_id)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        return {"download_url": download_url, "expires_at": expires_at}
