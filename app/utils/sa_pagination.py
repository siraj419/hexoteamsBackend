from typing import Any, Optional, Tuple

from app.core import settings


def apply_sa_limit_offset(
    stmt: Any,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Tuple[Optional[int], Optional[int], Any]:
    """Apply LIMIT/OFFSET to a SQLAlchemy selectable (2.0 style)."""
    off = offset if offset is not None else settings.DEFAULT_PAGINATION_OFFSET
    lim = limit if limit is not None else settings.DEFAULT_PAGINATION_LIMIT
    if off:
        stmt = stmt.offset(off)
    if lim is not None:
        stmt = stmt.limit(lim)
    return lim, off, stmt
