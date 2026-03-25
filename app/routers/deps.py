from uuid import UUID

import httpx
import logging
import time
from typing import Any

from fastapi import Depends, HTTPException, Query, Request, status
from pydantic import UUID4
from sqlalchemy import select

from app.core.security import TOKEN_TYPE_ACCESS, AuthenticatedUser, decode_token
from app.db.sync_session import SyncSessionLocal
from app.models import (
    ChatConversation,
    ChatMessage,
    DirectMessage,
    Organization,
    OrganizationMember,
    Profile,
    Project,
    ProjectMember,
    Task,
)
from app.schemas.organizations import OrganizationMemberRole

logger = logging.getLogger(__name__)


def get_current_user(request: Request) -> AuthenticatedUser:
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized, provide a valid token",
        )

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized, provide a valid token",
        )
    token = parts[1]

    try:
        payload = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
        user_id = UUID(payload["sub"])
    except ValueError as e:
        logger.debug("Token validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized, provide a valid token",
        )

    db = SyncSessionLocal()
    try:
        profile = db.execute(select(Profile).where(Profile.user_id == user_id)).scalar_one_or_none()
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized, provide a valid token",
            )
        return AuthenticatedUser(
            id=profile.user_id,
            email=profile.email,
            user_metadata={"display_name": profile.display_name or ""},
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )
    finally:
        db.close()


def get_active_organization(user: Any = Depends(get_current_user)) -> Any:
    max_retries = 3
    retry_delay = 0.5

    for attempt in range(max_retries):
        try:
            db = SyncSessionLocal()
            try:
                row = db.execute(
                    select(OrganizationMember, Organization)
                    .join(Organization, OrganizationMember.org_id == Organization.id)
                    .where(
                        OrganizationMember.user_id == user.id,
                        OrganizationMember.active.is_(True),
                    )
                ).first()
            finally:
                db.close()

            if not row:
                response_organization_member = type("R", (), {"data": []})()
            else:
                om, org = row
                nested = {
                    "id": str(org.id),
                    "name": org.name,
                    "description": org.description,
                    "avatar_color": org.avatar_color,
                    "avatar_icon": org.avatar_icon,
                    "avatar_file_id": org.avatar_file_id,
                }
                response_organization_member = type(
                    "R",
                    (),
                    {"data": [{"role": om.role, "organizations": nested}]},
                )()
            break
        except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2**attempt)
                logger.warning(
                    "Network error getting active organization (attempt %s/%s): %s. Retrying in %ss...",
                    attempt + 1,
                    max_retries,
                    e,
                    wait_time,
                )
                time.sleep(wait_time)
                continue
            logger.error("Failed to get active organization after %s attempts: %s", max_retries, e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service temporarily unavailable. Please try again in a moment.",
            )
        except Exception as e:
            logger.error("Unexpected error getting active organization: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get active organization: {str(e)}",
            )

    if not response_organization_member.data or len(response_organization_member.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have an active organization",
        )

    org = response_organization_member.data[0]["organizations"]
    afid = org.get("avatar_file_id")
    return {
        "id": str(org["id"]),
        "name": org["name"],
        "description": org["description"],
        "avatar_color": org["avatar_color"],
        "avatar_icon": org["avatar_icon"],
        "avatar_file_id": str(afid) if afid is not None else str(None),
        "member_user_id": str(user.id),
        "member_role": response_organization_member.data[0]["role"],
    }


def get_organization_member(organization_id: UUID4, user: Any = Depends(get_current_user)) -> Any:
    max_retries = 3
    retry_delay = 0.5

    for attempt in range(max_retries):
        try:
            db = SyncSessionLocal()
            try:
                m = db.execute(
                    select(OrganizationMember).where(
                        OrganizationMember.org_id == organization_id,
                        OrganizationMember.user_id == user.id,
                    )
                ).scalar_one_or_none()
            finally:
                db.close()

            if not m:
                response = type("R", (), {"data": []})()
            else:
                response = type(
                    "R",
                    (),
                    {
                        "data": [
                            {
                                "id": str(m.id),
                                "org_id": str(m.org_id),
                                "user_id": str(m.user_id),
                                "role": m.role,
                                "active": m.active,
                                "created_at": m.created_at.isoformat() if m.created_at else None,
                                "updated_at": m.updated_at.isoformat() if m.updated_at else None,
                            }
                        ]
                    },
                )()
            break
        except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2**attempt)
                logger.warning(
                    "Network error getting organization member (attempt %s/%s): %s. Retrying...",
                    attempt + 1,
                    max_retries,
                    e,
                )
                time.sleep(wait_time)
                continue
            logger.error("Failed to get organization member after %s attempts: %s", max_retries, e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service temporarily unavailable. Please try again in a moment.",
            )
        except Exception as e:
            logger.error("Unexpected error getting organization member: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organization member: {str(e)}",
            )

    if not response.data or len(response.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this organization or the organization does not exist",
        )

    return response.data[0]


def get_organization_owner(user: Any = Depends(get_current_user)) -> Any:
    organization = get_active_organization(user)
    if organization["member_role"] != OrganizationMemberRole.OWNER.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not the owner of this organization",
        )

    return organization


def get_organization_admin_or_owner(user: Any = Depends(get_current_user)) -> Any:
    organization = get_active_organization(user)
    member_role = organization["member_role"]
    if member_role != OrganizationMemberRole.ADMIN.value and member_role != OrganizationMemberRole.OWNER.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not an admin or owner of this organization",
        )

    return organization


def get_project_member(project_id: UUID4 = Query(...), user: Any = Depends(get_current_user)):
    db = SyncSessionLocal()
    try:
        m = db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user.id,
            )
        ).scalar_one_or_none()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get project member: {e}",
        )
    finally:
        db.close()

    if not m:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this project",
        )

    member_data = {
        "id": str(m.id),
        "project_id": str(m.project_id),
        "user_id": str(m.user_id),
        "role": m.role,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    member_data["project_id"] = project_id
    member_data["user_id"] = user.id
    return member_data


def get_project_member_with_chat_access(project_id: UUID4, user: Any = Depends(get_current_user)) -> dict:
    db = SyncSessionLocal()
    try:
        m = db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user.id,
            )
        ).scalar_one_or_none()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify project membership: {e}",
        )
    finally:
        db.close()

    if not m:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this project",
        )

    return {
        "user_id": user.id,
        "project_id": str(project_id),
        "role": m.role,
        "is_admin": m.role in ["admin", "owner"],
    }


def get_dm_conversation_participant(conversation_id: UUID4, user: Any = Depends(get_current_user)) -> dict:
    db = SyncSessionLocal()
    try:
        conv = db.execute(
            select(ChatConversation).where(ChatConversation.id == conversation_id)
        ).scalar_one_or_none()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify conversation access: {e}",
        )
    finally:
        db.close()

    if not conv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    if conv.user1_id != user.id and conv.user2_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this conversation",
        )

    conversation = {
        "id": str(conv.id),
        "user1_id": str(conv.user1_id),
        "user2_id": str(conv.user2_id),
        "organization_id": str(conv.organization_id),
        "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
    }

    return {
        "user_id": user.id,
        "conversation_id": str(conversation_id),
        "conversation": conversation,
    }


def verify_message_author(
    message_id: UUID4,
    user: Any = Depends(get_current_user),
    is_project_message: bool = True,
) -> dict:
    db = SyncSessionLocal()
    try:
        if is_project_message:
            msg = db.execute(select(ChatMessage).where(ChatMessage.id == message_id)).scalar_one_or_none()
            user_field = "user_id"
        else:
            msg = db.execute(select(DirectMessage).where(DirectMessage.id == message_id)).scalar_one_or_none()
            user_field = "sender_id"
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify message authorship: {e}",
        )
    finally:
        db.close()

    if not msg:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )

    author_id = msg.user_id if is_project_message else msg.sender_id
    if author_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify your own messages",
        )

    if is_project_message:
        message = {
            "id": str(msg.id),
            "project_id": str(msg.project_id),
            "user_id": str(msg.user_id),
            "body": msg.body,
            "message_type": msg.message_type,
            "reply_to_id": str(msg.reply_to_id) if msg.reply_to_id else None,
            "read_by": msg.read_by,
            "attachments": msg.attachments,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
            "deleted_at": msg.deleted_at.isoformat() if msg.deleted_at else None,
        }
    else:
        message = {
            "id": str(msg.id),
            "sender_id": str(msg.sender_id),
            "receiver_id": str(msg.receiver_id),
            "organization_id": str(msg.organization_id),
            "body": msg.body,
            "message_type": msg.message_type,
            "attachments": msg.attachments,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
            "deleted_at": msg.deleted_at.isoformat() if msg.deleted_at else None,
            "read_at": msg.read_at.isoformat() if msg.read_at else None,
        }

    return {
        "user_id": user.id,
        "message_id": str(message_id),
        "message": message,
    }


def verify_organization_membership(organization_id: UUID4, user: Any = Depends(get_current_user)) -> dict:
    m = None
    db = SyncSessionLocal()
    try:
        m = db.execute(
            select(OrganizationMember).where(
                OrganizationMember.org_id == organization_id,
                OrganizationMember.user_id == user.id,
            )
        ).scalar_one_or_none()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify organization membership: {e}",
        )
    finally:
        db.close()

    if not m:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this organization",
        )

    return {
        "user_id": user.id,
        "organization_id": str(organization_id),
        "role": m.role,
    }


def verify_task_delete_permission(
    task_id: UUID4,
    project_id: UUID4 = Query(...),
    user: Any = Depends(get_current_user),
) -> dict:
    db = SyncSessionLocal()
    try:
        pm = db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user.id,
            )
        ).scalar_one_or_none()
        if not pm:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not a member of this project",
            )

        task = db.execute(select(Task).where(Task.id == task_id)).scalar_one_or_none()
        if not task:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found",
            )

        if task.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Task does not belong to the specified project",
            )

        task_creator_id = task.created_by

        if task_creator_id == user.id:
            return {
                "user_id": user.id,
                "task_id": str(task_id),
                "can_delete": True,
                "is_creator": True,
                "is_org_admin": False,
            }

        proj = db.execute(select(Project).where(Project.id == project_id)).scalar_one_or_none()
        if not proj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )

        org_m = db.execute(
            select(OrganizationMember).where(
                OrganizationMember.org_id == proj.org_id,
                OrganizationMember.user_id == user.id,
            )
        ).scalar_one_or_none()

        if org_m and org_m.role in (
            OrganizationMemberRole.OWNER.value,
            OrganizationMemberRole.ADMIN.value,
        ):
            return {
                "user_id": user.id,
                "task_id": str(task_id),
                "can_delete": True,
                "is_creator": False,
                "is_org_admin": True,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify organization role: {e}",
        )
    finally:
        db.close()

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the task creator or organization admins/owners can delete this task",
    )
