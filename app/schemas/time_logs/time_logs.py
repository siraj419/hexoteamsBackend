from pydantic import BaseModel, UUID4, Field, model_validator, field_serializer, field_validator
from datetime import datetime, time, date
from typing import Optional, Union
from enum import Enum


def _parse_time_string(v: str) -> time:
    """Parse time from string. Handles HH:mm, HH:mm:ss, and HH:mm:ss:00 (extra trailing :00)."""
    v = v.strip()
    try:
        return datetime.strptime(v, '%I:%M:%S %p').time()
    except ValueError:
        pass
    try:
        return datetime.strptime(v, '%I:%M %p').time()
    except ValueError:
        pass
    try:
        return datetime.strptime(v, '%H:%M:%S').time()
    except ValueError:
        pass
    try:
        return datetime.strptime(v, '%H:%M').time()
    except ValueError:
        pass
    if len(v) >= 8 and v[8:].strip().startswith(':'):
        return datetime.strptime(v[:8], '%H:%M:%S').time()
    raise ValueError(f"Invalid time format: {v}")


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
    started_at: Union[time, str]
    stoped_at: Optional[Union[time, str]] = None
    date: date
    duration_seconds: float
    duration_formatted: str
    status: TimeLogStatus
    notes: Optional[str] = None
    created_by: Optional[UUID4] = None
    created_at: datetime
    updated_at: datetime
    
    @field_validator('started_at', mode='before')
    @classmethod
    def validate_started_at(cls, v):
        """Parse time from string or return time object"""
        if v is None or isinstance(v, time):
            return v
        if isinstance(v, str):
            v = v.strip()
            try:
                return datetime.strptime(v, '%I:%M:%S %p').time()
            except ValueError:
                try:
                    return datetime.strptime(v, '%I:%M %p').time()
                except ValueError:
                    try:
                        return datetime.strptime(v, '%H:%M:%S.%f').time()
                    except ValueError:
                        try:
                            return datetime.strptime(v, '%H:%M:%S').time()
                        except ValueError:
                            return datetime.strptime(v, '%H:%M').time()
        return v
    
    @field_validator('stoped_at', mode='before')
    @classmethod
    def validate_stoped_at(cls, v):
        """Parse time from string or return time object"""
        if v is None or isinstance(v, time):
            return v
        if isinstance(v, str):
            v = v.strip()
            try:
                return datetime.strptime(v, '%I:%M:%S %p').time()
            except ValueError:
                try:
                    return datetime.strptime(v, '%I:%M %p').time()
                except ValueError:
                    try:
                        return datetime.strptime(v, '%H:%M:%S.%f').time()
                    except ValueError:
                        try:
                            return datetime.strptime(v, '%H:%M:%S').time()
                        except ValueError:
                            return datetime.strptime(v, '%H:%M').time()
        return v
    
    @field_serializer('started_at')
    def serialize_started_at(self, v: time) -> str:
        """Serialize time to 12-hour format with AM/PM"""
        return v.strftime('%I:%M:%S %p')
    
    @field_serializer('stoped_at')
    def serialize_stoped_at(self, v: Optional[time]) -> Optional[str]:
        """Serialize time to 12-hour format with AM/PM"""
        if v is None:
            return None
        return v.strftime('%I:%M:%S %p')


class TimeLogStartResponse(TimeLogResponse):
    pass


class TimeLogStopResponse(TimeLogResponse):
    pass


class TimeLogGetResponse(TimeLogResponse):
    project_name: Optional[str] = None
    task_title: Optional[str] = None


class TimeLogListResponse(BaseModel):
    time_logs: list[TimeLogGetResponse]
    total_count: int
    total_duration_seconds: float
    total_duration_formatted: str


class TimeLogUpdateRequest(BaseModel):
    notes: Optional[str] = None
    started_at: Optional[Union[time, str]] = None
    stoped_at: Optional[Union[time, str]] = None
    duration_seconds: Optional[float] = None
    
    @field_validator('started_at', 'stoped_at', mode='before')
    @classmethod
    def parse_time(cls, v):
        if v is None or isinstance(v, time):
            return v
        if isinstance(v, str):
            return _parse_time_string(v)
        return v


class TimeLogUpdateResponse(TimeLogResponse):
    pass


class TimeLogDeleteResponse(BaseModel):
    success: bool
    message: str


class TimeLogCreateRequest(BaseModel):
    project_id: UUID4
    task_id: UUID4
    date: date
    started_at: Union[time, str]
    stoped_at: Optional[Union[time, str]] = None
    duration_seconds: Optional[float] = Field(None, ge=0, description="Duration in seconds. If not provided, will be calculated from started_at and stoped_at.")
    notes: Optional[str] = None
    
    @field_validator('started_at', 'stoped_at', mode='before')
    @classmethod
    def parse_time(cls, v):
        if v is None or isinstance(v, time):
            return v
        if isinstance(v, str):
            return _parse_time_string(v)
        return v

    @model_validator(mode='after')
    def validate_duration_or_stop_time(self):
        if self.stoped_at is None and self.duration_seconds is None:
            raise ValueError("Either stoped_at or duration_seconds must be provided")
        return self


class TimeLogCreateResponse(TimeLogResponse):
    pass