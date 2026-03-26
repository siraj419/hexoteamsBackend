"""Single-use WebSocket auth tickets (short-lived, in-memory)."""

from __future__ import annotations

import asyncio
import secrets
import string
import time
from dataclasses import dataclass

_TICKET_TTL_SEC = 30.0
_TICKET_LENGTH = 12
_ALPHABET = string.ascii_letters + string.digits


@dataclass
class _TicketEntry:
    user_id: str
    expires_at: float


_lock = asyncio.Lock()
_store: dict[str, _TicketEntry] = {}


def _purge_expired_unlocked() -> None:
    now = time.time()
    dead = [k for k, v in _store.items() if v.expires_at < now]
    for k in dead:
        del _store[k]


def _new_ticket_value() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_TICKET_LENGTH))


async def issue_ws_ticket(user_id: str) -> str:
    uid = str(user_id)
    async with _lock:
        _purge_expired_unlocked()
        ticket = _new_ticket_value()
        while ticket in _store:
            ticket = _new_ticket_value()
        _store[ticket] = _TicketEntry(user_id=uid, expires_at=time.time() + _TICKET_TTL_SEC)
        return ticket


async def consume_ws_ticket(ticket: str) -> str | None:
    if not ticket or not ticket.strip():
        return None
    key = ticket.strip()
    async with _lock:
        entry = _store.pop(key, None)
        if entry is None:
            return None
        if time.time() > entry.expires_at:
            return None
        return entry.user_id
