from fastapi import APIRouter, Depends, Query, File, UploadFile
from pydantic import UUID4
from typing import List
from fastapi import status

router = APIRouter()

from app.schemas.organizations import (
    OrganizationGetResponse,
    OrganizationChangeAvatarResponse,
    OrganizationUpdateRequest,
    OrganizationUpdateResponse,
    OrganizationCreateRequest,
    OrganizationCreateResponse,
    OrganizationGetPaginatedResponse,
)
from app.services.organization import OrganizationService
from app.routers.deps import get_organization_member, get_organization_owner, get_organization_admin_or_owner, get_current_user


@router.post("/{organization_id}/active", status_code=status.HTTP_204_NO_CONTENT)
def set_active_organization(
    organization_id: UUID4,
    member: any = Depends(get_organization_member)
):
    """
        Set the active organization for the current user
    """
    organization_service = OrganizationService()
    return organization_service.set_active_organization(organization_id, member['user_id'])

@router.get("/active", response_model=OrganizationGetResponse, status_code=status.HTTP_200_OK)
def get_active_organization(
    user: any = Depends(get_current_user)
):
    """
        Get the active organization for the current user
    """
    organization_service = OrganizationService()
    return organization_service.get_active_organization(user.id)

@router.post("/deactivate", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_active_organization(
    user: any = Depends(get_current_user)
):
    """
        Deactivate the active organization for the current user
    """
    organization_service = OrganizationService()
    return organization_service.deactivate_active_organization(user.id)

@router.post("/create", response_model=OrganizationCreateResponse, status_code=status.HTTP_201_CREATED)
def create_organization(
    organization_request: OrganizationCreateRequest,
    user: any = Depends(get_current_user)
):
    """
        Create a new organization for the current user
    """
    organization_service = OrganizationService()
    return organization_service.create_organization(organization_request, user.id)

@router.get("/", response_model=OrganizationGetPaginatedResponse, status_code=status.HTTP_200_OK)
def get_organizations(
    user: any = Depends(get_current_user),
    limit: int = Query(default=20, ge=1),
    offset: int = Query(default=0, ge=0)
):
    """
        Get all organizations for the current user
    """
    organization_service = OrganizationService()
    return organization_service.get_organizations(user.id, limit=limit, offset=offset)

@router.get("/{organization_id}", response_model=OrganizationGetResponse, status_code=status.HTTP_200_OK)
def get_organization(
    organization_id: UUID4,
    member: any = Depends(get_organization_member)
):
    """
        Get a specific organization for the organization member
    """
    organization_service = OrganizationService()
    return organization_service.get_organization(organization_id)

@router.delete("/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_organization(
    organization_id: UUID4,
    owner: any = Depends(get_organization_owner)
):
    """
        Delete a specific organization for the organization owner
    """
    organization_service = OrganizationService()
    organization_service.delete_organization(organization_id)

@router.put("/{organization_id}/avatar", response_model=OrganizationChangeAvatarResponse, status_code=status.HTTP_200_OK)
def change_organization_avatar(
    organization_id: UUID4,
    file: UploadFile = File(...),
    admin_or_owner: any = Depends(get_organization_admin_or_owner)
):
    """
        Change the avatar of a specific organization for the organization admin or owner
    """
    organization_service = OrganizationService()
    return organization_service.change_organization_avatar(organization_id, admin_or_owner['user_id'], file)

@router.put("/{organization_id}", response_model=OrganizationUpdateResponse, status_code=status.HTTP_200_OK)
def update_organization(
    organization_id: UUID4,
    organization_request: OrganizationUpdateRequest,
    member: any = Depends(get_organization_admin_or_owner)
):
    """
        Update a specific organization for the organization admin or owner
    """
    organization_service = OrganizationService()
    return organization_service.update_organization(organization_id, organization_request)

