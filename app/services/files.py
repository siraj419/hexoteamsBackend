import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID as PyUUID

from fastapi import HTTPException, UploadFile, status
from pydantic import UUID4
from app.utils.uuid_compat import as_uuid
from sqlalchemy import and_, delete, func, select, update

from app.core import settings
from app.core.s3 import S3ServiceException, s3_service
from app.db.sync_session import SyncSessionLocal
from app.models import ChatAttachment, ChatMessage, DirectMessage, File as FileModel, Profile, ProjectMember
from app.schemas.files import (
    FileBaseResponse,
    FileBaseResponseWithUploaderId,
    FileGetPaginatedResponseWithUploaders,
    FileGetResponseWithUser,
    FileUploadedByUserGetResponse,
)
from app.utils import calculate_file_size
from app.utils.redis_cache import UserCache, cache_service
from app.utils.sa_pagination import apply_sa_limit_offset

logger = logging.getLogger(__name__)

class FilesService:
    CACHE_TTL_FILE = 300  # 5 minutes for single file
    CACHE_TTL_FILE_LIST = 180  # 3 minutes for file lists
    
    def __init__(self):
        self.s3_service = s3_service

    def upload_file(
        self,
        file: UploadFile,
        user_id: UUID4,
        org_id: Optional[UUID4] = None,
        project_id: Optional[UUID4] = None,
        task_id: Optional[UUID4] = None,
    ) -> FileBaseResponse:
        if not self.s3_service.validate_file_extension(file.filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file extension")

        if not self.s3_service.validate_file_size(file.size):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File size exceeds the maximum allowed size",
            )

        now = datetime.now(timezone.utc)
        row = FileModel(
            name=file.filename or "unnamed",
            size_bytes=file.size or 0,
            content_type=file.content_type,
            uploaded_by=PyUUID(str(user_id)),
            org_id=PyUUID(str(org_id)) if org_id else None,
            project_id=PyUUID(str(project_id)) if project_id else None,
            task_id=PyUUID(str(task_id)) if task_id else None,
            created_at=now,
        )
        db = SyncSessionLocal()
        try:
            db.add(row)
            db.commit()
            db.refresh(row)
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create file record in database: {e!s}",
            )
        finally:
            db.close()

        file_extension = os.path.splitext(file.filename or "")[1]
        file_id_str = str(row.id)
        s3_key = f"{file_id_str}{file_extension}"
        file.file.seek(0)
        try:
            self.s3_service.upload_file(
                file=file.file,
                key=s3_key,
                content_type=file.content_type,
            )
        except (S3ServiceException, Exception) as e:
            db = SyncSessionLocal()
            try:
                db.delete(row)
                db.commit()
            except Exception as delete_err:
                logger.error("Failed to delete file record after upload failure: %s", delete_err)
            finally:
                db.close()
            logger.error("Failed to upload file to S3/MinIO: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload file to storage. Please try again later.",
            )

        db = SyncSessionLocal()
        try:
            has_profile = (
                db.execute(
                    select(Profile.id).where(Profile.user_id == PyUUID(str(user_id)))
                ).first()
                is not None
            )
        finally:
            db.close()
        if not has_profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User profile not found",
            )

        file_response = FileBaseResponse(
            id=row.id,
            name=row.name,
            size=calculate_file_size(row.size_bytes),
            content_type=row.content_type,
            uploaded_by=row.uploaded_by,
            is_deleted=row.is_deleted,
        )
        cache_service.invalidate_pattern("files:list:*")
        return file_response
    
    def update_file(self, file_id: UUID4, file: UploadFile) -> Dict[str, Any]:
        if not self.s3_service.validate_file_extension(file.filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file extension")

        if not self.s3_service.validate_file_size(file.size):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File size exceeds the maximum allowed size",
            )

        fid = PyUUID(str(file_id))
        db = SyncSessionLocal()
        try:
            row = db.get(FileModel, fid)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="File not found or update failed",
                )
            row.name = file.filename or row.name
            row.size_bytes = file.size or 0
            row.content_type = file.content_type
            db.commit()
            db.refresh(row)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update file record from database: {e!s}",
            )
        finally:
            db.close()

        file_extension = os.path.splitext(file.filename or "")[1]
        file_id_str = str(row.id)
        s3_key = f"{file_id_str}{file_extension}"
        file.file.seek(0)
        try:
            self.s3_service.upload_file(
                file=file.file,
                key=s3_key,
                content_type=file.content_type,
            )
        except (S3ServiceException, Exception) as e:
            logger.error("Failed to upload file to S3/MinIO: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload file to storage. Please try again later.",
            )

        cache_service.delete(f"file:{file_id_str}")
        cache_service.invalidate_pattern("files:list:*")
        return {
            "id": file_id_str,
            "name": row.name,
            "size_bytes": row.size_bytes,
            "content_type": row.content_type,
            "uploaded_by": str(row.uploaded_by),
            "is_deleted": row.is_deleted,
        }

    def update_file_metadata(
        self,
        file_id: UUID4,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> FileBaseResponse:
        """
        Update file metadata (name and/or content_type) without uploading a new file.
        """
        updates = {}
        
        if file_name is not None:
            updates["name"] = file_name
        
        if content_type is not None:
            updates["content_type"] = content_type
        
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one field (file_name or content_type) must be provided"
            )
        
        updates["updated_at"] = datetime.now(timezone.utc)
        fid = PyUUID(str(file_id))
        db = SyncSessionLocal()
        try:
            row = db.get(FileModel, fid)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="File not found",
                )
            if file_name is not None:
                row.name = file_name
            if content_type is not None:
                row.content_type = content_type
            row.updated_at = updates["updated_at"]
            db.commit()
            db.refresh(row)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update file metadata: {e!s}",
            )
        finally:
            db.close()

        self._get_user_profile(as_uuid(str(row.uploaded_by)))

        file_response = FileBaseResponse(
            id=row.id,
            name=row.name,
            size=calculate_file_size(row.size_bytes),
            uploaded_by=row.uploaded_by,
            is_deleted=row.is_deleted,
            content_type=row.content_type,
        )
        
        # Invalidate file caches
        cache_service.delete(f"file:{file_id}")
        cache_service.invalidate_pattern("files:list:*")
        
        return file_response
    
    def update_file_project_id(self, file_id: UUID4, project_id: UUID4) -> Dict[str, Any]:
        fid = PyUUID(str(file_id))
        pid = PyUUID(str(project_id))
        db = SyncSessionLocal()
        try:
            row = db.get(FileModel, fid)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="File not found",
                )
            row.project_id = pid
            db.commit()
            db.refresh(row)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update file project id: {e!s}",
            )
        finally:
            db.close()
        return {
            "id": str(row.id),
            "project_id": str(row.project_id) if row.project_id else None,
        }

    def delete_file(self, file_id: UUID4) -> bool:
        now = datetime.now(timezone.utc)
        fid = PyUUID(str(file_id))
        db = SyncSessionLocal()
        try:
            row = db.get(FileModel, fid)
            if row:
                row.is_deleted = True
                row.deleted_at = now
                db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete file record from database: {e!s}",
            )
        finally:
            db.close()

        cache_service.delete(f"file:{file_id}")
        cache_service.invalidate_pattern("files:list:*")
        return True

    def delete_file_permanently(self, file_id: UUID4) -> bool:
        file_data = self.get_file(file_id)
        file_extension = os.path.splitext(file_data.name)[1]
        s3_key = f"{str(file_id)}{file_extension}"
        try:
            self.s3_service.delete_file(s3_key)
        except (S3ServiceException, Exception) as e:
            logger.error("Failed to delete file from S3/MinIO: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete file from storage. Please try again later.",
            )

        fid = PyUUID(str(file_id))
        db = SyncSessionLocal()
        try:
            row = db.get(FileModel, fid)
            if row:
                db.delete(row)
                db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete file record from database: {e!s}",
            )
        finally:
            db.close()

        return True

    def restore_file(self, file_id: UUID4) -> bool:
        fid = PyUUID(str(file_id))
        db = SyncSessionLocal()
        try:
            row = db.get(FileModel, fid)
            if row:
                row.is_deleted = False
                row.deleted_at = None
                db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to restore file record from database: {e!s}",
            )
        finally:
            db.close()

        cache_service.delete(f"file:{file_id}")
        cache_service.invalidate_pattern("files:list:*")
        return True

    def get_files(self, 
            user_id: Optional[UUID4] = None,
            org_id: Optional[UUID4] = None,
            project_id: Optional[UUID4] = None,
            is_deleted: Optional[bool] = None,
            limit: Optional[int] = None,
            offset: Optional[int] = None
    ) -> FileGetPaginatedResponseWithUploaders:
        """
        Get files with optimized uploader information.
        
        Returns:
            - files: List of files with uploaded_by_id field
            - uploaders: Dictionary mapping user_id -> uploader profile
        
        """
        # Build cache key
        cache_key = f"files:list:{user_id}:{org_id}:{project_id}:{is_deleted}:{limit}:{offset}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached:
            return FileGetPaginatedResponseWithUploaders(**cached)
        
        conditions = []
        if user_id:
            conditions.append(FileModel.uploaded_by == PyUUID(str(user_id)))
        if org_id:
            conditions.append(FileModel.org_id == PyUUID(str(org_id)))
        if project_id:
            conditions.append(FileModel.project_id == PyUUID(str(project_id)))
        if is_deleted:
            conditions.append(FileModel.is_deleted.is_(True))
        conditions.append(FileModel.upload_status != "pending")

        base = select(FileModel).order_by(FileModel.created_at.desc())
        if conditions:
            base = base.where(and_(*conditions))

        db = SyncSessionLocal()
        try:
            count_stmt = select(func.count()).select_from(FileModel)
            if conditions:
                count_stmt = count_stmt.where(and_(*conditions))
            total = int(db.execute(count_stmt).scalar_one())
            _, off, page_stmt = apply_sa_limit_offset(base, limit, offset)
            rows = db.execute(page_stmt).scalars().all()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get files from database: {e!s}",
            )
        finally:
            db.close()

        files_data = []
        unique_uploader_ids = set()
        for frow in rows:
            unique_uploader_ids.add(frow.uploaded_by)
            files_data.append(
                FileBaseResponse(
                    id=frow.id,
                    name=frow.name,
                    size=calculate_file_size(frow.size_bytes),
                    content_type=frow.content_type,
                    uploaded_by=frow.uploaded_by,
                    is_deleted=frow.is_deleted,
                )
            )

        uploaders_dict: Dict[PyUUID, FileUploadedByUserGetResponse] = {}
        if unique_uploader_ids:
            db = SyncSessionLocal()
            try:
                profs = db.execute(
                    select(Profile).where(Profile.user_id.in_(list(unique_uploader_ids)))
                ).scalars().all()
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to get uploader profiles: {e!s}",
                )
            finally:
                db.close()
            for profile in profs:
                avatar_url = None
                if profile.avatar_file_id:
                    try:
                        avatar_url = self.get_file_url(profile.avatar_file_id)
                    except Exception:
                        pass
                uploaders_dict[profile.user_id] = FileUploadedByUserGetResponse(
                    id=profile.user_id,
                    display_name=profile.display_name or "",
                    avatar_url=avatar_url,
                )

        result = FileGetPaginatedResponseWithUploaders(
            files=files_data,
            uploaders=uploaders_dict,
            total=total,
            limit=limit,
            offset=off,
        )
        
        # Cache the result
        cache_service.set(cache_key, result.model_dump(mode='json'), ttl=self.CACHE_TTL_FILE_LIST)
        
        return result

    def get_file(self, file_id: UUID4) -> FileBaseResponse:
        cache_key = f"file:{file_id}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached:
            return FileBaseResponse(**cached)
        
        fid = PyUUID(str(file_id))
        db = SyncSessionLocal()
        try:
            row = db.get(FileModel, fid)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get file record from database: {e!s}",
            )
        finally:
            db.close()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found",
            )

        file_data = FileBaseResponse(
            id=row.id,
            name=row.name,
            size=calculate_file_size(row.size_bytes),
            content_type=row.content_type,
            uploaded_by=row.uploaded_by,
            is_deleted=row.is_deleted,
        )
        
        # Cache the result
        cache_service.set(cache_key, file_data.model_dump(mode='json'), ttl=self.CACHE_TTL_FILE)
        
        return file_data

    def get_file_url(self, file_id: UUID4) -> str:
        # Get file metadata to extract the extension
        file_data = self.get_file(file_id)
        
        # Extract extension from filename
        file_extension = os.path.splitext(file_data.name)[1]
        s3_key = f"{str(file_id)}{file_extension}"
        
        # Generate presigned URL with the correct S3 key
        try:
            file_url = self.s3_service.generate_presigned_url(s3_key)
        except (S3ServiceException, Exception) as e:
            logger.error(f"Failed to generate presigned URL from S3/MinIO: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate file URL. Please try again later."
            )
        
        return file_url
    
    def get_file_with_url(self, file_id: UUID4) :
        file_data = self.get_file(file_id)
        file_url = self.get_file_url(file_id)
        
        return FileGetResponseWithUser(
            id=file_data.id,
            name=file_data.name,
            size=file_data.size,
            content_type=file_data.content_type,
            is_deleted=file_data.is_deleted,
            file_url=file_url,
            uploaded_by=self._get_user_profile(file_data.uploaded_by),
        )
    
    def delete_permanently_all_files(self, org_id: UUID4) -> bool:
        
        # delete the files from s3 (construct keys with extensions)
        files_response = self.get_files(org_id=org_id)
        files_keys = [f"{file.id}{os.path.splitext(file.name)[1]}" for file in files_response.files]
        try:
            self.s3_service.delete_files(files_keys)
        except (S3ServiceException, Exception) as e:
            logger.error(f"Failed to delete files from S3/MinIO: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete files from storage. Please try again later."
            )
        
        db = SyncSessionLocal()
        try:
            db.execute(delete(FileModel).where(FileModel.org_id == PyUUID(str(org_id))))
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all files from database: {e!s}",
            )
        finally:
            db.close()

        return True
    
    def delete_permanently_all_files_by_project_id(self, project_id: UUID4) -> bool:
        """
        Delete all file records associated with a project from the database.
        Note: This does NOT delete files from S3/MinIO storage.
        """
        db = SyncSessionLocal()
        try:
            db.execute(delete(FileModel).where(FileModel.project_id == PyUUID(str(project_id))))
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all files from database: {e!s}",
            )
        finally:
            db.close()

        return True

    def validate_file_extension(self, filename: str) -> bool:
        return self.s3_service.validate_file_extension(filename)
    
    def validate_file_size(self, size: int) -> bool:
        return self.s3_service.validate_file_size(size)
    
    def _get_user_profile(self, user_id: UUID4) -> FileUploadedByUserGetResponse:
        # Use UserCache for consistency
        cached_user = UserCache.get_user(str(user_id))
        if cached_user and isinstance(cached_user, dict):
            avatar_url = None
            avatar_file_id = cached_user.get('avatar_file_id')
            if avatar_file_id:
                try:
                    avatar_url = self.get_file_url(as_uuid(avatar_file_id))
                except HTTPException:
                    pass
            
            # Handle both 'id' and 'user_id' keys for cache compatibility
            # Fallback to user_id parameter if neither key exists
            user_id_value = cached_user.get('id') or cached_user.get('user_id')
            if not user_id_value:
                # If cache doesn't have id, use the provided user_id
                user_id_value = str(user_id)
            
            display_name = cached_user.get('display_name', '')
            
            return FileUploadedByUserGetResponse(
                id=as_uuid(user_id_value),
                display_name=display_name,
                avatar_url=avatar_url,
            )
        
        db = SyncSessionLocal()
        try:
            profile = db.execute(
                select(Profile).where(Profile.user_id == PyUUID(str(user_id)))
            ).scalar_one_or_none()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user profile: {e!s}",
            )
        finally:
            db.close()

        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User profile not found",
            )
        avatar_url = None
        if profile.avatar_file_id:
            try:
                avatar_url = self.get_file_url(profile.avatar_file_id)
            except HTTPException:
                pass

        user_data_for_cache = {
            "id": str(profile.user_id),
            "display_name": profile.display_name,
            "email": profile.email,
            "avatar_file_id": str(profile.avatar_file_id) if profile.avatar_file_id else None,
        }
        UserCache.set_user(str(user_id), user_data_for_cache)

        return FileUploadedByUserGetResponse(
            id=profile.user_id,
            display_name=profile.display_name or "",
            avatar_url=avatar_url,
        )
    
    def check_uploaded_by_user(self, file_id: UUID4, user_id: UUID4) -> bool:
        db = SyncSessionLocal()
        try:
            uid = db.execute(
                select(FileModel.uploaded_by).where(FileModel.id == PyUUID(str(file_id)))
            ).scalar_one_or_none()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check file ownership: {e!s}",
            )
        finally:
            db.close()
        if not uid:
            return False
        return uid == PyUUID(str(user_id))
    
    def upload_chat_attachment(
        self,
        file_content: bytes,
        file_name: str,
        content_type: str,
        user_id: UUID4,
        organization_id: UUID4,
        chat_type: str,
        reference_id: UUID4
    ) -> Dict[str, Any]:
        """
        Upload a chat attachment to S3 and create database record
        
        Args:
            file_content: The file content as bytes
            file_name: Original file name
            content_type: MIME type
            user_id: User uploading the file
            organization_id: Organization ID
            chat_type: 'project' or 'direct'
            reference_id: Project ID or conversation ID
            
        Returns:
            Dict with attachment metadata
        """
        try:
            attachment_id = uuid.uuid4()
            file_size = len(file_content)
            if not self.validate_file_size(file_size):
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File size exceeds maximum limit of {settings.S3_MAX_FILE_SIZE_MB} MB",
                )

            file_extension = os.path.splitext(file_name)[1]
            
            year = datetime.now(timezone.utc).strftime('%Y')
            month = datetime.now(timezone.utc).strftime('%m')
            
            if chat_type == 'project':
                s3_key = f"chat-attachments/organizations/{organization_id}/projects/{reference_id}/{year}/{month}/{attachment_id}{file_extension}"
            else:
                s3_key = f"chat-attachments/organizations/{organization_id}/direct/{reference_id}/{year}/{month}/{attachment_id}{file_extension}"
            
            try:
                from io import BytesIO
                file_obj = BytesIO(file_content)
                self.s3_service.upload_file(
                    file=file_obj,
                    key=s3_key,
                    content_type=content_type
                )
            except (S3ServiceException, Exception) as e:
                logger.error(f"Failed to upload chat attachment to S3/MinIO: {str(e)}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to upload file to storage. Please try again later."
                )
            
            thumbnail_path = None
            thumbnail_url = None
            if content_type.startswith('image/'):
                try:
                    thumbnail_path = self._generate_thumbnail(file_content, attachment_id, file_extension)
                    if thumbnail_path:
                        # Generate presigned URL for thumbnail
                        thumbnail_url = self.s3_service.generate_presigned_url(thumbnail_path)
                except Exception as e:
                    logger.warning(f"Failed to generate thumbnail: {str(e)}")
                    pass
            
            ca = ChatAttachment(
                id=attachment_id,
                message_id=None,
                message_type=chat_type,
                file_name=file_name,
                file_size=file_size,
                file_type=content_type,
                storage_path=s3_key,
                thumbnail_path=thumbnail_path,
                uploaded_by=PyUUID(str(user_id)),
                created_at=datetime.now(timezone.utc),
            )
            db = SyncSessionLocal()
            try:
                db.add(ca)
                db.commit()
            except Exception as e:
                db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to create attachment record: {e!s}",
                )
            finally:
                db.close()
            
            return {
                'attachment_id': attachment_id,
                'file_name': file_name,
                'file_size': calculate_file_size(file_size),
                'file_type': content_type,
                'thumbnail_url': thumbnail_url
            }
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to upload chat attachment: {str(e)}"
            )
    
    def get_chat_attachment_details(
        self,
        attachment_id: UUID4,
        user_id: UUID4
    ) -> Dict[str, Any]:
        """
        Get chat attachment details
        
        Args:
            attachment_id: The attachment ID
            user_id: The requesting user ID
            
        Returns:
            Dict with attachment details (attachment_id, file_name, file_size, file_type, thumbnail_url)
        """
        try:
            db = SyncSessionLocal()
            try:
                attachment = db.execute(
                    select(ChatAttachment).where(ChatAttachment.id == PyUUID(str(attachment_id)))
                ).scalar_one_or_none()
            finally:
                db.close()

            if not attachment:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Attachment not found",
                )

            if not attachment.message_id:
                if str(attachment.uploaded_by) != str(user_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied to this attachment",
                    )
            else:
                self._verify_attachment_access(attachment, user_id)
            
            # Generate thumbnail URL if thumbnail exists
            thumbnail_url = None
            if attachment.thumbnail_path:
                try:
                    thumbnail_url = self.s3_service.generate_presigned_url(
                        attachment.thumbnail_path,
                        expiration=3600,
                    )
                except (S3ServiceException, Exception) as e:
                    logger.warning("Failed to generate thumbnail URL: %s", e)

            return {
                "attachment_id": attachment_id,
                "file_name": attachment.file_name,
                "file_size": calculate_file_size(attachment.file_size),
                "file_type": attachment.file_type,
                "thumbnail_url": thumbnail_url,
            }
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get attachment details: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to get attachment details. Please try again later."
            )
    
    def get_chat_attachment_download_url(
        self,
        attachment_id: UUID4,
        user_id: UUID4
    ) -> Dict[str, Any]:
        """
        Get signed URL for chat attachment download
        
        Args:
            attachment_id: The attachment ID
            user_id: The requesting user ID
            
        Returns:
            Dict with download URL and expiration
        """
        try:
            db = SyncSessionLocal()
            try:
                att = db.execute(
                    select(ChatAttachment).where(ChatAttachment.id == PyUUID(str(attachment_id)))
                ).scalar_one_or_none()
            finally:
                db.close()

            if not att:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Attachment not found",
                )

            self._verify_attachment_access(att, user_id)

            try:
                download_url = self.s3_service.generate_presigned_url(
                    att.storage_path,
                    expiration=900,
                )
            except (S3ServiceException, Exception) as e:
                logger.error("Failed to generate presigned URL from S3/MinIO: %s", e)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to generate download URL. Please try again later.",
                )

            return {
                "download_url": download_url,
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
            }
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get download URL: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to get download URL. Please try again later."
            )
    
    def delete_chat_attachment(self, attachment_id: UUID4, user_id: UUID4) -> bool:
        """
        Delete a chat attachment
        
        Args:
            attachment_id: The attachment ID
            user_id: The user deleting the attachment
            
        Returns:
            bool: True if successful
        """
        try:
            db = SyncSessionLocal()
            try:
                attachment = db.execute(
                    select(ChatAttachment).where(ChatAttachment.id == PyUUID(str(attachment_id)))
                ).scalar_one_or_none()
                if not attachment:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Attachment not found",
                    )
                if str(attachment.uploaded_by) != str(user_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You can only delete your own attachments",
                    )
                try:
                    self.s3_service.delete_file(attachment.storage_path)
                except (S3ServiceException, Exception) as e:
                    logger.warning("Failed to delete attachment file from S3/MinIO: %s", e)
                if attachment.thumbnail_path:
                    try:
                        self.s3_service.delete_file(attachment.thumbnail_path)
                    except (S3ServiceException, Exception) as e:
                        logger.warning("Failed to delete thumbnail from S3/MinIO: %s", e)
                db.delete(attachment)
                db.commit()
            except HTTPException:
                raise
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

            return True
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete attachment: {str(e)}"
            )
    
    def _generate_thumbnail(self, file_content: bytes, attachment_id: UUID4, file_extension: str) -> Optional[str]:
        """
        Generate thumbnail for image attachments
        
        Args:
            file_content: The image content
            attachment_id: The attachment ID
            file_extension: The file extension
            
        Returns:
            Optional[str]: S3 path to thumbnail or None
        """
        try:
            from PIL import Image
            from io import BytesIO
            
            img = Image.open(BytesIO(file_content))
            
            # Convert RGBA (or other modes with alpha) to RGB before saving as JPEG
            # JPEG doesn't support transparency
            if img.mode in ('RGBA', 'LA', 'P'):
                # Create a white background for transparent images
                rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = rgb_img
            elif img.mode != 'RGB':
                # Convert other modes (like grayscale) to RGB
                img = img.convert('RGB')
            
            img.thumbnail((256, 256), Image.Resampling.LANCZOS)
            
            thumb_buffer = BytesIO()
            img.save(thumb_buffer, format='JPEG', quality=85)
            thumb_buffer.seek(0)
            
            thumb_key = f"chat-attachments/thumbnails/{attachment_id}_thumb.jpg"
            
            try:
                self.s3_service.upload_file(
                    file=thumb_buffer,
                    key=thumb_key,
                    content_type='image/jpeg'
                )
            except (S3ServiceException, Exception) as e:
                logger.warning(f"Failed to upload thumbnail to S3/MinIO: {str(e)}")
                return None
            
            return thumb_key

        except Exception:
            return None

    def _verify_attachment_access(self, attachment: ChatAttachment, user_id: UUID4) -> None:
        if not attachment.message_id:
            return

        message_type = attachment.message_type
        mid = attachment.message_id
        uid = PyUUID(str(user_id))

        try:
            db = SyncSessionLocal()
            try:
                if message_type == "project":
                    pid = db.execute(
                        select(ChatMessage.project_id).where(ChatMessage.id == mid)
                    ).scalar_one_or_none()
                    if not pid:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Message not found",
                        )
                    ok = (
                        db.execute(
                            select(ProjectMember.id).where(
                                ProjectMember.project_id == pid,
                                ProjectMember.user_id == uid,
                            ).limit(1)
                        ).first()
                        is not None
                    )
                    if not ok:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Access denied to this attachment",
                        )
                else:
                    dm = db.execute(
                        select(DirectMessage).where(DirectMessage.id == mid)
                    ).scalar_one_or_none()
                    if not dm:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Message not found",
                        )
                    if dm.sender_id != uid and dm.receiver_id != uid:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Access denied to this attachment",
                        )
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify attachment access: {e!s}",
            )