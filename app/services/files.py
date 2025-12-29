from fastapi import HTTPException, status
from pydantic import UUID4
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from supabase_auth.errors import AuthApiError
import os
import logging

from app.core import supabase
from app.core.s3 import s3_service, S3ServiceException
from fastapi import UploadFile

logger = logging.getLogger(__name__)

from app.schemas.files import (
    FileBaseResponse,
    FileBaseResponseWithUploaderId,
    FileUploadedByUserGetResponse,
    FileGetResponseWithUser,
    FileGetPaginatedResponseWithUploaders,
)
from app.utils import calculate_file_size, apply_pagination

class FilesService:
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
        # validate the file
        if not self.s3_service.validate_file_extension(file.filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file extension")
        
        if not self.s3_service.validate_file_size(file.size):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File size exceeds the maximum allowed size")
            
        # store the file in the database
        try:
            response = supabase.table("files").insert({
                "name": file.filename,
                "size_bytes": file.size,
                "content_type": file.content_type,
                "uploaded_by": str(user_id),
                "org_id": str(org_id) if org_id else None,
                "project_id": str(project_id) if project_id else None,
                "task_id": str(task_id) if task_id else None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create file record in database: {e}"
            )
        
        # Extract file extension from original filename
        file_extension = os.path.splitext(file.filename)[1]  # Gets extension with dot (e.g., '.jpg')
        file_id = str(response.data[0]["id"])
        s3_key = f"{file_id}{file_extension}"
        
        # reset file pointer to beginning (in case validations read it)
        file.file.seek(0)
        
        # upload the file to S3 with extension
        try:
            self.s3_service.upload_file(
                file=file.file,
                key=s3_key,
                content_type=file.content_type
            )
        except (S3ServiceException, Exception) as e:
            # If upload fails, delete the database record that was inserted
            try:
                supabase.table("files").delete().eq("id", file_id).execute()
                logger.warning(f"Deleted file record {file_id} after failed S3 upload: {str(e)}")
            except Exception as delete_err:
                logger.error(f"Failed to delete file record {file_id} after upload failure: {str(delete_err)}")
            
            # Return 500 error instead of raising exception
            logger.error(f"Failed to upload file to S3/MinIO: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload file to storage. Please try again later."
            )
        
        # get user profile
        try:
            user_profile = supabase.table("profiles").select("display_name").eq("user_id", response.data[0]['uploaded_by']).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user profile: {e}"
            )
        
        if not user_profile.data or len(user_profile.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User profile not found"
            )
        
        return FileBaseResponse(
            id=response.data[0]['id'],
            name=response.data[0]['name'],
            size=calculate_file_size(response.data[0]['size_bytes']),
            content_type=response.data[0]['content_type'],
            uploaded_by=response.data[0]['uploaded_by'],
            is_deleted=response.data[0]['is_deleted'],
        )
    
    def update_file(self, file_id: UUID4, file: UploadFile) -> Dict[str, Any]:
        # validate the file
        if not self.s3_service.validate_file_extension(file.filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file extension")
        
        if not self.s3_service.validate_file_size(file.size):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File size exceeds the maximum allowed size")
        
        try:
            response = supabase.table("files").update({
                "name": file.filename,
                "size_bytes": file.size,
                "content_type": file.content_type,
            }).eq("id", str(file_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update file record from database: {e}"
            )
        
        # Extract file extension from original filename
        file_extension = os.path.splitext(file.filename)[1]  # Gets extension with dot (e.g., '.jpg')
        file_id = str(response.data[0]["id"])
        s3_key = f"{file_id}{file_extension}"
        
        # reset file pointer to beginning (in case validations read it)
        file.file.seek(0)
            
        # upload the file to S3 with extension
        try:
            self.s3_service.upload_file(
                file=file.file,
                key=s3_key,
                content_type=file.content_type
            )
        except (S3ServiceException, Exception) as e:
            logger.error(f"Failed to upload file to S3/MinIO: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload file to storage. Please try again later."
            )
        
        return response.data[0]

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
        
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        try:
            response = supabase.table("files").update(updates).eq("id", str(file_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update file metadata: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found"
            )
        
        file_data = response.data[0]
        
        # Get user profile for response
        uploaded_by = self._get_user_profile(UUID4(file_data["uploaded_by"]))
        
        return FileBaseResponse(
            id=UUID4(file_data["id"]),
            name=file_data["name"],
            size=calculate_file_size(file_data["size_bytes"]),
            uploaded_by=UUID4(file_data["uploaded_by"]),
            is_deleted=file_data.get("is_deleted", False),
            content_type=file_data["content_type"],
        )
    
    def update_file_project_id(self, file_id: UUID4, project_id: UUID4) -> Dict[str, Any]:
        try:
            response = supabase.table("files").update({
                "project_id": str(project_id),
            }).eq("id", str(file_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update file project id: {e}"
            )
        
        return response.data[0]

    def delete_file(self, file_id: UUID4) -> bool:
        
        try:
            supabase.table("files").update({
                "is_deleted": True,
                "deleted_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", str(file_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete file record from database: {e}"
            )
        
        return True
    
    def delete_file_permanently(self, file_id: UUID4) -> bool:
        
        # Get file metadata to extract the extension
        file_data = self.get_file(file_id)
        file_extension = os.path.splitext(file_data.name)[1]
        s3_key = f"{str(file_id)}{file_extension}"
        
        # delete the file from s3
        try:
            self.s3_service.delete_file(s3_key)
        except (S3ServiceException, Exception) as e:
            logger.error(f"Failed to delete file from S3/MinIO: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete file from storage. Please try again later."
            )
        
        # delete the file record from the database
        try:
            supabase.table("files").delete().eq("id", str(file_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete file record from database: {e}"
            )
        
        return True
    
    def restore_file(self, file_id: UUID4) -> bool:
        try:
            file_data =supabase.table("files").update({
                "is_deleted": False,
                "deleted_at": None,
            }).eq("id", str(file_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to restore file record from database: {e}"
            )
        
        return file_data.data[0]

    def get_files(self, 
            user_id: Optional[UUID4] = None,
            org_id: Optional[UUID4] = None,
            project_id: Optional[UUID4] = None,
            is_deleted: Optional[bool] = None,
            limit: Optional[int] = None,
            offset: Optional[int] = None
    ) -> tuple[List[FileBaseResponseWithUploaderId], Dict[str, FileUploadedByUserGetResponse]]:
        """
        Get files with optimized uploader information.
        
        Returns:
            - files: List of files with uploaded_by_id field
            - uploaders: Dictionary mapping user_id -> uploader profile
        
        """
        
        query = supabase.table("files").select(
            "id, name, size_bytes, content_type, uploaded_by, org_id, project_id, is_deleted, created_at",
            count="exact"
        )
        if user_id:
            query = query.eq("uploaded_by", str(user_id))
        if org_id:
            query = query.eq("org_id", str(org_id))
        if project_id:
            query = query.eq("project_id", str(project_id))
        if is_deleted:
            query = query.eq("is_deleted", is_deleted)
        
        # apply the pagination
        limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get files from database: {e}"
            )
        
        files_data = []
        unique_uploader_ids = set()
        
        for file in response.data:
            uploader_id = file['uploaded_by']
            unique_uploader_ids.add(uploader_id)
            
            files_data.append(FileBaseResponse(
                id=file['id'],
                name=file['name'],
                size=calculate_file_size(file['size_bytes']),
                content_type=file['content_type'],
                uploaded_by=file['uploaded_by'],
                is_deleted=file['is_deleted'],
            ))
        
        uploaders_dict = {}
        if unique_uploader_ids:
            try:
                profiles_response = supabase.table("profiles").select(
                    "user_id, display_name, avatar_file_id"
                ).in_("user_id", list(unique_uploader_ids)).execute()
                
                for profile in profiles_response.data:
                    avatar_url = None
                    if profile.get('avatar_file_id'):
                        try:
                            avatar_url = self.get_file_url(profile['avatar_file_id'])
                        except:
                            pass
                    
                    uploaders_dict[profile['user_id']] = FileUploadedByUserGetResponse(
                        id=profile['user_id'],
                        display_name=profile['display_name'],
                        avatar_url=avatar_url,
                    )
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to get uploader profiles: {e}"
                )
        
        return FileGetPaginatedResponseWithUploaders(
            files=files_data,
            uploaders=uploaders_dict,
            total=response.count,
            limit=limit,
            offset=offset,
        )

    def get_file(self, file_id: UUID4) -> FileBaseResponse:
        try:
            response = supabase.table("files").select(
                "id, name, size_bytes, content_type, uploaded_by, is_deleted"
            ).eq("id", str(file_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get file record from database: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found"
            )
        
        return FileBaseResponse(
            id=response.data[0]['id'],
            name=response.data[0]['name'],
            size=calculate_file_size(response.data[0]['size_bytes']),
            content_type=response.data[0]['content_type'],
            uploaded_by=response.data[0]['uploaded_by'],
            is_deleted=response.data[0]['is_deleted'],
        )

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
        files, _ = self.get_files(org_id=org_id)
        files_keys = [f"{file.id}{os.path.splitext(file.name)[1]}" for file in files]
        try:
            self.s3_service.delete_files(files_keys)
        except (S3ServiceException, Exception) as e:
            logger.error(f"Failed to delete files from S3/MinIO: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete files from storage. Please try again later."
            )
        
        # delete the files from the database
        try:
            supabase.table("files").delete().eq("org_id", str(org_id)).execute()
        except AuthApiError as e:
            raise HTTPException( 
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all files from database: {e}"
            )
        
        return True
    
    def delete_permanently_all_files_by_project_id(self, project_id: UUID4) -> bool:
        """
        Delete all file records associated with a project from the database.
        Note: This does NOT delete files from S3/MinIO storage.
        """
        try:
            supabase.table("files").delete().eq("project_id", str(project_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all files from database: {e}"
            )
        
        return True

    def validate_file_extension(self, filename: str) -> bool:
        return self.s3_service.validate_file_extension(filename)
    
    def validate_file_size(self, size: int) -> bool:
        return self.s3_service.validate_file_size(size)
    
    def _get_user_profile(self, user_id: UUID4) -> FileUploadedByUserGetResponse:
        try:
            response = supabase.table("profiles").select("display_name, avatar_file_id").eq("user_id", str(user_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user profile: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User profile not found"
            )
        
        avatar_url = None
        if response.data[0]['avatar_file_id']:
            try:
                avatar_url = self.get_file_url(response.data[0]['avatar_file_id'])
            except HTTPException:
                pass
        
        
        return FileUploadedByUserGetResponse(
            id=user_id,
            display_name=response.data[0]['display_name'],
            avatar_url=avatar_url,
        )
    
    def check_uploaded_by_user(self, file_id: UUID4, user_id: UUID4) -> bool:
        try:
            response = supabase.table("files").select("uploaded_by").eq("id", str(file_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check file ownership: {e}"
            )
        
        # If file doesn't exist, return False
        if not response.data or len(response.data) == 0:
            return False
        
        return response.data[0]['uploaded_by'] == str(user_id)
    
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
            import uuid
            from datetime import datetime, timezone
            
            attachment_id = uuid.uuid4()
            file_size = len(file_content)
            
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
            
            insert_data = {
                'id': str(attachment_id),
                'message_id': None,
                'message_type': chat_type,
                'file_name': file_name,
                'file_size': file_size,
                'file_type': content_type,
                'storage_path': s3_key,
                'thumbnail_path': thumbnail_path,
                'uploaded_by': str(user_id),
                'created_at': datetime.now(timezone.utc).isoformat()
            }
            
            response = supabase.table('chat_attachments').insert(insert_data).execute()
            
            if not response.data:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create attachment record"
                )
            
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
            attachment_response = supabase.table('chat_attachments').select('*').eq(
                'id', str(attachment_id)
            ).execute()
            
            if not attachment_response.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Attachment not found"
                )
            
            attachment = attachment_response.data[0]
            
            # Verify user has access to the attachment
            # If attachment is not yet linked to a message, check if user is the uploader
            if not attachment.get('message_id'):
                if attachment.get('uploaded_by') != str(user_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied to this attachment"
                    )
            else:
                # If linked to a message, verify access through message
                self._verify_attachment_access(attachment, user_id)
            
            # Generate thumbnail URL if thumbnail exists
            thumbnail_url = None
            if attachment.get('thumbnail_path'):
                try:
                    thumbnail_url = self.s3_service.generate_presigned_url(
                        attachment['thumbnail_path'],
                        expiration=3600  # 1 hour
                    )
                except (S3ServiceException, Exception) as e:
                    logger.warning(f"Failed to generate thumbnail URL: {str(e)}")
                    pass
            
            return {
                'attachment_id': attachment_id,
                'file_name': attachment['file_name'],
                'file_size': calculate_file_size(attachment['file_size']),
                'file_type': attachment['file_type'],
                'thumbnail_url': thumbnail_url
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
            from datetime import datetime, timezone, timedelta
            
            attachment_response = supabase.table('chat_attachments').select('*').eq(
                'id', str(attachment_id)
            ).execute()
            
            if not attachment_response.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Attachment not found"
                )
            
            attachment = attachment_response.data[0]
            
            self._verify_attachment_access(attachment, user_id)
            
            try:
                download_url = self.s3_service.generate_presigned_url(
                    attachment['storage_path'],
                    expiration=900
                )
            except (S3ServiceException, Exception) as e:
                logger.error(f"Failed to generate presigned URL from S3/MinIO: {str(e)}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to generate download URL. Please try again later."
                )
            
            return {
                'download_url': download_url,
                'expires_at': datetime.now(timezone.utc) + timedelta(minutes=15)
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
            attachment_response = supabase.table('chat_attachments').select('*').eq(
                'id', str(attachment_id)
            ).execute()
            
            if not attachment_response.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Attachment not found"
                )
            
            attachment = attachment_response.data[0]
            
            if attachment['uploaded_by'] != str(user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only delete your own attachments"
                )
            
            try:
                self.s3_service.delete_file(attachment['storage_path'])
            except (S3ServiceException, Exception) as e:
                logger.warning(f"Failed to delete attachment file from S3/MinIO: {str(e)}")
                pass
            
            if attachment.get('thumbnail_path'):
                try:
                    self.s3_service.delete_file(attachment['thumbnail_path'])
                except (S3ServiceException, Exception) as e:
                    logger.warning(f"Failed to delete thumbnail from S3/MinIO: {str(e)}")
                    pass
            
            supabase.table('chat_attachments').delete().eq('id', str(attachment_id)).execute()
            
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
            
            print(thumb_key)
            
            return thumb_key
            
        except Exception as e:
            print(e)
            return None
    
    def _verify_attachment_access(self, attachment: Dict[str, Any], user_id: UUID4) -> None:
        """
        Verify user has access to the attachment's parent message
        
        Args:
            attachment: The attachment record
            user_id: The user ID
            
        Raises:
            HTTPException: If access denied
        """
        if not attachment.get('message_id'):
            return
        
        message_type = attachment['message_type']
        message_id = attachment['message_id']
        
        try:
            if message_type == 'project':
                message_response = supabase.table('chat_messages').select(
                    'project_id'
                ).eq('id', message_id).execute()
                
                if not message_response.data:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Message not found"
                    )
                
                project_id = message_response.data[0]['project_id']
                
                member_response = supabase.table('project_members').select('id').eq(
                    'project_id', project_id
                ).eq('user_id', str(user_id)).execute()
                
                if not member_response.data:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied to this attachment"
                    )
            
            else:
                dm_response = supabase.table('direct_messages').select(
                    'sender_id, receiver_id'
                ).eq('id', message_id).execute()
                
                if not dm_response.data:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Message not found"
                    )
                
                dm = dm_response.data[0]
                
                if dm['sender_id'] != str(user_id) and dm['receiver_id'] != str(user_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied to this attachment"
                    )
        
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify attachment access: {str(e)}"
            )