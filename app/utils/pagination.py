from typing import Any, Optional
from app.core import settings

def apply_pagination(
        query: Any,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Any:
    
    print(f"limit: {limit}, offset: {offset}")
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