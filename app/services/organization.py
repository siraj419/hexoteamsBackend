from fastapi import HTTPException, status, UploadFile
from pydantic import UUID4
from datetime import datetime, timezone
from supabase_auth.errors import AuthApiError
from typing import Optional, List, Any

from app.core import supabase
from app.schemas.organizations import (
    OrganizationCreateRequest,
    OrganizationCreateResponse,
    OrganizationGetResponse,
    OrganizationUpdateRequest,
    OrganizationChangeAvatarResponse,
    OrganizationUpdateResponse,
    OrganizationMemberRole,
    OrganizationGetPaginatedResponse,
)
from app.utils import random_color, random_icon
from app.services.files import FilesService
from app.core import settings

class OrganizationService:
    def __init__(self):
        self.files_service = FilesService()
    
    def create_organization(
        self,
        organization_request: OrganizationCreateRequest,
        user_id: UUID4
    ) -> OrganizationCreateResponse:
        
        # create the organization
        try:
            response = supabase.table('organizations').insert({
                'name': organization_request.name,
                'description': organization_request.description,
                'avatar_color': random_color(),
                'avatar_icon': random_icon(),
                'created_by': str(user_id),
                'created_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).execute()
            
        except Exception as e:
            if e.code == '23505': # unique violation
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Organization name already taken"
                )
                
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create organization: {e.message}"
            )
            
        # create the organization member
        self._add_organization_member(response.data[0]['id'], user_id, OrganizationMemberRole.OWNER.value)
        
        return OrganizationCreateResponse(
            id=response.data[0]['id'],
            name=response.data[0]['name'],
            description=response.data[0]['description'],
            avatar_color=response.data[0]['avatar_color'],
            avatar_icon=response.data[0]['avatar_icon'],
        )
    
    def get_organizations(
        self,
        user_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None
    ) -> OrganizationGetPaginatedResponse:
        # apply the default limit and offset
        if limit is None:
            limit = settings.DEFAULT_PAGINATION_LIMIT
        if offset is None:
            offset = settings.DEFAULT_PAGINATION_OFFSET
        
        # Query organization_members to get organizations where user is a member
        # Using PostgREST embedded resources to join with organizations table
        query = (
            supabase.table('organization_members')
            .select('organizations(*)', count='exact')
            .eq('user_id', str(user_id))
        )
        
        # apply the limit and offset
        query = query.range(offset, offset + limit - 1)
        
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organizations: {e}"
            )
        
        organizations = []
        # generate the avatar urls
        for member_record in response.data:
            organization = member_record.get('organizations')
            if not organization:
                continue
                
            avatar_url = None
            if organization.get('avatar_file_id'):
                avatar_url = self.files_service.get_file_url(organization['avatar_file_id'])
            organizations.append(
                OrganizationGetResponse(
                    id=organization['id'],
                    name=organization['name'],
                    description=organization['description'],
                    avatar_color=organization['avatar_color'],
                    avatar_icon=organization['avatar_icon'],
                    avatar_url=avatar_url,
                )
            )
            
        return OrganizationGetPaginatedResponse(
            organizations=organizations,
            total=response.count,
            offset=offset,
            limit=limit,
        )
    
    def get_organization(self, organization_id: UUID4) -> OrganizationGetResponse:
        try:
            response = supabase.table('organizations').select('*').eq('id', str(organization_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organization: {e}"
            )
            
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )
            
        avatar_url = None
        if response.data[0]['avatar_file_id']:
            avatar_url = self.files_service.get_file_url(response.data[0]['avatar_file_id'])
        
        return OrganizationGetResponse(
            id=response.data[0]['id'],
            name=response.data[0]['name'],
            description=response.data[0]['description'],
            avatar_color=response.data[0]['avatar_color'],
            avatar_icon=response.data[0]['avatar_icon'],
            avatar_url=avatar_url,
        )

    def delete_organization(self, organization_id: UUID4) -> bool:
        
        # delete the org files
        self.files_service.delete_permanently_all_files(organization_id)
        
        try:
            response = supabase.table('organizations').delete().eq('id', str(organization_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete organization: {e}"
            )
            
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )
            
        return True
    
    def change_organization_avatar(
        self,
        organization_id: UUID4,
        user_id: UUID4,
        file: UploadFile
    ) -> OrganizationChangeAvatarResponse:
        
        # validate the file
        if not self.files_service.validate_file_extension(file.filename):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid file extension"
            )
        
        if not self.files_service.validate_file_size(file.size):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File size exceeds the maximum allowed size"
            )
        
        # upload the file
        file_data = self.files_service.upload_file(file, user_id, org_id=organization_id)
        avatar_url = self.files_service.get_file_url(file_data['id'])
        
        try:
            response = supabase.table('organizations').update({
                'avatar_file_id': str(file_data['id']),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', str(organization_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to change organization avatar: {e}"
            )
            
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )
        
        return OrganizationChangeAvatarResponse(avatar_url=avatar_url)
    
    def update_organization(
        self,
        organization_id: UUID4,
        organization_request: OrganizationUpdateRequest
    ) -> OrganizationUpdateResponse:
        update_data = {}
        if organization_request.name:
            update_data['name'] = organization_request.name
        if organization_request.description:
            update_data['description'] = organization_request.description
        if organization_request.avatar_icon:
            update_data['avatar_icon'] = organization_request.avatar_icon
        if organization_request.avatar_color:
            update_data['avatar_color'] = organization_request.avatar_color
            
        try:
            if update_data:
                update_data['updated_at'] = datetime.now(timezone.utc).isoformat()
                response = supabase.table('organizations').update(update_data).eq('id', str(organization_id)).execute()
        except Exception as e:
            if e.code == '23505': # unique violation
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Organization name already taken"
                )
                
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update organization: {e}"
            )
        
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )
        
        return OrganizationUpdateResponse(
            id=response.data[0]['id'],
            name=response.data[0]['name'],
            description=response.data[0]['description'],
            avatar_color=response.data[0]['avatar_color'],
            avatar_icon=response.data[0]['avatar_icon']
        )

    def set_active_organization(
        self,
        organization_id: UUID4,
        user_id: UUID4
    ) -> bool:
        
        # deactivate the active organization for the user
        self.deactivate_active_organization(user_id)
        
        print(organization_id, user_id)
    
        try:
            response = supabase.table('organization_members').update({
                'active': True,
            }).eq('org_id', str(organization_id)).eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to set active organization: {e}"
            )
            
        return True
    
    def get_active_organization(
        self,
        user_id: UUID4
    ) -> OrganizationGetResponse:

        
        try:
            response = supabase.table('organization_members').select('organizations(*)').eq('user_id', str(user_id)).eq('active', True).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get active organization: {e}"
            )
                    
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No active organization found"
            )
            
        avatar_url = None
        if response.data[0]['organizations']['avatar_file_id']:
            avatar_url = self.files_service.get_file_url(response.data[0]['organizations']['avatar_file_id'])
                    
        return OrganizationGetResponse(
            id=response.data[0]['organizations']['id'],
            name=response.data[0]['organizations']['name'],
            description=response.data[0]['organizations']['description'],
            avatar_color=response.data[0]['organizations']['avatar_color'],
            avatar_icon=response.data[0]['organizations']['avatar_icon'],
            avatar_url=avatar_url,
        )
    
    def deactivate_active_organization(
        self,
        user_id: UUID4
    ) -> bool:
        
        try:
            response = supabase.table('organization_members').update({
                'active': False,
            }).eq('user_id', str(user_id)).eq('active', True).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to deactivate active organization: {e}"
            )

            
        return True
    
    def _add_organization_member(
        self,
        organization_id: UUID4,
        user_id: UUID4,
        role: OrganizationMemberRole
    ) -> Any:
        
        try:
            response = supabase.table('organization_members').insert({
                'org_id': str(organization_id),
                'user_id': str(user_id),
                'role': role.value,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add organization member: {e}"
            )
            
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to add organization member"
            )
            
        return response.data[0]
