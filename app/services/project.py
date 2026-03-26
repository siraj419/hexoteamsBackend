from __future__ import annotations

import logging
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional
from uuid import UUID as StdUUID

from fastapi import HTTPException, UploadFile, status
from pydantic import UUID4
from app.utils.uuid_compat import as_uuid
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from app.db.rpc_compat import select_member_projects, select_non_member_projects
from app.db.sync_session import SyncSessionLocal
from app.models import (
    FavouriteProject,
    Link,
    OrganizationMember,
    Profile,
    Project,
    Task,
)
from app.models.project_member import ProjectMember as ProjectMemberRow
from app.schemas.activities import ActivityResponse, ActivityType
from app.schemas.projects import (
    ProjectCreateRequest, ProjectCreateResponse,
    ProjectGetResponse, ProjectUpdateRequest, ProjectUpdateResponse,
    ProjectChangeAvatarResponse,
    ProjectMemberRole,
    ProjectMember,
    ProjectTasksView,
    ProjectOrderBy,
    AllProjectsResponse,
    NonMemberProjectsResponse,
    ArchivedProjectsResponse,
    ProjectSummaryResponse,
    ProjectMemberSummary,
    ProjectLinkSummary,
    TaskSummary,
    TeamWorkload,
    UserWorkload,
    ProjectResponse,
    FavouriteProjectsResponse,
    RecentProjectResponse,
    RecentProjectsResponse,
)
from app.schemas.organizations import OrganizationMemberRole
from app.services.activity import ActivityService
from app.services.files import FilesService
from app.services.link import LinkEntityType
from app.utils import random_color, random_icon
from app.utils.redis_cache import ProjectSummaryCache, UserCache
from app.utils.sa_pagination import apply_sa_limit_offset
from app.schemas.tasks import TaskStatus

logger = logging.getLogger(__name__)

class ProjectService:
    def __init__(self):
        self.files_service = FilesService()
        self.activity_service = ActivityService(self.files_service)

    @staticmethod
    def _parse_view(view_value: Any) -> ProjectTasksView:
        if isinstance(view_value, str):
            try:
                return ProjectTasksView(view_value)
            except ValueError:
                return ProjectTasksView.LIST
        return ProjectTasksView.LIST

    @staticmethod
    def _project_member_row_to_api(
        m: ProjectMemberRow,
        project_id: UUID4,
    ) -> ProjectMember:
        return ProjectMember(
            id=as_uuid(str(m.id)),
            project_id=project_id,
            user_id=as_uuid(str(m.user_id)),
            role=ProjectMemberRole(m.role),
            created_at=m.created_at,
            updated_at=m.updated_at,
        )

    def change_project_avatar(
        self,
        user_id: UUID4,
        org_id: UUID4,
        file: UploadFile,
        project_id: Optional[UUID4] = None,
    ) -> ProjectChangeAvatarResponse:
        avatar_file_id = None
        if project_id:
            db = SyncSessionLocal()
            try:
                row = db.get(Project, StdUUID(str(project_id)))
                if not row:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Project not found",
                    )
                avatar_file_id = row.avatar_file_id
            finally:
                db.close()
            
        
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
        
        if avatar_file_id:
            file_data = self.files_service.update_file(avatar_file_id, file)
        else:
            file_data = self.files_service.upload_file(file, user_id, org_id, project_id)
        
        return ProjectChangeAvatarResponse(
            avatar_url=self.files_service.get_file_url(file_data.id),
        )

    def archive_project(
        self,
        project_id: UUID4,
    ) -> bool:
        db = SyncSessionLocal()
        try:
            row = db.get(Project, StdUUID(str(project_id)))
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found",
                )
            row.archived = True
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to archive project: {e}",
            )
        finally:
            db.close()
        return True

    def restore_project(
        self,
        project_id: UUID4,
    ) -> bool:
        db = SyncSessionLocal()
        try:
            row = db.get(Project, StdUUID(str(project_id)))
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found",
                )
            row.archived = False
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to restore project: {e}",
            )
        finally:
            db.close()
        return True

    def toggle_project_favourite(
        self,
        project_id: UUID4,
        user_id: UUID4,
    ) -> bool:
        pid, uid = StdUUID(str(project_id)), StdUUID(str(user_id))
        db = SyncSessionLocal()
        try:
            res = db.execute(
                delete(FavouriteProject).where(
                    FavouriteProject.project_id == pid,
                    FavouriteProject.user_id == uid,
                )
            )
            if res.rowcount and res.rowcount > 0:
                db.commit()
                return True
            try:
                db.add(FavouriteProject(project_id=pid, user_id=uid))
                db.commit()
            except IntegrityError as e:
                db.rollback()
                orig = getattr(e.orig, "pgcode", None) or getattr(e, "pgcode", None)
                if orig == "23503":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Project or user not found",
                    )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to add project to favourites: {e}",
                )
            return True
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to toggle favourite: {e}",
            )
        finally:
            db.close()
    
    def create_project(
        self,
        project_request: ProjectCreateRequest,
        org_id: str,
        user_id: str,
    ) -> ProjectCreateResponse:
        oid = StdUUID(str(org_id))
        uid = StdUUID(str(user_id))
        db = SyncSessionLocal()
        try:
            taken = db.scalar(
                select(func.count())
                .select_from(Project)
                .where(
                    Project.org_id == oid,
                    Project.name.ilike(f"%{project_request.name}%"),
                )
            )
            if taken and taken > 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Project name already taken",
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check if project name is already taken: {e}",
            )
        finally:
            db.close()

        if not project_request.avatar_icon:
            project_request.avatar_icon = random_icon()
        if not project_request.avatar_color:
            project_request.avatar_color = random_color()

        view_val = (
            project_request.view.value
            if project_request.view
            else ProjectTasksView.LIST.value
        )
        now = datetime.now(timezone.utc)
        row = Project(
            org_id=oid,
            name=project_request.name,
            avatar_color=project_request.avatar_color,
            avatar_icon=project_request.avatar_icon,
            avatar_file_id=StdUUID(str(project_request.avatar_file_id))
            if project_request.avatar_file_id
            else None,
            start_date=project_request.start_date,
            end_date=project_request.end_date,
            view=view_val,
            progress_percentage=0,
            created_by=uid,
            created_at=now,
            updated_at=now,
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
                detail=f"Failed to create project: {e}",
            )
        finally:
            db.close()

        project_id = as_uuid(str(row.id))
        self._add_project_member(project_id, as_uuid(str(user_id)), ProjectMemberRole.OWNER)

        if project_request.avatar_file_id:
            self.files_service.update_file_project_id(
                project_request.avatar_file_id, project_id
            )

        project_avatar_url = None
        if project_request.avatar_file_id:
            project_avatar_url = self.files_service.get_file_url(project_request.avatar_file_id)

        try:
            pdb = SyncSessionLocal()
            try:
                dn = pdb.scalar(
                    select(Profile.display_name).where(Profile.user_id == uid)
                )
                user_display_name = dn or "Unknown"
                self.activity_service.add_activity(
                    ActivityType.PROJECT,
                    project_id,
                    as_uuid(str(user_id)),
                    f"Project created by {user_display_name}",
                )
            finally:
                pdb.close()
        except Exception as e:
            logger.error(
                "Failed to record project creation activity: %s", e, exc_info=True
            )

        members = self._get_project_members(project_id)
        view = self._parse_view(row.view)

        return ProjectCreateResponse(
            id=project_id,
            name=row.name,
            org_id=as_uuid(str(row.org_id)),
            avatar_color=project_request.avatar_color,
            avatar_icon=project_request.avatar_icon,
            avatar_url=project_avatar_url,
            start_date=row.start_date or date.today(),
            end_date=row.end_date,
            view=view,
            progress_percentage=int(row.progress_percentage or 0),
            members=members,
            favourite_project=self._is_favourite_project(project_id, as_uuid(str(user_id))),
        )
        
    def get_projects(
        self,
        org_id: UUID4,
        user_id: UUID4,
        org_member_role: str,
        search: Optional[str] = None,
        order_by: Optional[ProjectOrderBy] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> AllProjectsResponse:
        oid, uid = StdUUID(str(org_id)), StdUUID(str(user_id))
        db = SyncSessionLocal()
        lim: Optional[int] = None
        off: Optional[int] = None
        try:
            stmt = select_member_projects(db, uid, oid).where(Project.archived.is_(False))
            if search:
                stmt = stmt.where(Project.name.ilike(f"%{search}%"))
            if order_by == ProjectOrderBy.ALPHABETICAL_ASC:
                stmt = stmt.order_by(Project.name.asc())
            elif order_by == ProjectOrderBy.ALPHABETICAL_DESC:
                stmt = stmt.order_by(Project.name.desc())
            elif order_by == ProjectOrderBy.DATE_CREATED_ASC:
                stmt = stmt.order_by(Project.created_at.asc())
            elif order_by == ProjectOrderBy.DATE_CREATED_DESC:
                stmt = stmt.order_by(Project.created_at.desc())
            else:
                stmt = stmt.order_by(Project.created_at.desc())

            lim, off, page_stmt = apply_sa_limit_offset(stmt, limit, offset)
            rows = list(db.scalars(page_stmt).all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get projects: {e}",
            )
        finally:
            db.close()

        projects = []
        for project in rows:
            avatar_url = (
                self.files_service.get_file_url(project.avatar_file_id)
                if project.avatar_file_id
                else None
            )
            project_id = as_uuid(str(project.id))
            members = self._get_project_members(project_id)
            view = self._parse_view(project.view)
            projects.append(
                ProjectGetResponse(
                    id=project_id,
                    name=project.name,
                    org_id=as_uuid(str(project.org_id)),
                    avatar_color=project.avatar_color,
                    avatar_icon=project.avatar_icon,
                    avatar_url=avatar_url,
                    start_date=project.start_date or date.today(),
                    end_date=project.end_date,
                    view=view,
                    progress_percentage=int(project.progress_percentage or 0),
                    members=members,
                    favourite_project=self._is_favourite_project(project_id, user_id),
                )
            )

        non_member_projects_count = self._get_non_member_projects_count(
            org_member_role, org_id, user_id
        )

        return AllProjectsResponse(
            member_projects=projects,
            non_member_projects_count=non_member_projects_count,
            total=len(projects),
            offset=off,
            limit=lim,
        )
    
    def get_archived_projects(
        self,
        org_id: UUID4,
        user_id: Optional[UUID4] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> ArchivedProjectsResponse:
        oid = StdUUID(str(org_id))
        db = SyncSessionLocal()
        lim: Optional[int] = None
        off: Optional[int] = None
        try:
            stmt = (
                select(Project)
                .where(Project.org_id == oid, Project.archived.is_(True))
                .order_by(Project.created_at.desc())
            )
            lim, off, page_stmt = apply_sa_limit_offset(stmt, limit, offset)
            rows = list(db.scalars(page_stmt).all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get archived projects: {e}",
            )
        finally:
            db.close()

        projects = []
        for project in rows:
            avatar_url = (
                self.files_service.get_file_url(project.avatar_file_id)
                if project.avatar_file_id
                else None
            )
            project_id = as_uuid(str(project.id))
            members = self._get_project_members(project_id)
            view = self._parse_view(project.view)
            projects.append(
                ProjectGetResponse(
                    id=project_id,
                    name=project.name,
                    org_id=as_uuid(str(project.org_id)),
                    avatar_color=project.avatar_color,
                    avatar_icon=project.avatar_icon,
                    avatar_url=avatar_url,
                    start_date=project.start_date or date.today(),
                    end_date=project.end_date,
                    view=view,
                    progress_percentage=int(project.progress_percentage or 0),
                    members=members,
                    archived=True,
                )
            )

        return ArchivedProjectsResponse(
            projects=projects,
            total=len(projects),
            offset=off,
            limit=lim,
        )
    
    def get_non_member_projects(
        self,
        org_id: UUID4,
        user_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> NonMemberProjectsResponse:
        oid, uid = StdUUID(str(org_id)), StdUUID(str(user_id))
        db = SyncSessionLocal()
        lim: Optional[int] = None
        off: Optional[int] = None
        try:
            stmt = (
                select_non_member_projects(db, oid, uid)
                .where(Project.archived.is_(False))
                .order_by(Project.created_at.desc())
            )
            lim, off, page_stmt = apply_sa_limit_offset(stmt, limit, offset)
            rows = list(db.scalars(page_stmt).all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get non-member projects: {e}",
            )
        finally:
            db.close()

        projects = []
        for project in rows:
            avatar_url = (
                self.files_service.get_file_url(project.avatar_file_id)
                if project.avatar_file_id
                else None
            )
            project_id = as_uuid(str(project.id))
            members = self._get_project_members(project_id)
            view = self._parse_view(project.view)
            projects.append(
                ProjectGetResponse(
                    id=project_id,
                    name=project.name,
                    org_id=as_uuid(str(project.org_id)),
                    avatar_color=project.avatar_color,
                    avatar_icon=project.avatar_icon,
                    avatar_url=avatar_url,
                    start_date=project.start_date or date.today(),
                    end_date=project.end_date,
                    view=view,
                    progress_percentage=int(project.progress_percentage or 0),
                    members=members,
                )
            )

        return NonMemberProjectsResponse(
            projects=projects,
            total=len(projects),
            offset=off,
            limit=lim,
        )
    
    
    def get_project(
        self,
        project_id: UUID4,
        user_id: Optional[UUID4] = None,
    ) -> ProjectGetResponse:
        pid = StdUUID(str(project_id))
        db = SyncSessionLocal()
        try:
            row = db.get(Project, pid)
        finally:
            db.close()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )

        avatar_url = (
            self.files_service.get_file_url(row.avatar_file_id)
            if row.avatar_file_id
            else None
        )
        members = self._get_project_members(project_id)
        view = self._parse_view(row.view)

        return ProjectGetResponse(
            id=as_uuid(str(row.id)),
            name=row.name,
            org_id=as_uuid(str(row.org_id)),
            avatar_color=row.avatar_color,
            avatar_icon=row.avatar_icon,
            avatar_url=avatar_url,
            start_date=row.start_date or date.today(),
            end_date=row.end_date,
            view=view,
            progress_percentage=int(row.progress_percentage or 0),
            members=members,
            favourite_project=self._is_favourite_project(project_id, user_id),
            archived=bool(row.archived),
        )
    
    def get_project_summary(
        self,
        project_id: UUID4,
        user_id: Optional[UUID4] = None,
    ) -> ProjectSummaryResponse:
        """
        Get comprehensive project summary with caching
        
        Returns:
            ProjectSummaryResponse with members, attachments, links, tasks, workload, and activities
        """
        project_id_str = str(project_id)
        
        # Try to get from cache first
        cached_summary = ProjectSummaryCache.get_summary(project_id_str)
        if cached_summary:
            try:
                # Ensure project info exists and has favourite_project field
                if not cached_summary.get('project'):
                    pdb = SyncSessionLocal()
                    try:
                        prow = pdb.get(Project, StdUUID(project_id_str))
                    finally:
                        pdb.close()
                    if prow:
                        avatar_url = (
                            self.files_service.get_file_url(prow.avatar_file_id)
                            if prow.avatar_file_id
                            else None
                        )
                        view = self._parse_view(prow.view)
                        project_info = ProjectResponse(
                            id=as_uuid(str(prow.id)),
                            name=prow.name,
                            org_id=as_uuid(str(prow.org_id)),
                            avatar_color=prow.avatar_color,
                            avatar_icon=prow.avatar_icon,
                            avatar_url=avatar_url,
                            start_date=prow.start_date or date.today(),
                            end_date=prow.end_date,
                            view=view,
                            progress_percentage=int(prow.progress_percentage or 0),
                            members=cached_summary.get("members", []),
                            favourite_project=self._is_favourite_project(
                                as_uuid(str(prow.id)), user_id
                            ),
                            archived=bool(prow.archived),
                        )
                        cached_summary["project"] = project_info.model_dump(mode="json")
                else:
                    # Update cached project with favourite_project field if missing or invalid
                    cached_project = cached_summary.get('project', {})
                    if isinstance(cached_project, dict):
                        # Ensure favourite_project is a boolean, not a list or other type
                        if 'favourite_project' not in cached_project or not isinstance(cached_project.get('favourite_project'), bool):
                            cached_project['favourite_project'] = self._is_favourite_project(project_id, user_id)
                            cached_summary['project'] = cached_project
                
                return ProjectSummaryResponse(**cached_summary)
            except Exception as e:
                logger.error(f"Error getting project summary: {e}")
                # If cache parsing fails, fall through to fetch from database
        
        # Cache miss - fetch all data
        try:
            pid = StdUUID(project_id_str)
            member_rows: List[Any] = []
            link_rows: List[Any] = []
            task_rows: List[Any] = []
            db = SyncSessionLocal()
            try:
                project_data_row = db.get(Project, pid)
                if not project_data_row:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Project not found",
                    )

                avatar_url = (
                    self.files_service.get_file_url(project_data_row.avatar_file_id)
                    if project_data_row.avatar_file_id
                    else None
                )
                view = self._parse_view(project_data_row.view)

                project_info = ProjectResponse(
                    id=as_uuid(str(project_data_row.id)),
                    name=project_data_row.name,
                    org_id=as_uuid(str(project_data_row.org_id)),
                    avatar_color=project_data_row.avatar_color,
                    avatar_icon=project_data_row.avatar_icon,
                    avatar_url=avatar_url,
                    start_date=project_data_row.start_date or date.today(),
                    end_date=project_data_row.end_date,
                    view=view,
                    progress_percentage=int(project_data_row.progress_percentage or 0),
                    members=[],
                    favourite_project=self._is_favourite_project(project_id, user_id),
                    archived=bool(project_data_row.archived),
                )

                member_rows = db.execute(
                    select(ProjectMemberRow.user_id, ProjectMemberRow.role).where(
                        ProjectMemberRow.project_id == pid
                    )
                ).all()

                link_rows = list(
                    db.scalars(
                        select(Link)
                        .where(
                            Link.entity_id == pid,
                            Link.entity_type == LinkEntityType.PROJECT.value,
                        )
                        .order_by(Link.created_at.desc())
                        .limit(5)
                    ).all()
                )

                task_rows = list(
                    db.scalars(
                        select(Task).where(
                            Task.project_id == pid,
                            Task.parent_id.is_(None),
                        )
                    ).all()
                )

            finally:
                db.close()

            members = []
            seen_user_ids = set()
            if member_rows:
                user_role_map = {
                    as_uuid(str(uid)): role for uid, role in member_rows
                }
                pm_user_ids = list(user_role_map.keys())

                unique_user_ids = []
                for pm_uid in pm_user_ids:
                    pm_uid_str = str(pm_uid)
                    if pm_uid_str not in seen_user_ids:
                        seen_user_ids.add(pm_uid_str)
                        unique_user_ids.append(pm_uid)

                user_info_cache = {}
                if unique_user_ids:
                    user_info_cache = self._batch_get_user_info(unique_user_ids)

                for pm_uid in unique_user_ids:
                    pm_uid_str = str(pm_uid)
                    user_info = user_info_cache.get(pm_uid_str) or {
                        "id": pm_uid_str,
                        "display_name": None,
                        "avatar_url": None,
                    }
                    role = user_role_map.get(pm_uid)
                    members.append(
                        ProjectMemberSummary(
                            id=pm_uid,
                            display_name=user_info.get("display_name"),
                            avatar_url=user_info.get("avatar_url"),
                            role=ProjectMemberRole(role) if role else None,
                        )
                    )
            
            # Update project_info with members
            project_info.members = members
            
            latest_links = []
            try:
                for lk in link_rows:
                    created_at = lk.created_at or datetime.now(timezone.utc)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    latest_links.append(
                        ProjectLinkSummary(
                            id=as_uuid(str(lk.id)),
                            title=lk.title,
                            link_url=str(lk.link_url or ""),
                            created_at=created_at,
                        )
                    )
            except Exception as e:
                logger.error("Failed to build project links: %s", e)
                latest_links = []

            now = datetime.now(timezone.utc)
            completed = 0
            incomplete = 0
            overdue = 0

            for t in task_rows:
                task_status = t.status
                due_date = t.due_date
                if task_status == TaskStatus.COMPLETED.value:
                    completed += 1
                else:
                    incomplete += 1
                    if due_date:
                        due_dt = due_date
                        if due_dt.tzinfo is None:
                            due_dt = due_dt.replace(tzinfo=timezone.utc)
                        if due_dt < now:
                            overdue += 1

            task_summary = TaskSummary(
                completed=completed,
                incomplete=incomplete,
                overdue=overdue,
            )

            total_tasks = len(task_rows)
            assigned_tasks = sum(1 for t in task_rows if t.assignee_id)
            unassigned_tasks = total_tasks - assigned_tasks

            assigned_percentage = (
                (assigned_tasks / total_tasks * 100) if total_tasks > 0 else 0.0
            )
            unassigned_percentage = (
                (unassigned_tasks / total_tasks * 100) if total_tasks > 0 else 0.0
            )

            user_task_counts: Dict[str, int] = {}
            for t in task_rows:
                if t.assignee_id:
                    aid = str(t.assignee_id)
                    user_task_counts[aid] = user_task_counts.get(aid, 0) + 1

            user_workloads = []
            if user_task_counts:
                wl_uids = [as_uuid(x) for x in user_task_counts.keys()]
                user_info_cache = self._batch_get_user_info(wl_uids)
                for assignee_key, task_count in user_task_counts.items():
                    wl_uid = as_uuid(assignee_key)
                    user_info = user_info_cache.get(assignee_key) or {
                        "id": assignee_key,
                        "display_name": None,
                        "avatar_url": None,
                    }
                    percentage = (
                        (task_count / total_tasks * 100) if total_tasks > 0 else 0.0
                    )
                    user_workloads.append(
                        UserWorkload(
                            user_id=wl_uid,
                            display_name=user_info.get("display_name"),
                            avatar_url=user_info.get("avatar_url"),
                            task_count=task_count,
                            percentage=round(percentage, 2),
                        )
                    )
            
            team_workload = TeamWorkload(
                assigned_percentage=round(assigned_percentage, 2),
                unassigned_percentage=round(unassigned_percentage, 2),
                user_workloads=user_workloads
            )
            
            # 6. Get top 10 recent activities
            activities = self.activity_service.get_activities(
                project_id,
                ActivityType.PROJECT,
                limit=10,
                offset=0
            )
            
            recent_activities = []
            for activity in activities[:10]:
                recent_activities.append(ActivityResponse(
                    id=activity.id,
                    user_display_name=activity.user_display_name,
                    user_avatar_url=activity.user_avatar_url,
                    description=activity.description,
                    activity_time=activity.activity_time
                ))
            
            # Build response
            summary = ProjectSummaryResponse(
                project=project_info,
                members=members,
                latest_links=latest_links,
                task_summary=task_summary,
                team_workload=team_workload,
                recent_activities=recent_activities
            )
            
            # Cache the result
            try:
                ProjectSummaryCache.set_summary(project_id_str, summary.model_dump(mode='json'))
            except Exception as e:
                logger.warning(f"Failed to cache project summary: {e}")
            
            return summary

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Error getting project summary: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get project summary: {e!s}",
            )
    
    def _get_user_info_with_cache(self, user_id: UUID4) -> Dict[str, Any]:
        """
        Get user information with Redis caching and avatar URL from avatar_file_id.
        Similar to ChatService._get_user_info_with_cache but for ProjectService.
        """
        user_id_str = str(user_id)
        
        try:
            # Try to get from cache first
            cached_user = UserCache.get_user(user_id_str)
            
            if cached_user:
                avatar_url = None
                if cached_user.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(as_uuid(cached_user['avatar_file_id']))
                    except Exception as e:
                        logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                
                return {
                    'id': cached_user.get('user_id') or cached_user.get('id'),
                    'display_name': cached_user.get('display_name'),
                    'avatar_url': avatar_url
                }
            
            pdb = SyncSessionLocal()
            try:
                prow = pdb.execute(
                    select(
                        Profile.user_id,
                        Profile.display_name,
                        Profile.email,
                        Profile.avatar_file_id,
                    ).where(Profile.user_id == StdUUID(user_id_str))
                ).one_or_none()
            finally:
                pdb.close()

            if prow:
                uid, display_name, email, avatar_file_id = prow
                avatar_url = None
                if avatar_file_id:
                    try:
                        avatar_url = self.files_service.get_file_url(as_uuid(str(avatar_file_id)))
                    except Exception as e:
                        logger.warning(
                            "Failed to get avatar URL for user %s: %s", user_id_str, e
                        )
                user_data_for_cache = {
                    "id": str(uid),
                    "display_name": display_name,
                    "email": email,
                    "avatar_file_id": str(avatar_file_id) if avatar_file_id else None,
                }
                UserCache.set_user(user_id_str, user_data_for_cache)
                return {
                    "id": str(uid),
                    "display_name": display_name,
                    "avatar_url": avatar_url,
                }
            return {
                "id": user_id_str,
                "display_name": None,
                "avatar_url": None,
            }
                
        except Exception as e:
            logger.error(f"Error getting user info for {user_id_str}: {str(e)}")
            return {
                'id': user_id_str,
                'display_name': None,
                'avatar_url': None
            }
    
    def _batch_get_user_info(self, user_ids: List[UUID4]) -> Dict[str, Dict[str, Any]]:
        """
        Batch fetch user information with Redis caching and avatar URLs.
        This method optimizes N+1 queries by fetching all users in a single database call.
        
        Args:
            user_ids: List of user IDs to fetch
            
        Returns:
            Dict mapping user_id (as string) to user info dict with id, display_name, and avatar_url
        """
        if not user_ids:
            return {}
        
        result = {}
        user_ids_str = [str(uid) for uid in user_ids]
        user_ids_to_fetch = []
        
        # First, try to get from cache
        for user_id_str in user_ids_str:
            try:
                cached_user = UserCache.get_user(user_id_str)
                if cached_user:
                    avatar_url = None
                    if cached_user.get('avatar_file_id'):
                        try:
                            avatar_url = self.files_service.get_file_url(as_uuid(cached_user['avatar_file_id']))
                        except Exception as e:
                            logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                    
                    result[user_id_str] = {
                        'id': cached_user.get('user_id') or cached_user.get('id'),
                        'display_name': cached_user.get('display_name'),
                        'avatar_url': avatar_url
                    }
                else:
                    user_ids_to_fetch.append(user_id_str)
            except Exception as e:
                logger.warning(f"Error getting user {user_id_str} from cache: {e}")
                user_ids_to_fetch.append(user_id_str)
        
        if user_ids_to_fetch:
            try:
                uuids = [StdUUID(x) for x in user_ids_to_fetch]
                pdb = SyncSessionLocal()
                try:
                    rows = pdb.execute(
                        select(
                            Profile.user_id,
                            Profile.display_name,
                            Profile.email,
                            Profile.avatar_file_id,
                        ).where(Profile.user_id.in_(uuids))
                    ).all()
                finally:
                    pdb.close()

                for row in rows:
                    uid, display_name, email, avatar_file_id = row
                    uid_str = str(uid)
                    avatar_url = None
                    if avatar_file_id:
                        try:
                            avatar_url = self.files_service.get_file_url(
                                as_uuid(str(avatar_file_id))
                            )
                        except Exception as e:
                            logger.warning(
                                "Failed to get avatar URL for user %s: %s", uid_str, e
                            )
                    user_data_for_cache = {
                        "id": uid_str,
                        "display_name": display_name,
                        "email": email,
                        "avatar_file_id": str(avatar_file_id) if avatar_file_id else None,
                    }
                    UserCache.set_user(uid_str, user_data_for_cache)
                    result[uid_str] = {
                        "id": uid_str,
                        "display_name": display_name,
                        "avatar_url": avatar_url,
                    }

                for uid_str in user_ids_to_fetch:
                    if uid_str not in result:
                        result[uid_str] = {
                            "id": uid_str,
                            "display_name": None,
                            "avatar_url": None,
                        }

            except Exception as e:
                logger.error("Error batch fetching user info: %s", e)
                for uid_str in user_ids_to_fetch:
                    if uid_str not in result:
                        result[uid_str] = {
                            "id": uid_str,
                            "display_name": None,
                            "avatar_url": None,
                        }
        
        return result
    
    def _is_favourite_project(self, project_id: UUID4, user_id: Optional[UUID4]) -> bool:
        """
        Check if a project is a favourite for a user.
        
        Args:
            project_id: The project ID to check
            user_id: The user ID to check (None if not available)
            
        Returns:
            bool: True if the project is a favourite, False otherwise
        """
        if not user_id:
            return False
        db = SyncSessionLocal()
        try:
            n = db.scalar(
                select(func.count())
                .select_from(FavouriteProject)
                .where(
                    FavouriteProject.project_id == StdUUID(str(project_id)),
                    FavouriteProject.user_id == StdUUID(str(user_id)),
                )
            )
            return bool(n and n > 0)
        except Exception as e:
            logger.warning("Error checking favourite project: %s", e)
            return False
        finally:
            db.close()
    
    def _get_project_members(self, project_id: UUID4) -> List[ProjectMemberSummary]:
        """
        Get project members with user info using Redis caching.
        Returns a list of ProjectMemberSummary with id, display_name, avatar_url, and role.
        """
        project_id_str = str(project_id)
        members = []

        try:
            db = SyncSessionLocal()
            try:
                rows = db.execute(
                    select(ProjectMemberRow.user_id, ProjectMemberRow.role).where(
                        ProjectMemberRow.project_id == StdUUID(project_id_str)
                    )
                ).all()
            finally:
                db.close()

            for uid, role in rows:
                m_uid = as_uuid(str(uid))
                user_info = self._get_user_info_with_cache(m_uid)
                members.append(
                    ProjectMemberSummary(
                        id=m_uid,
                        display_name=user_info.get("display_name"),
                        avatar_url=user_info.get("avatar_url"),
                        role=ProjectMemberRole(role) if role else None,
                    )
                )
        except Exception as e:
            logger.error(
                "Error getting project members for project %s: %s",
                project_id_str,
                e,
            )

        return members
    
    def get_project_members(self, project_id: UUID4) -> List[ProjectMemberSummary]:
        """
        Get project members with user info.
        Returns a list of ProjectMemberSummary with id, display_name, and avatar_url.
        """
        return self._get_project_members(project_id)
    
    def delete_project(
        self,
        project_id: UUID4,
    ) -> bool:
        pid = StdUUID(str(project_id))
        db = SyncSessionLocal()
        try:
            row = db.get(Project, pid)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found",
                )
            if not row.archived:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Project is not archived, cannot delete unarchived projects",
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check if project is archived: {e}",
            )
        finally:
            db.close()

        from app.services.attachment import AttachmentService, AttachmentType

        attachment_service = AttachmentService(self.files_service)
        try:
            attachment_service.delete_all(project_id, AttachmentType.PROJECT)
        except Exception as e:
            logger.warning("Failed to delete project attachments: %s", e)

        try:
            self.files_service.delete_permanently_all_files_by_project_id(project_id)
        except Exception as e:
            logger.warning("Failed to delete project files: %s", e)

        db = SyncSessionLocal()
        try:
            row = db.get(Project, pid)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found",
                )
            db.delete(row)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete project: {e}",
            )
        finally:
            db.close()

        return True
    
    def update_project(
        self,
        project_id: UUID4,
        project_request: ProjectUpdateRequest,
        user_id: Optional[UUID4] = None,
    ) -> ProjectUpdateResponse:
        pid = StdUUID(str(project_id))
        db = SyncSessionLocal()
        try:
            row = db.get(Project, pid)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found",
                )
            if project_request.name is not None:
                row.name = project_request.name
            if project_request.avatar_color is not None:
                row.avatar_color = project_request.avatar_color
            if project_request.avatar_icon is not None:
                row.avatar_icon = project_request.avatar_icon
            if project_request.start_date is not None:
                row.start_date = project_request.start_date
            if project_request.end_date is not None:
                row.end_date = project_request.end_date
            if project_request.view is not None:
                row.view = project_request.view.value
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(row)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update project: {e}",
            )
        finally:
            db.close()

        avatar_url = (
            self.files_service.get_file_url(row.avatar_file_id)
            if row.avatar_file_id
            else None
        )
        return ProjectUpdateResponse(
            id=as_uuid(str(row.id)),
            name=row.name,
            org_id=as_uuid(str(row.org_id)),
            avatar_color=row.avatar_color,
            avatar_icon=row.avatar_icon,
            avatar_url=avatar_url,
            start_date=row.start_date or date.today(),
            end_date=row.end_date,
            view=self._parse_view(row.view),
            progress_percentage=int(row.progress_percentage or 0),
            members=[],
            favourite_project=self._is_favourite_project(project_id, user_id),
            archived=bool(row.archived),
        )
    
    def update_project_optimized(
        self,
        project_id: UUID4,
        name: Optional[str] = None,
        avatar_file_id: Optional[UUID4] = None,
        avatar_color: Optional[str] = None,
        avatar_icon: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        user_id: Optional[UUID4] = None,
    ) -> ProjectUpdateResponse:
        """
        Optimized project update method.
        Updates name, avatar_file_id, avatar_color, avatar_icon, start_date, and end_date in a single operation.
        """
        
        pid = StdUUID(str(project_id))
        db = SyncSessionLocal()
        try:
            row = db.get(Project, pid)
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found",
                )
            if name is not None:
                row.name = name
            if avatar_file_id is not None:
                row.avatar_file_id = StdUUID(str(avatar_file_id))
            elif avatar_file_id is None and avatar_color is not None and avatar_icon is not None:
                row.avatar_file_id = None
            if avatar_color is not None:
                row.avatar_color = avatar_color
            if avatar_icon is not None:
                row.avatar_icon = avatar_icon
            if start_date is not None:
                row.start_date = start_date
            if end_date is not None:
                row.end_date = end_date
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(row)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update project: {e}",
            )
        finally:
            db.close()

        ProjectSummaryCache.delete_summary(str(project_id))

        avatar_url = None
        if row.avatar_file_id:
            try:
                avatar_url = self.files_service.get_file_url(as_uuid(str(row.avatar_file_id)))
            except HTTPException:
                pass

        return ProjectUpdateResponse(
            id=as_uuid(str(row.id)),
            name=row.name,
            org_id=as_uuid(str(row.org_id)),
            avatar_color=row.avatar_color,
            avatar_icon=row.avatar_icon,
            avatar_url=avatar_url,
            start_date=row.start_date or date.today(),
            end_date=row.end_date,
            view=self._parse_view(row.view),
            progress_percentage=int(row.progress_percentage or 0),
            members=[],
            favourite_project=self._is_favourite_project(project_id, user_id),
            archived=bool(row.archived),
        )
    
    def update_project_progress(self, project_id: UUID4) -> None:
        """
        Calculate and update project progress_percentage based on completed tasks.
        Only counts top-level tasks (where parent_id is null).
        Progress = (completed_tasks / total_tasks) * 100
        """
        pid = StdUUID(str(project_id))
        try:
            db = SyncSessionLocal()
            try:
                rows = list(
                    db.scalars(
                        select(Task).where(
                            Task.project_id == pid,
                            Task.parent_id.is_(None),
                        )
                    ).all()
                )
                total_tasks = len(rows)
                completed_tasks = sum(
                    1 for t in rows if t.status == TaskStatus.COMPLETED.value
                )
                progress_percentage = (
                    int((completed_tasks / total_tasks) * 100) if total_tasks else 0
                )
                db.execute(
                    update(Project)
                    .where(Project.id == pid)
                    .values(
                        progress_percentage=progress_percentage,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

            logger.debug(
                "Updated project %s progress to %s%% (%s/%s tasks completed)",
                project_id,
                progress_percentage,
                completed_tasks,
                total_tasks,
            )
            ProjectSummaryCache.delete_summary(str(project_id))
        except Exception as e:
            logger.error(
                "Error updating project progress for %s: %s",
                project_id,
                e,
                exc_info=True,
            )

    def join_project(
        self,
        project_id: UUID4,
        user_id: UUID4,
        org_id: UUID4,
    ) -> None:
        _ = org_id
        pid, uid = StdUUID(str(project_id)), StdUUID(str(user_id))
        now = datetime.now(timezone.utc)
        db = SyncSessionLocal()
        try:
            db.add(
                ProjectMemberRow(
                    project_id=pid,
                    user_id=uid,
                    role=ProjectMemberRole.MEMBER.value,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()
        except IntegrityError as e:
            db.rollback()
            code = getattr(e.orig, "pgcode", None)
            if code == "23505":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User is already a member of this project",
                )
            if code == "23503":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found",
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to join project: {e}",
            )
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to join project: {e}",
            )
        finally:
            db.close()

    def _add_project_member(
        self,
        project_id: UUID4,
        user_id: UUID4,
        role: ProjectMemberRole,
        added_by_id: Optional[UUID4] = None,
        skip_notification: bool = False,
    ) -> ProjectMember:
        logger.info(
            "Adding project member %s to project %s with role %s and added_by_id %s and skip_notification %s",
            user_id,
            project_id,
            role,
            added_by_id,
            skip_notification,
        )
        pid, uid = StdUUID(str(project_id)), StdUUID(str(user_id))
        role_value = role.value if isinstance(role, ProjectMemberRole) else str(role)
        db = SyncSessionLocal()
        try:
            existing = db.scalar(
                select(ProjectMemberRow).where(
                    ProjectMemberRow.project_id == pid,
                    ProjectMemberRow.user_id == uid,
                )
            )
            if existing:
                logger.info(
                    "User %s is already a member of project %s, skipping duplicate addition",
                    user_id,
                    project_id,
                )
                return self._project_member_row_to_api(existing, project_id)

            now = datetime.now(timezone.utc)
            m = ProjectMemberRow(
                project_id=pid,
                user_id=uid,
                role=role_value,
                created_at=now,
                updated_at=now,
            )
            db.add(m)
            db.commit()
            db.refresh(m)
            out = self._project_member_row_to_api(m, project_id)
        except IntegrityError as e:
            db.rollback()
            if getattr(e.orig, "pgcode", None) == "23505":
                db2 = SyncSessionLocal()
                try:
                    again = db2.scalar(
                        select(ProjectMemberRow).where(
                            ProjectMemberRow.project_id == pid,
                            ProjectMemberRow.user_id == uid,
                        )
                    )
                    if again:
                        logger.info(
                            "Duplicate project member prevented, returning existing member"
                        )
                        return self._project_member_row_to_api(again, project_id)
                finally:
                    db2.close()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add project member: {e}",
            )
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add project member: {e}",
            )
        finally:
            db.close()

        try:
            ProjectSummaryCache.delete_summary(str(project_id))
        except Exception as e:
            logger.warning("Failed to invalidate project summary cache: %s", e)

        if added_by_id and not skip_notification and str(added_by_id) != str(user_id):
            try:
                from app.utils.inbox_helpers import (
                    trigger_project_member_added_notification,
                )

                ndb = SyncSessionLocal()
                try:
                    prow = ndb.get(Project, pid)
                    project_name = prow.name if prow else "Unknown Project"
                    org_id_val = prow.org_id if prow else None
                    added_by_name = "Someone"
                    if org_id_val is not None:
                        dn = ndb.scalar(
                            select(Profile.display_name).where(
                                Profile.user_id == StdUUID(str(added_by_id))
                            )
                        )
                        if dn:
                            added_by_name = dn
                finally:
                    ndb.close()

                if org_id_val is not None:
                    trigger_project_member_added_notification(
                        user_id=user_id,
                        org_id=as_uuid(str(org_id_val)),
                        project_id=project_id,
                        project_name=project_name,
                        added_by_id=added_by_id,
                        added_by_name=added_by_name,
                    )
            except Exception as e:
                logger.warning("Failed to send project member added notification: %s", e)

        return out
    
    def add_project_member(
        self,
        project_id: UUID4,
        user_id: UUID4,
        role: ProjectMemberRole,
        added_by_id: Optional[UUID4] = None,
    ) -> ProjectMember:
        """
        Add a member to a project.
        Checks if user is already a member to avoid duplicates.
        """
        project_id_str = str(project_id)
        user_id_str = str(user_id)
        pid, uid = StdUUID(project_id_str), StdUUID(user_id_str)

        db = SyncSessionLocal()
        try:
            n = db.scalar(
                select(func.count())
                .select_from(ProjectMemberRow)
                .where(
                    ProjectMemberRow.project_id == pid,
                    ProjectMemberRow.user_id == uid,
                )
            )
            if n and n > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="User is already a member of this project",
                )

            prow = db.get(Project, pid)
            if not prow:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found",
                )
            project_org_id = prow.org_id

            om = db.scalar(
                select(func.count())
                .select_from(OrganizationMember)
                .where(
                    OrganizationMember.org_id == project_org_id,
                    OrganizationMember.user_id == uid,
                )
            )
            if not om:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User must be a member of the organization to be added to the project",
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify organization membership: {e}",
            )
        finally:
            db.close()

        return self._add_project_member(project_id, user_id, role, added_by_id=added_by_id)
    
    def remove_project_member(
        self,
        project_id: UUID4,
        user_id: UUID4,
        removed_by_id: Optional[UUID4] = None,
    ) -> None:
        """
        Remove a member from a project.
        
        Args:
            project_id: The project ID
            user_id: The user ID to remove
            removed_by_id: The user ID who is removing the member (for activity logging)
        
        Raises:
            HTTPException: If member not found, is the last owner, or removal fails
        """
        project_id_str = str(project_id)
        user_id_str = str(user_id)
        pid, uid = StdUUID(project_id_str), StdUUID(user_id_str)

        db = SyncSessionLocal()
        try:
            mrow = db.scalar(
                select(ProjectMemberRow).where(
                    ProjectMemberRow.project_id == pid,
                    ProjectMemberRow.user_id == uid,
                )
            )
            if not mrow:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User is not a member of this project",
                )
            member_role = mrow.role
            if member_role == ProjectMemberRole.OWNER.value:
                owner_count = db.scalar(
                    select(func.count())
                    .select_from(ProjectMemberRow)
                    .where(
                        ProjectMemberRow.project_id == pid,
                        ProjectMemberRow.role == ProjectMemberRole.OWNER.value,
                    )
                )
                if owner_count is not None and owner_count <= 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cannot remove the last owner of the project",
                    )

            res = db.execute(
                delete(ProjectMemberRow).where(
                    ProjectMemberRow.project_id == pid,
                    ProjectMemberRow.user_id == uid,
                )
            )
            if not res.rowcount:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Member not found or already removed",
                )
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to remove project member: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to remove project member: {e}",
            )
        finally:
            db.close()

        try:
            ProjectSummaryCache.delete_summary(project_id_str)
        except Exception as e:
            logger.warning("Failed to invalidate project summary cache: %s", e)

        if removed_by_id:
            try:
                pdb = SyncSessionLocal()
                try:
                    pname = pdb.scalar(select(Project.name).where(Project.id == pid))
                finally:
                    pdb.close()
                project_name = pname or "Project"

                removed_u = UserCache.get_user(user_id_str) or {}
                remover_u = UserCache.get_user(str(removed_by_id)) or {}
                removed_user_name = removed_u.get("display_name") or "User"
                remover_name = remover_u.get("display_name") or "User"

                self.activity_service.add_activity(
                    ActivityType.PROJECT,
                    project_id,
                    removed_by_id,
                    f"{remover_name} removed {removed_user_name} from project {project_name}",
                )
            except Exception as e:
                logger.warning(
                    "Failed to log project member removal activity: %s", e
                )

    def get_favourite_projects(
        self,
        user_id: UUID4,
        org_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> FavouriteProjectsResponse:
        oid, uid = StdUUID(str(org_id)), StdUUID(str(user_id))
        db = SyncSessionLocal()
        lim: Optional[int] = None
        off: Optional[int] = None
        try:
            base = (
                select(Project)
                .join(
                    FavouriteProject,
                    FavouriteProject.project_id == Project.id,
                )
                .where(
                    FavouriteProject.user_id == uid,
                    Project.org_id == oid,
                    Project.archived.is_(False),
                )
            )
            total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
            stmt = base.order_by(Project.created_at.desc())
            lim, off, page_stmt = apply_sa_limit_offset(stmt, limit, offset)
            rows = list(db.scalars(page_stmt).all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get favourite projects: {e}",
            )
        finally:
            db.close()

        projects = []
        for project_data in rows:
            avatar_url = (
                self.files_service.get_file_url(project_data.avatar_file_id)
                if project_data.avatar_file_id
                else None
            )
            project_id = as_uuid(str(project_data.id))
            members = self._get_project_members(project_id)
            view = self._parse_view(project_data.view)
            projects.append(
                ProjectGetResponse(
                    id=project_id,
                    name=project_data.name,
                    org_id=as_uuid(str(project_data.org_id)),
                    avatar_color=project_data.avatar_color,
                    avatar_icon=project_data.avatar_icon,
                    avatar_url=avatar_url,
                    start_date=project_data.start_date or date.today(),
                    end_date=project_data.end_date,
                    view=view,
                    progress_percentage=int(project_data.progress_percentage or 0),
                    members=members,
                    favourite_project=True,
                )
            )

        return FavouriteProjectsResponse(
            projects=projects,
            total=int(total),
            offset=off,
            limit=lim,
        )
    
    def get_recent_projects(
        self,
        org_id: UUID4,
        user_id: UUID4,
    ) -> RecentProjectsResponse:
        """
        Get 5 most recent projects (by created_at) that the user is a member of.
        Returns only id and name for sidebar display.
        """
        oid, uid = StdUUID(str(org_id)), StdUUID(str(user_id))
        db = SyncSessionLocal()
        try:
            stmt = (
                select_member_projects(db, uid, oid)
                .where(Project.archived.is_(False))
                .order_by(Project.created_at.desc())
                .limit(5)
            )
            rows = list(db.scalars(stmt).all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get recent projects: {e}",
            )
        finally:
            db.close()

        projects = [
            RecentProjectResponse(id=as_uuid(str(r.id)), name=r.name) for r in rows
        ]
        return RecentProjectsResponse(projects=projects)

    def _get_non_member_projects_count(
        self,
        org_member_role: str,
        org_id: UUID4,
        user_id: UUID4,
    ) -> int:
        if org_member_role == OrganizationMemberRole.MEMBER.value:
            return 0
        oid, uid = StdUUID(str(org_id)), StdUUID(str(user_id))
        db = SyncSessionLocal()
        try:
            total_in_org = db.scalar(
                select(func.count())
                .select_from(Project)
                .where(Project.org_id == oid, Project.archived.is_(False))
            ) or 0
            member_in_org = db.scalar(
                select(func.count())
                .select_from(Project)
                .join(
                    ProjectMemberRow,
                    ProjectMemberRow.project_id == Project.id,
                )
                .where(
                    ProjectMemberRow.user_id == uid,
                    Project.org_id == oid,
                    Project.archived.is_(False),
                )
            ) or 0
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get total projects count: {e}",
            )
        finally:
            db.close()

        return max(0, int(total_in_org) - int(member_in_org))