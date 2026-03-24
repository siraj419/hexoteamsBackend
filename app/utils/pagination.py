from typing import Any, Optional, Tuple

from sqlalchemy.sql import Select

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


def apply_sa_limit_offset(
    stmt: Select[Any],
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Tuple[int, int, Select[Any]]:
    lim = settings.DEFAULT_PAGINATION_LIMIT if limit is None else limit
    off = settings.DEFAULT_PAGINATION_OFFSET if offset is None else offset
    return lim, off, stmt.limit(lim).offset(off)
