import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, UploadFile, status
from pydantic import UUID4
from app.utils.uuid_compat import as_uuid
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core import settings
from app.db.sync_session import SyncSessionLocal
from app.models import Organization, OrganizationMember
from app.schemas.organizations import (
    OrganizationChangeAvatarResponse,
    OrganizationCreateRequest,
    OrganizationCreateResponse,
    OrganizationGetPaginatedResponse,
    OrganizationGetResponse,
    OrganizationMemberRole,
    OrganizationUpdateRequest,
    OrganizationUpdateResponse,
)
from app.services.files import FilesService
from app.utils import random_color, random_icon
from app.utils.redis_cache import ActiveOrganizationCache, cache_service

logger = logging.getLogger(__name__)


class OrganizationService:
    def __init__(self):
        self.files_service = FilesService()

    def create_organization(
        self,
        organization_request: OrganizationCreateRequest,
        user_id: UUID4,
    ) -> OrganizationCreateResponse:
        now = datetime.now(timezone.utc)
        uid = UUID(str(user_id))
        try:
            db = SyncSessionLocal()
            try:
                org = Organization(
                    name=organization_request.name,
                    description=organization_request.description,
                    avatar_color=random_color(),
                    avatar_icon=random_icon(),
                    created_by=uid,
                    created_at=now,
                    updated_at=now,
                )
                db.add(org)
                db.flush()
                db.add(
                    OrganizationMember(
                        org_id=org.id,
                        user_id=uid,
                        role=OrganizationMemberRole.OWNER.value,
                        active=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.commit()
                db.refresh(org)
            except IntegrityError as e:
                db.rollback()
                if getattr(e.orig, "pgcode", None) == "23505":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Organization name already taken",
                    )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to create organization: {e}",
                )
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create organization: {e!s}",
            )

        return OrganizationCreateResponse(
            id=org.id,
            name=org.name,
            description=org.description,
            avatar_color=org.avatar_color,
            avatar_icon=org.avatar_icon,
        )

    def get_organizations(
        self,
        user_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> OrganizationGetPaginatedResponse:
        if limit is None:
            limit = settings.DEFAULT_PAGINATION_LIMIT
        if offset is None:
            offset = settings.DEFAULT_PAGINATION_OFFSET

        uid = UUID(str(user_id))
        try:
            db = SyncSessionLocal()
            try:
                base = (
                    select(Organization, OrganizationMember.role)
                    .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
                    .where(OrganizationMember.user_id == uid)
                )
                total = int(
                    db.execute(
                        select(func.count()).select_from(OrganizationMember).where(
                            OrganizationMember.user_id == uid
                        )
                    ).scalar_one()
                )
                rows = db.execute(base.offset(offset).limit(limit)).all()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organizations: {e!s}",
            )

        organizations = []
        for organization, role in rows:
            avatar_url = None
            if organization.avatar_file_id:
                avatar_url = self.files_service.get_file_url(organization.avatar_file_id)
            member_role = None
            if role:
                try:
                    member_role = OrganizationMemberRole(role)
                except (ValueError, KeyError):
                    member_role = None
            organizations.append(
                OrganizationGetResponse(
                    id=organization.id,
                    name=organization.name,
                    description=organization.description,
                    avatar_color=organization.avatar_color,
                    avatar_icon=organization.avatar_icon,
                    avatar_url=avatar_url,
                    member_role=member_role,
                )
            )

        return OrganizationGetPaginatedResponse(
            organizations=organizations,
            total=total,
            offset=offset,
            limit=limit,
        )

    def get_organization(
        self, organization_id: UUID4, member_role: Optional[str] = None
    ) -> OrganizationGetResponse:
        oid = UUID(str(organization_id))
        try:
            db = SyncSessionLocal()
            try:
                org = db.execute(
                    select(Organization).where(Organization.id == oid)
                ).scalar_one_or_none()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organization: {e!s}",
            )

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )

        avatar_url = None
        if org.avatar_file_id:
            avatar_url = self.files_service.get_file_url(org.avatar_file_id)

        role_enum = None
        if member_role:
            try:
                role_enum = OrganizationMemberRole(member_role)
            except (ValueError, KeyError):
                role_enum = None

        return OrganizationGetResponse(
            id=org.id,
            name=org.name,
            description=org.description,
            avatar_color=org.avatar_color,
            avatar_icon=org.avatar_icon,
            avatar_url=avatar_url,
            member_role=role_enum,
        )

    def delete_organization(self, organization_id: UUID4) -> bool:
        self.files_service.delete_permanently_all_files(organization_id)
        oid = UUID(str(organization_id))
        try:
            db = SyncSessionLocal()
            try:
                org = db.get(Organization, oid)
                if not org:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Organization not found",
                    )
                db.delete(org)
                db.commit()
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete organization: {e!s}",
            )

        return True

    def change_organization_avatar(
        self,
        organization_id: UUID4,
        user_id: UUID4,
        file: UploadFile,
    ) -> OrganizationChangeAvatarResponse:
        oid = UUID(str(organization_id))
        try:
            db = SyncSessionLocal()
            try:
                org = db.execute(
                    select(Organization).where(Organization.id == oid)
                ).scalar_one_or_none()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organization: {e!s}",
            )

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )

        if not self.files_service.validate_file_extension(file.filename):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid file extension",
            )

        if not self.files_service.validate_file_size(file.size):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File size exceeds the maximum allowed size",
            )

        avatar_file_id = org.avatar_file_id
        if avatar_file_id:
            file_data = self.files_service.update_file(as_uuid(str(avatar_file_id)), file)
            file_id = as_uuid(file_data["id"])
        else:
            file_data = self.files_service.upload_file(file, user_id, org_id=organization_id)
            file_id = file_data.id

        avatar_url = self.files_service.get_file_url(file_id)
        now = datetime.now(timezone.utc)

        try:
            db = SyncSessionLocal()
            try:
                r = db.execute(
                    update(Organization)
                    .where(Organization.id == oid)
                    .values(avatar_file_id=UUID(str(file_id)), updated_at=now)
                    .returning(Organization.id)
                )
                if r.first() is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Organization not found",
                    )
                db.commit()
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to change organization avatar: {e!s}",
            )

        cache_service.delete(f"organization:{organization_id}")
        try:
            db = SyncSessionLocal()
            try:
                uids = list(
                    db.execute(
                        select(OrganizationMember.user_id).where(
                            OrganizationMember.org_id == oid,
                            OrganizationMember.active.is_(True),
                        )
                    ).scalars().all()
                )
                if uids:
                    ActiveOrganizationCache.delete_many([str(u) for u in uids])
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to invalidate organization cache: %s", e)

        return OrganizationChangeAvatarResponse(avatar_url=avatar_url)

    def delete_organization_avatar(self, organization_id: UUID4) -> bool:
        oid = UUID(str(organization_id))
        try:
            db = SyncSessionLocal()
            try:
                org = db.execute(
                    select(Organization).where(Organization.id == oid)
                ).scalar_one_or_none()
            finally:
                db.close()

            if not org:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Organization not found",
                )

            avatar_file_id = org.avatar_file_id
            if avatar_file_id:
                try:
                    self.files_service.delete_file_permanently(as_uuid(str(avatar_file_id)))
                except HTTPException as e:
                    logger.warning(
                        "Failed to delete avatar file %s from S3: %s", avatar_file_id, e
                    )
                except Exception as e:
                    logger.warning(
                        "Unexpected error deleting avatar file %s: %s", avatar_file_id, e
                    )

            now = datetime.now(timezone.utc)
            db2 = SyncSessionLocal()
            try:
                r = db2.execute(
                    update(Organization)
                    .where(Organization.id == oid)
                    .values(avatar_file_id=None, updated_at=now)
                    .returning(Organization.id)
                )
                if r.first() is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Organization not found",
                    )
                db2.commit()
            finally:
                db2.close()

            cache_service.delete(f"organization:{organization_id}")
            db3 = SyncSessionLocal()
            try:
                uids = list(
                    db3.execute(
                        select(OrganizationMember.user_id).where(
                            OrganizationMember.org_id == oid,
                            OrganizationMember.active.is_(True),
                        )
                    ).scalars().all()
                )
                if uids:
                    ActiveOrganizationCache.delete_many([str(u) for u in uids])
            finally:
                db3.close()

            return True

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete organization avatar: {e!s}",
            )

    def update_organization(
        self,
        organization_id: UUID4,
        organization_request: OrganizationUpdateRequest,
    ) -> OrganizationUpdateResponse:
        update_values = {}
        if organization_request.name:
            update_values["name"] = organization_request.name
        if organization_request.description:
            update_values["description"] = organization_request.description
        if organization_request.avatar_icon:
            update_values["avatar_icon"] = organization_request.avatar_icon
        if organization_request.avatar_color:
            update_values["avatar_color"] = organization_request.avatar_color

        oid = UUID(str(organization_id))
        now = datetime.now(timezone.utc)

        try:
            db = SyncSessionLocal()
            try:
                if update_values:
                    update_values["updated_at"] = now
                    try:
                        org = db.execute(
                            update(Organization)
                            .where(Organization.id == oid)
                            .values(**update_values)
                            .returning(
                                Organization.id,
                                Organization.name,
                                Organization.description,
                                Organization.avatar_color,
                                Organization.avatar_icon,
                            )
                        ).one_or_none()
                    except IntegrityError as e:
                        db.rollback()
                        if getattr(e.orig, "pgcode", None) == "23505":
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail="Organization name already taken",
                            )
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Failed to update organization: {e!s}",
                        )
                    if not org:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Organization not found",
                        )
                    db.commit()
                else:
                    row = db.execute(
                        select(
                            Organization.id,
                            Organization.name,
                            Organization.description,
                            Organization.avatar_color,
                            Organization.avatar_icon,
                        ).where(Organization.id == oid)
                    ).one_or_none()
                    if not row:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Organization not found",
                        )
                    org = row
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update organization: {e!s}",
            )

        result = OrganizationUpdateResponse(
            id=org.id,
            name=org.name,
            description=org.description,
            avatar_color=org.avatar_color,
            avatar_icon=org.avatar_icon,
        )

        cache_service.delete(f"organization:{organization_id}")
        try:
            db = SyncSessionLocal()
            try:
                uids = list(
                    db.execute(
                        select(OrganizationMember.user_id).where(
                            OrganizationMember.org_id == oid,
                            OrganizationMember.active.is_(True),
                        )
                    ).scalars().all()
                )
                if uids:
                    ActiveOrganizationCache.delete_many([str(u) for u in uids])
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to invalidate organization cache: %s", e)

        return result

    def set_active_organization(self, organization_id: UUID4, user_id: UUID4) -> bool:
        self.deactivate_active_organization(user_id)
        oid = UUID(str(organization_id))
        uid = UUID(str(user_id))
        try:
            db = SyncSessionLocal()
            try:
                db.execute(
                    update(OrganizationMember)
                    .where(
                        OrganizationMember.org_id == oid,
                        OrganizationMember.user_id == uid,
                    )
                    .values(active=True, updated_at=datetime.now(timezone.utc))
                )
                db.commit()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to set active organization: {e!s}",
            )

        ActiveOrganizationCache.delete_organization(str(user_id))
        return True

    def get_active_organization(self, user_id: UUID4) -> OrganizationGetResponse:
        user_id_str = str(user_id)
        cached_org = ActiveOrganizationCache.get_organization(user_id_str)
        if cached_org:
            return OrganizationGetResponse(**cached_org)

        uid = UUID(user_id_str)
        try:
            db = SyncSessionLocal()
            try:
                row = db.execute(
                    select(Organization, OrganizationMember.role)
                    .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
                    .where(
                        OrganizationMember.user_id == uid,
                        OrganizationMember.active.is_(True),
                    )
                ).first()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get active organization: {e!s}",
            )

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No active organization found",
            )

        organization, role = row
        avatar_url = None
        if organization.avatar_file_id:
            avatar_url = self.files_service.get_file_url(organization.avatar_file_id)

        member_role = OrganizationMemberRole(role) if role else None

        org_response = OrganizationGetResponse(
            id=organization.id,
            name=organization.name,
            description=organization.description,
            avatar_color=organization.avatar_color,
            avatar_icon=organization.avatar_icon,
            avatar_url=avatar_url,
            member_role=member_role,
        )

        ActiveOrganizationCache.set_organization(
            user_id_str, org_response.model_dump(mode="json")
        )
        return org_response

    def deactivate_active_organization(self, user_id: UUID4) -> bool:
        uid = UUID(str(user_id))
        try:
            db = SyncSessionLocal()
            try:
                db.execute(
                    update(OrganizationMember)
                    .where(
                        OrganizationMember.user_id == uid,
                        OrganizationMember.active.is_(True),
                    )
                    .values(active=False, updated_at=datetime.now(timezone.utc))
                )
                db.commit()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to deactivate active organization: {e!s}",
            )

        ActiveOrganizationCache.delete_organization(str(user_id))
        return True

    def _add_organization_member(
        self,
        organization_id: UUID4,
        user_id: UUID4,
        role: OrganizationMemberRole,
    ):
        now = datetime.now(timezone.utc)
        role_value = role.value if isinstance(role, OrganizationMemberRole) else role
        oid = UUID(str(organization_id))
        uid = UUID(str(user_id))
        try:
            db = SyncSessionLocal()
            try:
                db.add(
                    OrganizationMember(
                        org_id=oid,
                        user_id=uid,
                        role=role_value,
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.commit()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add organization member: {e!s}",
            )
