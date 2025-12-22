from pydantic import UUID4
from typing import List, Optional
from datetime import datetime, timezone
from supabase_auth.errors import AuthApiError
from fastapi import HTTPException, status
from app.core import supabase
from app.schemas.attachments import (
    AttachmentType,
    AttachmentResponse,
    AttachmentGetPaginatedResponse,
)
from app.services.files import FilesService
from app.utils import apply_pagination, calculate_file_size
from app.utils.redis_cache import ProjectSummaryCache

class AttachmentService:
    def __init__(self, files_service: FilesService):
        self.files_service = files_service

    def add_attachment(
        self,
        entity_type: AttachmentType,
        entity_id: UUID4,
        file_id: UUID4,
    ) -> AttachmentResponse:
        try:
            response = supabase.table('attachments').insert({
                'entity_type': entity_type.value,
                'entity_id': str(entity_id),
                'file_id': str(file_id),
                'created_at': datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            if e.code == '23503':
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to add attachment for file {file_id}, invalid file id",
                )
            
            if e.code == '23505':
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to add attachment for {entity_type.value}: {entity_id}, file already attached",
                )
            
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add attachment: {e}"
            )

        # get file info
        file_info = self.files_service.get_file(file_id)
        
        # Invalidate project summary cache for project attachments
        if entity_type == AttachmentType.PROJECT:
            ProjectSummaryCache.delete_summary(str(entity_id))
        
        return AttachmentResponse(
            id=response.data[0]['id'],
            file_id=file_id,
            file_name=file_info.name,
            file_size=file_info.size,
            content_type=file_info.content_type,
        )
    
    def get_attachment_file_url(
        self,
        attachment_id: UUID4,
    ) -> str:
        try:
            response = supabase.table('attachments').select('file_id').eq('id', attachment_id).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get attachment file url: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Attachment not found"
            )
        
        file_id = response.data[0]['file_id']
        file_url = self.files_service.get_file_url(file_id)
        
        return file_url
    
    def get_attachments(
        self,
        entity_type: AttachmentType,
        entity_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> AttachmentGetPaginatedResponse:
        """
        Get attachments for an entity with pagination.
        Optimized to fetch all data in a single query with file info joined.
        """
        query = supabase.table('attachments').select('id,file_id,files(name,size_bytes,content_type)', count='exact').eq('entity_type', entity_type.value).eq('entity_id', str(entity_id))
        
        limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get attachments: {e}"
            )
        
        total_count = response.count if hasattr(response, 'count') and response.count is not None else len(response.data) if response.data else 0
        
        attachments = [AttachmentResponse(
            id=attachment['id'],
            file_id=attachment['file_id'],
            file_name=attachment['files']['name'],
            file_size=calculate_file_size(attachment['files']['size_bytes']),
            content_type=attachment['files']['content_type'],
        ) for attachment in response.data]
        
        return AttachmentGetPaginatedResponse(
            attachments=attachments,
            total=total_count,
            offset=offset,
            limit=limit,
        )
    
    def delete_attachment(
        self,
        attachment_id: UUID4,
    ) -> bool:
        """
        Delete an attachment. Optimized single query operation.
        """
        # Get attachment info before deleting
        attachment_data = None
        try:
            attachment_response = supabase.table('attachments').select('entity_id, entity_type').eq('id', str(attachment_id)).execute()
            if attachment_response.data and len(attachment_response.data) > 0:
                attachment_data = attachment_response.data[0]
        except Exception:
            pass
        
        try:
            response = supabase.table('attachments').delete().eq('id', str(attachment_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete attachment: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Attachment not found"
            )
        
        # Invalidate project summary cache for project attachments
        if attachment_data and attachment_data.get('entity_type') == AttachmentType.PROJECT.value:
            ProjectSummaryCache.delete_summary(attachment_data['entity_id'])
        
        return True

    def delete_all(
        self,
        entity_id: UUID4,
        entity_type: AttachmentType,
    ) -> bool:
        try:
            supabase.table('attachments').delete().eq('entity_id', entity_id).eq('entity_type', entity_type.value).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all attachments: {e}"
            )
        
        # Invalidate project summary cache for project attachments
        if entity_type == AttachmentType.PROJECT:
            ProjectSummaryCache.delete_summary(str(entity_id))
        
        return True