from fastapi import APIRouter, Depends, Query, status
from pydantic import UUID4
from datetime import date
from typing import Optional

from app.schemas.time_logs.time_logs import (
    TimeLogStartRequest,
    TimeLogStopRequest,
    TimeLogCreateRequest,
    TimeLogStartResponse,
    TimeLogStopResponse,
    TimeLogCreateResponse,
    TimeLogGetResponse,
    TimeLogListResponse,
    TimeLogUpdateRequest,
    TimeLogUpdateResponse,
    TimeLogStatus,
)
from app.services.time_log import TimeLogService
from app.routers.deps import get_active_organization

router = APIRouter()
time_log_service = TimeLogService()


@router.post(
    "",
    response_model=TimeLogCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a time log manually",
    description="Manually create a time log entry with specific start time, stop time, and duration. Either stoped_at or duration_seconds must be provided.",
)
def create_time_log(
    time_log_request: TimeLogCreateRequest,
    organization: dict = Depends(get_active_organization),
):
    return time_log_service.create_time_log(time_log_request, UUID4(organization['member_user_id']))


@router.post(
    "/start",
    response_model=TimeLogStartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new time log",
    description="Start tracking time for a specific task and project. Only one time log can be active at a time per user.",
)
def start_time_log(
    time_log_request: TimeLogStartRequest,
    organization: dict = Depends(get_active_organization),
):
    return time_log_service.start_time_log(time_log_request, UUID4(organization['member_user_id']))


@router.post(
    "/{time_log_id}/stop",
    response_model=TimeLogStopResponse,
    summary="Stop a running time log",
    description="Stop the currently running time log and calculate the duration.",
)
def stop_time_log(
    time_log_id: UUID4,
    stop_request: TimeLogStopRequest,
    organization: dict = Depends(get_active_organization),
):
    return time_log_service.stop_time_log(time_log_id, stop_request, UUID4(organization['member_user_id']))


@router.get(
    "/active",
    response_model=Optional[TimeLogGetResponse],
    summary="Get active time log",
    description="Get the currently running time log for the authenticated user.",
)
def get_active_time_log(
    organization: dict = Depends(get_active_organization),
):
    return time_log_service.get_active_time_log(UUID4(organization['member_user_id']))


@router.get(
    "",
    response_model=TimeLogListResponse,
    summary="Get time logs",
    description="Get a list of time logs with optional filters. Returns paginated results with total count and duration.",
)
def get_time_logs(
    project_id: Optional[UUID4] = Query(None, description="Filter by project ID"),
    task_id: Optional[UUID4] = Query(None, description="Filter by task ID"),
    from_date: Optional[date] = Query(None, description="Filter by start date (inclusive)"),
    to_date: Optional[date] = Query(None, description="Filter by end date (inclusive)"),
    status_filter: Optional[TimeLogStatus] = Query(None, description="Filter by status (running or stopped)"),
    limit: Optional[int] = Query(100, ge=1, le=1000, description="Maximum number of results to return"),
    offset: Optional[int] = Query(0, ge=0, description="Number of results to skip"),
    organization: dict = Depends(get_active_organization),
):
    return time_log_service.get_time_logs(
        user_id=UUID4(organization['member_user_id']),
        project_id=project_id,
        task_id=task_id,
        from_date=from_date,
        to_date=to_date,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{time_log_id}",
    response_model=TimeLogGetResponse,
    summary="Get a specific time log",
    description="Get details of a specific time log by ID.",
)
def get_time_log(
    time_log_id: UUID4,
    organization: dict = Depends(get_active_organization),
):
    return time_log_service.get_time_log(time_log_id, UUID4(organization['member_user_id']))


@router.put(
    "/{time_log_id}",
    response_model=TimeLogUpdateResponse,
    summary="Update a time log",
    description="Update a stopped time log. Running time logs cannot be updated.",
)
def update_time_log(
    time_log_id: UUID4,
    time_log_request: TimeLogUpdateRequest,
    organization: dict = Depends(get_active_organization),
):
    return time_log_service.update_time_log(time_log_id, time_log_request, UUID4(organization['member_user_id']))


@router.delete(
    "/{time_log_id}",
    summary="Delete a time log",
    description="Delete a time log by ID.",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_time_log(
    time_log_id: UUID4,
    organization: dict = Depends(get_active_organization),
):
    time_log_service.delete_time_log(time_log_id, UUID4(organization['member_user_id']))

