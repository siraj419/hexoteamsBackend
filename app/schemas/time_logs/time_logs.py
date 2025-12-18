from pydantic import BaseModel, UUID4, Field, model_validator
from datetime import datetime, time, date
from typing import Optional
from enum import Enum


class TimeLogStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


class TimeLogStartRequest(BaseModel):
    project_id: UUID4
    task_id: UUID4
    notes: Optional[str] = None


class TimeLogStopRequest(BaseModel):
    notes: Optional[str] = None


class TimeLogResponse(BaseModel):
    id: UUID4
    project_id: UUID4
    task_id: UUID4
    started_at: time
    stoped_at: Optional[time] = None
    date: date
    duration_seconds: float
    duration_formatted: str
    status: TimeLogStatus
    notes: Optional[str] = None
    created_by: Optional[UUID4] = None
    created_at: datetime
    updated_at: datetime


class TimeLogStartResponse(TimeLogResponse):
    pass


class TimeLogStopResponse(TimeLogResponse):
    pass


class TimeLogGetResponse(TimeLogResponse):
    pass


class TimeLogListResponse(BaseModel):
    time_logs: list[TimeLogGetResponse]
    total_count: int
    total_duration_seconds: float
    total_duration_formatted: str


class TimeLogUpdateRequest(BaseModel):
    notes: Optional[str] = None
    started_at: Optional[time] = None
    stoped_at: Optional[time] = None
    duration_seconds: Optional[float] = None


class TimeLogUpdateResponse(TimeLogResponse):
    pass


class TimeLogDeleteResponse(BaseModel):
    success: bool
    message: str


class TimeLogCreateRequest(BaseModel):
    project_id: UUID4
    task_id: UUID4
    date: date
    started_at: time
    stoped_at: Optional[time] = None
    duration_seconds: Optional[float] = Field(None, ge=0, description="Duration in seconds. If not provided, will be calculated from started_at and stoped_at.")
    notes: Optional[str] = None

    @model_validator(mode='after')
    def validate_duration_or_stop_time(self):
        if self.stoped_at is None and self.duration_seconds is None:
            raise ValueError("Either stoped_at or duration_seconds must be provided")
        return self


class TimeLogCreateResponse(TimeLogResponse):
    pass