from fastapi import HTTPException, status
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

def calculate_time_ago(created_at: datetime | str, user_timezone: str) -> str:
    try:
        if isinstance(created_at, str):
            created_at_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            if created_at_dt.tzinfo is None:
                created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
        else:
            created_at_dt = created_at
            if created_at_dt.tzinfo is None:
                created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        time_diff = now - created_at_dt
        
        if time_diff < timedelta(0):
            time_diff = timedelta(0)
        
        total_seconds = int(time_diff.total_seconds())
        
        if total_seconds < 60:
            return f"{total_seconds} seconds ago"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        elif time_diff.days < 30:
            days = time_diff.days
            return f"{days} day{'s' if days != 1 else ''} ago"
        elif time_diff.days < 365:
            months = time_diff.days // 30
            return f"{months} month{'s' if months != 1 else ''} ago"
        else:
            zone_obj = ZoneInfo(user_timezone)
            created_at_local = created_at_dt.astimezone(zone_obj)
            return created_at_local.strftime("%d %b %Y %H:%M")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate time ago: {e}"
        )

def calculate_file_size(size_bytes: int) -> str:
    if size_bytes < 0:
        return "0 B"
    
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        kb = size_bytes / 1024
        return f"{kb:.2f} KB" if kb < 100 else f"{int(kb)} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        mb = size_bytes / (1024 * 1024)
        return f"{mb:.2f} MB" if mb < 100 else f"{int(mb)} MB"
    else:
        gb = size_bytes / (1024 * 1024 * 1024)
        return f"{gb:.2f} GB" if gb < 100 else f"{int(gb)} GB"