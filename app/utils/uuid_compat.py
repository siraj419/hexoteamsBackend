from __future__ import annotations

from uuid import UUID


def as_uuid(value: UUID | str) -> UUID:
    """
    Coerce to uuid.UUID. Pydantic's UUID4(x) delegates to uuid.UUID(x), which
    expects a string; passing an existing UUID raises AttributeError on Python 3.13+.
    """
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
