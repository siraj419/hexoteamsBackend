from fastapi import HTTPException, status
from pydantic import UUID4
from supabase_auth.errors import AuthApiError
from typing import Optional, List, Any, Dict
from datetime import datetime, timezone, date
from fastapi import UploadFile

from app.core import supabase
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
)
from app.schemas.organizations import OrganizationMemberRole
from app.services.files import FilesService
from app.services.activity import ActivityService, ActivityType
from app.services.link import LinkService, LinkEntityType
from app.utils import random_color, random_icon, calculate_file_size
from app.utils.redis_cache import ProjectSummaryCache, UserCache
from app.schemas.activities import ActivityResponse
from app.schemas.tasks import TaskStatus
import logging

from app.core import settings

logger = logging.getLogger(__name__)

class ProjectService:
    def __init__(self):
        self.files_service = FilesService()
        self.activity_service = ActivityService(self.files_service)
    
    def change_project_avatar(
        self,
        user_id: UUID4,
        org_id: UUID4,
        file: UploadFile,
        project_id: Optional[UUID4] = None,
    ) -> ProjectChangeAvatarResponse:
        
        if project_id:
            try:
                response = supabase.table('projects').select('avatar_file_id').eq('id', project_id).execute()
            except AuthApiError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to get project: {e}"
                )
            
            if not response.data or len(response.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found"
                )
            
            avatar_file_id = response.data[0]['avatar_file_id']
        else:
            avatar_file_id = None
            
        
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
            avatar_url=self.files_service.get_file_url(file_data['id']),
        )
    
    def archive_project(
        self,
        project_id: UUID4,
    ) -> bool:
        # archive the project
        try:
            response = supabase.table('projects').update({
                'archived': True,
            }).eq('id', project_id).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to archive project: {e}"
            )
        
        return True
    
    def restore_project(
        self,
        project_id: UUID4,
    ) -> bool:
        # restore the project
        try:
            response = supabase.table('projects').update({
                'archived': False,
            }).eq('id', project_id).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to restore project: {e}"
            )
        
        return True
    
    def toggle_project_favourite(
        self,
        project_id: UUID4,
        user_id: UUID4,
    ) -> bool:
        
        # delete if the project is already a favourite
        try:
            response = supabase.table('favourite_projects').delete().eq('project_id', project_id).eq('user_id', user_id).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete project from favourites: {e}"
            )
        
        # add the project to favourites
        if not response.data or len(response.data) == 0:
            try:
                response = supabase.table('favourite_projects').insert({
                    'project_id': str(project_id),
                    'user_id': str(user_id),
                }).execute()
            except Exception as e:
                if e.code == '23503': # foreign key violation
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Project or user not found"
                    )
                    
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to add project to favourites: {e}"
                )
        
        
        return True
    
    def create_project( 
        self,
        project_request: ProjectCreateRequest,
        org_id: UUID4,
        user_id: UUID4,
    ) -> ProjectCreateResponse:
        
        # check if the project name is already taken
        try:
            response = (
                supabase.table('projects')
                .select('*')
                .ilike('name', f"%{project_request.name}%")
                .eq('org_id', org_id)
                .execute()
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check if project name is already taken: {e}"
            )
        
        if response.data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project name already taken"
            )
        
        if not project_request.avatar_icon:
            project_request.avatar_icon = random_icon()
        if not project_request.avatar_color:
            project_request.avatar_color = random_color()
        
        
        # create the project
        try:
            response = supabase.table('projects').insert({
                'name': project_request.name,
                'org_id': org_id,
                'avatar_color': project_request.avatar_color,
                'avatar_icon': project_request.avatar_icon,
                'avatar_file_id': project_request.avatar_file_id,
                'start_date': project_request.start_date.isoformat(),
                'end_date': project_request.end_date.isoformat() if project_request.end_date else None,
                'view': project_request.view.value if project_request.view else ProjectTasksView.LIST.value,
                'progress_percentage': 0,
                'created_by': user_id,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create project: {e}"
            )
        
        
        # add the project member
        self._add_project_member(response.data[0]['id'], user_id, ProjectMemberRole.OWNER.value)
        
        # update the project avatar file id if provided
        if project_request.avatar_file_id:
            self.files_service.update_file_project_id(project_request.avatar_file_id, response.data[0]['id'])
        
        # get the avatar file url if avatar file id is provided
        project_avatar_url = None
        if project_request.avatar_file_id:
            project_avatar_url = self.files_service.get_file_url(project_request.avatar_file_id)
        
        # Record activity: project created
        try:
            profile_response = supabase.table('profiles').select('display_name').eq('user_id', str(user_id)).execute()
            if profile_response.data and len(profile_response.data) > 0:
                user_display_name = profile_response.data[0].get('display_name', 'Unknown')
                self.activity_service.add_activity(
                    ActivityType.PROJECT,
                    UUID4(response.data[0]['id']),
                    user_id,
                    f"Project created by {user_display_name}"
                )
        except Exception as e:
            logger.error(f"Failed to record project creation activity: {str(e)}", exc_info=True)
        
        project_id = UUID4(response.data[0]['id'])
        members = self._get_project_members(project_id)
        
        return ProjectCreateResponse(
            id=project_id,
            name=response.data[0]['name'],
            org_id=response.data[0]['org_id'],
            avatar_color=project_request.avatar_color,
            avatar_icon=project_request.avatar_icon,
            avatar_url=project_avatar_url,
            start_date=response.data[0]['start_date'],
            end_date=response.data[0]['end_date'],
            view=response.data[0]['view'],
            progress_percentage=response.data[0]['progress_percentage'],
            members=members,
            favourite_project=self._is_favourite_project(project_id, user_id),
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
        
        # get all projects in which the user is a member (using a custom RPC function)
        query = supabase.rpc('get_member_projects', {
            'user_id': str(user_id),
            'org_id': str(org_id),
        })
        
        
        
        # filter out archived projects
        query = query.eq('archived', False)
        
        # search for projects by name
        if search:
            query = query.ilike('name', f'%{search}%')
        
        # order by name or date created
        if order_by:
            if order_by == ProjectOrderBy.ALPHABETICAL_ASC:
                query = query.order('name', desc=False)
            elif order_by == ProjectOrderBy.ALPHABETICAL_DESC:
                query = query.order('name', desc=True)
            elif order_by == ProjectOrderBy.DATE_CREATED_ASC:
                query = query.order('created_at', desc=False)
            elif order_by == ProjectOrderBy.DATE_CREATED_DESC:
                query = query.order('created_at', desc=True)
        
        # Optional limit and offset (in case if needed)
        limit, offset, query = self._apply_pagination(query, limit, offset)
            
        try:
            response = query.execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get projects: {e}"
            )
        
        projects = []
        for project in response.data:
            avatar_url = None
            if project['avatar_file_id']:
                avatar_url = self.files_service.get_file_url(project['avatar_file_id'])
            
            project_id = UUID4(project['id'])
            members = self._get_project_members(project_id)
                
            projects.append(
                ProjectGetResponse(
                    id=project_id,
                    name=project['name'],
                    org_id=project['org_id'],
                    avatar_color=project['avatar_color'],
                    avatar_icon=project['avatar_icon'],
                    avatar_url=avatar_url,
                    start_date=project['start_date'],
                    end_date=project['end_date'],
                    view=project['view'],
                    progress_percentage=project['progress_percentage'],
                    members=members,
                    favourite_project=False,  # Not included in get_projects response
                )
            )
        
        # get non-member projects count
        non_member_projects_count = self._get_non_member_projects_count(org_member_role, org_id, user_id)
        
        return AllProjectsResponse(
            member_projects=projects,
            non_member_projects_count=non_member_projects_count,
            total=len(projects),
            offset=offset,
            limit=limit,
        )
    
    def get_archived_projects(
        self,
        org_id: UUID4,
        user_id: Optional[UUID4] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> ArchivedProjectsResponse:
        # get all archived projects
        query = supabase.table('projects').select('*').eq('org_id', org_id).eq('archived', True)
        
        # apply pagination
        limit, offset, query = self._apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get archived projects: {e}"
            )
        
        projects = []
        for project in response.data:
            avatar_url = None
            if project['avatar_file_id']:
                avatar_url = self.files_service.get_file_url(project['avatar_file_id'])
            
            project_id = UUID4(project['id'])
            members = self._get_project_members(project_id)
            
            projects.append(ProjectGetResponse(
                id=project_id,
                name=project['name'],
                org_id=project['org_id'],
                avatar_color=project['avatar_color'],
                avatar_icon=project['avatar_icon'],
                avatar_url=avatar_url,
                start_date=project['start_date'],
                end_date=project['end_date'],
                view=project['view'],
                progress_percentage=project['progress_percentage'],
                members=members,
            ))
        
        return ArchivedProjectsResponse(
            projects=projects,
            total=len(projects),
            offset=offset,
            limit=limit,
        )
    
    def get_non_member_projects(
        self,
        org_id: UUID4,
        user_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> NonMemberProjectsResponse:
        # get all projects in which the user is not a member (using a custom RPC function)
        query = supabase.rpc('get_non_member_projects', {
            'org_id': org_id,
            'user_id': user_id,
        })
        
        # filter out archived projects
        query = query.eq('archived', False)
        
        # apply pagination
        limit, offset, query = self._apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get non-member projects: {e}"
            )
        
        projects = []
        for project in response.data:
            avatar_url = None
            if project['avatar_file_id']:
                avatar_url = self.files_service.get_file_url(project['avatar_file_id'])
            
            project_id = UUID4(project['id'])
            members = self._get_project_members(project_id)
            
            projects.append(ProjectGetResponse(
                id=project_id,
                name=project['name'],
                org_id=project['org_id'],
                avatar_color=project['avatar_color'],
                avatar_icon=project['avatar_icon'],
                avatar_url=avatar_url,
                start_date=project['start_date'],
                end_date=project['end_date'],
                view=project['view'],
                progress_percentage=project['progress_percentage'],
                members=members,
            ))
        
        return NonMemberProjectsResponse(
            projects=projects,
            total=len(projects),
            offset=offset,
            limit=limit,
        )
    
    
    def get_project(
        self,
        project_id: UUID4,
        user_id: Optional[UUID4] = None,
    ) -> ProjectGetResponse:
        try:
            response = supabase.table('projects').select('*').eq('id', project_id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get project: {e}"
            )
            
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )
            
        avatar_url = None
        if response.data[0].get('avatar_file_id'):
            avatar_url = self.files_service.get_file_url(response.data[0]['avatar_file_id'])
        
        members = self._get_project_members(project_id)
        
        view_value = response.data[0].get('view', ProjectTasksView.LIST.value)
        if isinstance(view_value, str):
            try:
                view = ProjectTasksView(view_value)
            except ValueError:
                view = ProjectTasksView.LIST
        else:
            view = ProjectTasksView.LIST
        
        return ProjectGetResponse(
            id=UUID4(response.data[0]['id']),
            name=response.data[0]['name'],
            org_id=UUID4(response.data[0]['org_id']),
            avatar_color=response.data[0].get('avatar_color'),
            avatar_icon=response.data[0].get('avatar_icon'),
            avatar_url=avatar_url,
            start_date=response.data[0]['start_date'],
            end_date=response.data[0].get('end_date'),
            view=view,
            progress_percentage=response.data[0].get('progress_percentage', 0),
            members=members,
            favourite_project=self._is_favourite_project(project_id, user_id),
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
                    # Fetch project info and add to cached summary
                    project_response = supabase.table('projects').select('*').eq('id', project_id_str).execute()
                    if project_response.data and len(project_response.data) > 0:
                        project_data = project_response.data[0]
                        avatar_url = None
                        if project_data.get('avatar_file_id'):
                            avatar_url = self.files_service.get_file_url(project_data['avatar_file_id'])
                        
                        view_value = project_data.get('view', ProjectTasksView.LIST.value)
                        if isinstance(view_value, str):
                            try:
                                view = ProjectTasksView(view_value)
                            except ValueError:
                                view = ProjectTasksView.LIST
                        else:
                            view = ProjectTasksView.LIST
                        
                        project_info = ProjectResponse(
                            id=UUID4(project_data['id']),
                            name=project_data['name'],
                            org_id=UUID4(project_data['org_id']),
                            avatar_color=project_data.get('avatar_color'),
                            avatar_icon=project_data.get('avatar_icon'),
                            avatar_url=avatar_url,
                            start_date=project_data['start_date'],
                            end_date=project_data.get('end_date'),
                            view=view,
                            progress_percentage=project_data.get('progress_percentage', 0),
                            members=cached_summary.get('members', []),
                            favourite_project=self._is_favourite_project(UUID4(project_data['id']), user_id),
                        )
                        cached_summary['project'] = project_info.model_dump(mode='json')
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
            # 0. Get project basic information
            project_response = supabase.table('projects').select('*').eq('id', project_id_str).execute()
            if not project_response.data or len(project_response.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found"
                )
            
            project_data = project_response.data[0]
            avatar_url = None
            if project_data.get('avatar_file_id'):
                avatar_url = self.files_service.get_file_url(project_data['avatar_file_id'])
            
            view_value = project_data.get('view', ProjectTasksView.LIST.value)
            if isinstance(view_value, str):
                try:
                    view = ProjectTasksView(view_value)
                except ValueError:
                    view = ProjectTasksView.LIST
            else:
                view = ProjectTasksView.LIST
            
            project_info = ProjectResponse(
                id=UUID4(project_data['id']),
                name=project_data['name'],
                org_id=UUID4(project_data['org_id']),
                avatar_color=project_data.get('avatar_color'),
                avatar_icon=project_data.get('avatar_icon'),
                avatar_url=avatar_url,
                start_date=project_data['start_date'],
                end_date=project_data.get('end_date'),
                view=view,
                progress_percentage=project_data.get('progress_percentage', 0),
                members=[],  # Will be populated below
                favourite_project=self._is_favourite_project(project_id, user_id),
            )
            
            # 1. Get project members with user info (handle duplicates)
            members_response = supabase.table('project_members').select(
                'user_id'
            ).eq('project_id', project_id_str).execute()
            
            members = []
            seen_user_ids = set()
            if members_response.data:
                user_ids = [UUID4(member['user_id']) for member in members_response.data]
                for user_id in user_ids:
                    user_id_str = str(user_id)
                    # Skip duplicates
                    if user_id_str in seen_user_ids:
                        continue
                    seen_user_ids.add(user_id_str)
                    
                    user_info = self._get_user_info_with_cache(user_id)
                    members.append(ProjectMemberSummary(
                        id=user_id,
                        display_name=user_info.get('display_name'),
                        avatar_url=user_info.get('avatar_url')
                    ))
            
            # Update project_info with members
            project_info.members = members
            
            # 2. Get top 5 latest project links
            try:
                links_response = supabase.table('links').select(
                    'id, title, link_url, created_at'
                ).eq('entity_id', project_id_str).eq('entity_type', LinkEntityType.PROJECT.value).order(
                    'created_at', desc=True
                ).limit(5).execute()
                
                latest_links = []
                if links_response.data:
                    for link_data in links_response.data:
                        try:
                            created_at_str = link_data.get('created_at')
                            if created_at_str:
                                if isinstance(created_at_str, str):
                                    created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                                    if created_at.tzinfo is None:
                                        created_at = created_at.replace(tzinfo=timezone.utc)
                                else:
                                    created_at = datetime.now(timezone.utc)
                            else:
                                created_at = datetime.now(timezone.utc)
                        except Exception as e:
                            logger.warning(f"Failed to parse link created_at: {e}")
                            created_at = datetime.now(timezone.utc)
                        
                        latest_links.append(ProjectLinkSummary(
                            id=UUID4(link_data['id']),
                            title=link_data.get('title'),
                            link_url=str(link_data.get('link_url', '')),
                            created_at=created_at
                        ))
            except Exception as e:
                logger.error(f"Failed to get project links: {str(e)}")
                latest_links = []
            
            # 4. Get task summary
            now = datetime.now(timezone.utc)
            tasks_response = supabase.table('tasks').select(
                'id, status, due_date, assignee_id'
            ).eq('project_id', project_id_str).is_('parent_id', 'null').execute()
            
            completed = 0
            incomplete = 0
            overdue = 0
            
            if tasks_response.data:
                for task in tasks_response.data:
                    task_status = task.get('status')
                    due_date = task.get('due_date')
                    
                    if task_status == TaskStatus.COMPLETED.value:
                        completed += 1
                    else:
                        incomplete += 1
                        if due_date:
                            try:
                                due_dt = datetime.fromisoformat(due_date.replace('Z', '+00:00'))
                                if due_dt.tzinfo is None:
                                    due_dt = due_dt.replace(tzinfo=timezone.utc)
                                if due_dt < now:
                                    overdue += 1
                            except:
                                pass
            
            task_summary = TaskSummary(
                completed=completed,
                incomplete=incomplete,
                overdue=overdue
            )
            
            # 5. Get team workload
            total_tasks = len(tasks_response.data) if tasks_response.data else 0
            assigned_tasks = sum(1 for task in tasks_response.data if task.get('assignee_id')) if tasks_response.data else 0
            unassigned_tasks = total_tasks - assigned_tasks
            
            assigned_percentage = (assigned_tasks / total_tasks * 100) if total_tasks > 0 else 0.0
            unassigned_percentage = (unassigned_tasks / total_tasks * 100) if total_tasks > 0 else 0.0
            
            # Count tasks per user
            user_task_counts = {}
            if tasks_response.data:
                for task in tasks_response.data:
                    assignee_id = task.get('assignee_id')
                    if assignee_id:
                        user_task_counts[assignee_id] = user_task_counts.get(assignee_id, 0) + 1
            
            user_workloads = []
            for user_id_str, task_count in user_task_counts.items():
                user_id = UUID4(user_id_str)
                user_info = self._get_user_info_with_cache(user_id)
                percentage = (task_count / total_tasks * 100) if total_tasks > 0 else 0.0
                user_workloads.append(UserWorkload(
                    user_id=user_id,
                    display_name=user_info.get('display_name'),
                    avatar_url=user_info.get('avatar_url'),
                    task_count=task_count,
                    percentage=round(percentage, 2)
                ))
            
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
            
        except Exception as e:
            logger.error(f"Error getting project summary: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get project summary: {str(e)}"
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
                        avatar_url = self.files_service.get_file_url(UUID4(cached_user['avatar_file_id']))
                    except Exception as e:
                        logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                
                return {
                    'id': cached_user.get('user_id') or cached_user.get('id'),
                    'display_name': cached_user.get('display_name'),
                    'avatar_url': avatar_url
                }
            
            # Cache miss - fetch from database
            user_response = supabase.table('profiles').select('user_id, display_name, avatar_file_id').eq(
                'user_id', user_id_str
            ).execute()
            
            if user_response.data and len(user_response.data) > 0:
                user = user_response.data[0]
                
                avatar_url = None
                if user.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(UUID4(user['avatar_file_id']))
                    except Exception as e:
                        logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                
                user_data_for_cache = {
                    'user_id': user['user_id'],
                    'display_name': user.get('display_name'),
                    'avatar_file_id': user.get('avatar_file_id')
                }
                
                UserCache.set_user(user_id_str, user_data_for_cache)
                
                return {
                    'id': user['user_id'],
                    'display_name': user.get('display_name'),
                    'avatar_url': avatar_url
                }
            else:
                return {
                    'id': user_id_str,
                    'display_name': None,
                    'avatar_url': None
                }
                
        except Exception as e:
            logger.error(f"Error getting user info for {user_id_str}: {str(e)}")
            return {
                'id': user_id_str,
                'display_name': None,
                'avatar_url': None
            }
    
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
        
        try:
            response = supabase.table('favourite_projects').select('id').eq('project_id', str(project_id)).eq('user_id', str(user_id)).execute()
            return response.data and len(response.data) > 0
        except Exception:
            return False
    
    def _get_project_members(self, project_id: UUID4) -> List[ProjectMemberSummary]:
        """
        Get project members with user info using Redis caching.
        Returns a list of ProjectMemberSummary with id, display_name, and avatar_url.
        """
        project_id_str = str(project_id)
        members = []
        
        try:
            members_response = supabase.table('project_members').select(
                'user_id'
            ).eq('project_id', project_id_str).execute()
            
            if members_response.data:
                user_ids = [UUID4(member['user_id']) for member in members_response.data]
                for user_id in user_ids:
                    user_info = self._get_user_info_with_cache(user_id)
                    members.append(ProjectMemberSummary(
                        id=user_id,
                        display_name=user_info.get('display_name'),
                        avatar_url=user_info.get('avatar_url')
                    ))
        except Exception as e:
            logger.error(f"Error getting project members for project {project_id_str}: {str(e)}")
        
        return members
    
    def delete_project(
        self,
        project_id: UUID4
    ) -> bool:
        
        try:
            is_archived = supabase.table('projects').select('archived').eq('id', project_id).execute().data[0]['archived']
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check if project is archived: {e}"
            )
        
        if not is_archived:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project is not archived, cannot delete unarchived projects"
            )
        
        # Delete project attachments from database only (not from S3)
        from app.services.attachment import AttachmentService, AttachmentType
        attachment_service = AttachmentService(self.files_service)
        try:
            attachment_service.delete_all(project_id, AttachmentType.PROJECT)
        except Exception as e:
            logger.warning(f"Failed to delete project attachments: {e}")
            # Continue with project deletion even if attachment deletion fails
        
        # Delete project file records from database only (not from S3)
        try:
            self.files_service.delete_permanently_all_files_by_project_id(project_id)
        except Exception as e:
            logger.warning(f"Failed to delete project files: {e}")
            # Continue with project deletion even if file deletion fails
        
        try:
            response = supabase.table('projects').delete().eq('id', project_id).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete project: {e}"
            )
    
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )
            
        return True
    
    def update_project(
        self,
        project_id: UUID4,
        project_request: ProjectUpdateRequest,
        user_id: Optional[UUID4] = None,
    ) -> ProjectUpdateResponse:
        
        updates = {}
        if project_request.name:
            updates['name'] = project_request.name
        if project_request.avatar_color:
            updates['avatar_color'] = project_request.avatar_color
        if project_request.avatar_icon:
            updates['avatar_icon'] = project_request.avatar_icon
        if project_request.avatar_file_id:
            updates['avatar_file_id'] = project_request.avatar_file_id
        if project_request.start_date:
            updates['start_date'] = project_request.start_date
        if project_request.end_date:
            updates['end_date'] = project_request.end_date
        if project_request.view:
            updates['view'] = project_request.view
        updates['updated_at'] = datetime.now(timezone.utc)
        
        try:
            response = supabase.table('projects').update(updates).eq('id', project_id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update project: {e}"
            )
            
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )
            
        avatar_url = None
        if response.data[0]['avatar_file_id']:
            avatar_url = self.files_service.get_file_url(response.data[0]['avatar_file_id'])
            
        return ProjectUpdateResponse(
            id=response.data[0]['id'],
            name=response.data[0]['name'],
            org_id=response.data[0]['org_id'],
            avatar_color=response.data[0]['avatar_color'],
            avatar_icon=response.data[0]['avatar_icon'],
            avatar_url=avatar_url,
            start_date=response.data[0]['start_date'],
            end_date=response.data[0]['end_date'],
            view=response.data[0]['view'],
            status=response.data[0]['status'],
            created_at=response.data[0]['created_at'],
            updated_at=response.data[0]['updated_at'],
            favourite_project=self._is_favourite_project(project_id, user_id),
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
        
        # Build update dictionary - only include fields that are being updated
        updates = {
            'updated_at': datetime.now(timezone.utc).isoformat()
        }
        
        if name is not None:
            updates['name'] = name
        if avatar_file_id is not None:
            updates['avatar_file_id'] = str(avatar_file_id)
        if avatar_color is not None:
            updates['avatar_color'] = avatar_color
        if avatar_icon is not None:
            updates['avatar_icon'] = avatar_icon
        if start_date is not None:
            updates['start_date'] = start_date.isoformat() if isinstance(start_date, date) else start_date
        if end_date is not None:
            updates['end_date'] = end_date.isoformat() if isinstance(end_date, date) else end_date
        
        # Single optimized update query
        try:
            response = supabase.table('projects').update(updates).eq('id', str(project_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update project: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )
        
        # Invalidate project summary cache
        ProjectSummaryCache.delete_summary(str(project_id))
        
        # Get avatar URL efficiently
        updated_project = response.data[0]
        avatar_url = None
        if updated_project.get('avatar_file_id'):
            try:
                avatar_url = self.files_service.get_file_url(UUID4(updated_project['avatar_file_id']))
            except HTTPException:
                pass
        
        return ProjectUpdateResponse(
            id=updated_project['id'],
            name=updated_project['name'],
            org_id=updated_project['org_id'],
            avatar_color=updated_project.get('avatar_color'),
            avatar_icon=updated_project.get('avatar_icon'),
            avatar_url=avatar_url,
            start_date=updated_project['start_date'],
            end_date=updated_project.get('end_date'),
            view=updated_project.get('view'),
            status=updated_project.get('status'),
            created_at=updated_project['created_at'],
            updated_at=updated_project['updated_at'],
            favourite_project=self._is_favourite_project(project_id, user_id),
        )
        
    def join_project(
        self,
        project_id: UUID4,
        user_id: UUID4,
        org_id: UUID4,
    ) -> None:
        try:
            supabase.table('project_members').insert({
                'project_id': str(project_id),
                'user_id': str(user_id),
                'role': ProjectMemberRole.MEMBER.value,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            error_code = getattr(e, 'code', None)
            if error_code == '23505':  # unique_violation
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User is already a member of this project"
                )
            elif error_code == '23503':  # foreign_key_violation
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found"
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to join project: {e}"
            )

    def _add_project_member(
        self,
        project_id: UUID4,
        user_id: UUID4,
        role: ProjectMemberRole,
    ) -> ProjectMember:
        try:
            response = supabase.table('project_members').insert({
                'project_id': str(project_id),
                'user_id': str(user_id),
                'role': role,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add project member: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to add project member"
            )
        
        return response.data[0]
    
    def add_project_member(
        self,
        project_id: UUID4,
        user_id: UUID4,
        role: ProjectMemberRole,
    ) -> ProjectMember:
        """
        Add a member to a project.
        Checks if user is already a member to avoid duplicates.
        """
        project_id_str = str(project_id)
        user_id_str = str(user_id)
        
        # Check if user is already a member
        try:
            existing = supabase.table('project_members').select('id').eq(
                'project_id', project_id_str
            ).eq('user_id', user_id_str).execute()
            
            if existing.data and len(existing.data) > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="User is already a member of this project"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check existing membership: {e}"
            )
        
        # Verify user belongs to the same organization as the project
        try:
            project_response = supabase.table('projects').select('org_id').eq('id', project_id_str).execute()
            if not project_response.data or len(project_response.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found"
                )
            
            project_org_id = project_response.data[0]['org_id']
            
            # Check if user is a member of the organization
            org_member_response = supabase.table('organization_members').select('id').eq(
                'org_id', str(project_org_id)
            ).eq('user_id', user_id_str).execute()
            
            if not org_member_response.data or len(org_member_response.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User must be a member of the organization to be added to the project"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify organization membership: {e}"
            )
        
        # Add the member
        return self._add_project_member(project_id, user_id, role)
    
    def _apply_pagination(
        self,
        query: Any,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Any:
        if limit and offset:
            query = query.range(offset, offset + limit - 1)
        else:
            if limit:
                offset = offset or settings.DEFAULT_PAGINATION_OFFSET
                query = query.range(offset, offset + limit - 1)
            elif offset:
                limit = limit or settings.DEFAULT_PAGINATION_LIMIT
                query = query.range(0, limit - 1)
                
        return limit, offset, query
    
    def get_favourite_projects(
        self,
        user_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> FavouriteProjectsResponse:
        try:
            query = supabase.table('favourite_projects').select('projects(*)', count='exact').eq('user_id', str(user_id))
            
            # Apply pagination
            limit, offset, query = self._apply_pagination(query, limit, offset)
            
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get favourite projects: {e}"
            )
        
        projects = []
        for project in response.data:
            avatar_url = None
            if project['projects']['avatar_file_id']:
                avatar_url = self.files_service.get_file_url(project['projects']['avatar_file_id'])
            
            project_id = UUID4(project['projects']['id'])
            members = self._get_project_members(project_id)
            
            projects.append(ProjectGetResponse(
                id=project_id,
                name=project['projects']['name'],
                org_id=project['projects']['org_id'],
                avatar_color=project['projects']['avatar_color'],
                avatar_icon=project['projects']['avatar_icon'],
                avatar_url=avatar_url,
                start_date=project['projects']['start_date'],
                end_date=project['projects']['end_date'],
                view=project['projects']['view'],
                progress_percentage=project['projects']['progress_percentage'],
                members=members,
                favourite_project=True,  # These are already favourite projects
            ))
        
        total = response.count if hasattr(response, 'count') and response.count is not None else len(projects)
        
        return FavouriteProjectsResponse(
            projects=projects,
            total=total,
            offset=offset,
            limit=limit,
        )
    
    def _get_non_member_projects_count(
        self,
        org_member_role: OrganizationMemberRole,
        org_id: UUID4,
        user_id: UUID4,
    ) -> int:
        if org_member_role != OrganizationMemberRole.MEMBER.value:
            try:
                total_member_projects_count = (
                    supabase
                        .table('project_members')
                        .select('*', count='exact', head=True)
                        .eq('user_id', user_id)
                        .execute().count
                    )
                total_projects_count = (
                    supabase
                        .table('projects')
                        .select('*', count='exact', head=True)
                        .eq('org_id', org_id)
                        .execute().count
                    )
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to get total projects count: {e}"
                )
            
            # calculate the number of non-member projects
            return total_projects_count - total_member_projects_count
        
        return 0