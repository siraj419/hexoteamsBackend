from fastapi import APIRouter, File, UploadFile, Depends, status, HTTPException, Query
from pydantic import UUID4
from typing import Optional

from app.services.files import FilesService
from app.schemas.files import (
    FileBaseResponse,
    FileUploadedByUserGetResponse,
    FileGetResponseWithUser,
    FileGetPaginatedResponseWithUploaders
)
from app.routers.deps import get_active_organization
from app.schemas.organizations import OrganizationMemberRole



router = APIRouter()

def check_organization_admin_or_owner(active_organization: any) -> None:
    return  active_organization['member_role'] == OrganizationMemberRole.ADMIN.value or \
            active_organization['member_role'] == OrganizationMemberRole.OWNER.value
        

@router.post('/upload', response_model=FileBaseResponse, status_code=status.HTTP_201_CREATED)
def upload_file(
    file: UploadFile = File(...),
    active_organization: any = Depends(get_active_organization)
):
    """
        Upload a file to the server
    """
    files_service = FilesService()
    file_data = files_service.upload_file(file, user_id=active_organization['member_user_id'], org_id=active_organization['id'])
    return file_data

@router.get('/{file_id}', response_model=FileGetResponseWithUser, status_code=status.HTTP_200_OK)
def get_file(
    file_id: UUID4,
    active_organization: any = Depends(get_active_organization)
):
    """
        Get a file by its ID
    """
    files_service = FilesService()
    
    # check if the user is the owner of the file
    if not files_service.check_uploaded_by_user(file_id, active_organization['member_user_id']) and not check_organization_admin_or_owner(active_organization):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not the owner of this file or an admin or owner of the organization"
        )
    
    return files_service.get_file_with_url(file_id)

@router.get('/', response_model=FileGetPaginatedResponseWithUploaders, status_code=status.HTTP_200_OK)
def get_files(
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    is_deleted: Optional[bool] = Query(default=None),
    active_organization: any = Depends(get_active_organization)
):
    """
        Get files by the active organization
    """
    
    user_id = None
    if not check_organization_admin_or_owner(active_organization):
        # if the user is not an admin or owner, he can only get his own files
        user_id = active_organization['member_user_id']
    
    
    files_service = FilesService()
    return files_service.get_files(
        org_id=active_organization['id'],
        limit=limit,
        offset=offset,
        user_id=user_id,
        is_deleted=is_deleted,
    )

@router.get('/{file_id}/url', response_model=str, status_code=status.HTTP_200_OK)
def get_file_url(
    file_id: UUID4,
    active_organization: any = Depends(get_active_organization)
):
    """
        Get the presigned URL of a file by its ID
    """
    files_service = FilesService()
    
    # check if the user is the owner of the file
    if  not files_service.check_uploaded_by_user(file_id, active_organization['member_user_id']) and \
        not check_organization_admin_or_owner(active_organization):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not the owner of this file or an admin or owner of the organization"
        )
    
    return files_service.get_file_url(file_id)

@router.delete('/{file_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    file_id: UUID4,
    active_organization: any = Depends(get_active_organization)
):
    """
        Delete a file by its ID (Mark as deleted)
    """
    files_service = FilesService()
    
    # check if the user is the owner of the file
    if  not files_service.check_uploaded_by_user(file_id, active_organization['member_user_id']) and \
        not check_organization_admin_or_owner(active_organization):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not the owner of this file or an admin or owner of the organization"
        )
    
    # delete the file
    files_service.delete_file(file_id)


@router.delete('/{file_id}/permanently', status_code=status.HTTP_204_NO_CONTENT)
def delete_file_permanently(
    file_id: UUID4,
    active_organization: any = Depends(get_active_organization)
):
    """
        Delete a file by its ID (Permanently)
    """
    files_service = FilesService()
    
    # check if the user is the owner of the file
    if  not files_service.check_uploaded_by_user(file_id, active_organization['member_user_id']) and \
        not check_organization_admin_or_owner(active_organization):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not the owner of this file or an admin or owner of the organization"
        )
    
    # delete the file permanently
    files_service.delete_file_permanently(file_id)