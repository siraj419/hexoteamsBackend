"""SQL equivalents for former Supabase RPCs (use with sync Session)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select, text
from sqlalchemy.orm import Session

from app.models import Project, ProjectMember


def select_member_projects(session: Session, user_id: UUID, org_id: UUID) -> Select[Any]:
    return (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            ProjectMember.user_id == user_id,
            Project.org_id == org_id,
        )
    )


def select_non_member_projects(session: Session, org_id: UUID, user_id: UUID) -> Select[Any]:
    member_project_ids = select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
    return select(Project).where(
        Project.org_id == org_id,
        ~Project.id.in_(member_project_ids),
    )


def mark_project_messages_read_batch(
    session: Session,
    project_id: UUID,
    user_id_text: str,
    last_message_created_at: Any,
) -> None:
    session.execute(
        text(
            """
            UPDATE chat_messages
            SET read_by = CASE
                WHEN read_by IS NULL THEN jsonb_build_array(:uid)
                WHEN NOT (read_by @> to_jsonb(CAST(:uid AS TEXT))) THEN read_by || to_jsonb(CAST(:uid AS TEXT))
                ELSE read_by
            END
            WHERE project_id = :pid
              AND created_at <= :ts
              AND deleted_at IS NULL
              AND (read_by IS NULL OR NOT (read_by @> to_jsonb(CAST(:uid AS TEXT))))
            """
        ),
        {
            "pid": str(project_id),
            "uid": user_id_text,
            "ts": last_message_created_at,
        },
    )
