from fastapi import HTTPException, status
from pydantic import UUID4
from datetime import datetime, timezone
from typing import List, Optional

from app.schemas.links import (
    LinkEntityType,
    LinkRequest,
    LinkResponse,
    LinkUpdateRequest,
    LinkGetPaginatedResponse,
)
from app.core import supabase
from app.utils import calculate_time_ago, apply_pagination

class LinkService:
    def __init__(self, user_timezone: str = 'utc'):
        self.user_timezone = user_timezone
    
    def create_link(
        self,
        link_request: LinkRequest,
        entity_id: UUID4,
        entity_type: LinkEntityType
    ) -> LinkResponse:
        
        try:
            response = supabase.table('links').insert({
                'title': link_request.title,
                'link_url': str(link_request.link_url),
                'entity_id': str(entity_id),
                'entity_type': entity_type.value,
                'created_at': datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create link: {e}"
            )
        
        return LinkResponse(
            id=response.data[0]['id'],
            title=response.data[0]['title'],
            link_url=response.data[0]['link_url'],
            created_time=calculate_time_ago(response.data[0]['created_at'], self.user_timezone),
        )
    
    def get_links(
        self,
        entity_id: UUID4,
        entity_type: LinkEntityType,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> LinkGetPaginatedResponse:
        """
        Get links for an entity with pagination.
        Optimized to fetch all data in a single query.
        """
        query = supabase.table('links').select('*', count='exact').eq('entity_id', str(entity_id)).eq('entity_type', entity_type.value).order('created_at', desc=True)
        
        limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get links: {e}"
            )
        
        total_count = response.count if hasattr(response, 'count') and response.count is not None else len(response.data) if response.data else 0
        
        links = [LinkResponse(
            id=link['id'],
            title=link['title'],
            link_url=link['link_url'],
            created_time=calculate_time_ago(link['created_at'], self.user_timezone),
        ) for link in response.data]
        
        return LinkGetPaginatedResponse(
            links=links,
            total=total_count,
            offset=offset,
            limit=limit,
        )
    
    def update_link(
        self,
        link_id: UUID4,
        link_request: LinkUpdateRequest,
    ) -> LinkResponse:
        """
        Update a link. Optimized to fetch updated data in single query.
        """
        updates = {}
        if link_request.title is not None:
            updates['title'] = link_request.title
        if link_request.link_url is not None:
            updates['link_url'] = str(link_request.link_url)
        
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No updates provided"
            )
                
        try:
            response = supabase.table('links').update(updates).eq('id', str(link_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update link: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Link not found"
            )
        
        return LinkResponse(
            id=response.data[0]['id'],
            title=response.data[0]['title'],
            link_url=response.data[0]['link_url'],
            created_time=calculate_time_ago(response.data[0]['created_at'], self.user_timezone),
        )
    
    def delete_link(
        self,
        link_id: UUID4,
    ) -> bool:
        """
        Delete a link. Optimized single query operation.
        """
        try:
            response = supabase.table('links').delete().eq('id', str(link_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete link: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Link not found"
            )
        
        return True

    def delete_all(
        self,
        entity_id: UUID4,
        entity_type: LinkEntityType,
    ) -> bool:
        try:
            response = supabase.table('links').delete().eq('entity_id', str(entity_id)).eq('entity_type', entity_type.value).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all links: {e}"
            )
            
        return True