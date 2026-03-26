"""Single-use WebSocket auth tickets (short-lived).

Uses Redis when the app Redis client is available (multi-worker safe); otherwise in-memory.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import string
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_TICKET_TTL_SEC = 30
_TICKET_LENGTH = 12
_ALPHABET = string.ascii_letters + string.digits
_REDIS_KEY_PREFIX = "wsticket:"

_REDIS_CONSUME_LUA = """
local v = redis.call('GET', KEYS[1])
if v == false then
  return nil
end
redis.call('DEL', KEYS[1])
return v
"""

try:
    from app.utils.redis_cache import redis_client as _redis_client
except Exception:  # pragma: no cover
    _redis_client = None


@dataclass
class _TicketEntry:
    user_id: str
    expires_at: float


_lock = asyncio.Lock()
_store: dict[str, _TicketEntry] = {}


def _redis_ok() -> bool:
    return _redis_client is not None


def _new_ticket_value() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_TICKET_LENGTH))


def _purge_expired_unlocked() -> None:
    now = time.time()
    dead = [k for k, v in _store.items() if v.expires_at < now]
    for k in dead:
        del _store[k]


def _memory_issue_sync(uid: str) -> str:
    _purge_expired_unlocked()
    ticket = _new_ticket_value()
    while ticket in _store:
        ticket = _new_ticket_value()
    _store[ticket] = _TicketEntry(user_id=uid, expires_at=time.time() + _TICKET_TTL_SEC)
    return ticket


def _memory_consume_sync(key: str) -> str | None:
    entry = _store.pop(key, None)
    if entry is None:
        return None
    if time.time() > entry.expires_at:
        return None
    return entry.user_id


def _redis_issue_sync(uid: str) -> str:
    for _ in range(64):
        ticket = _new_ticket_value()
        rkey = f"{_REDIS_KEY_PREFIX}{ticket}"
        if _redis_client.set(rkey, uid, ex=_TICKET_TTL_SEC, nx=True):
            return ticket
    raise RuntimeError("Failed to allocate unique WebSocket ticket")


def _redis_consume_sync(ticket_key: str) -> str | None:
    rkey = f"{_REDIS_KEY_PREFIX}{ticket_key}"
    raw = _redis_client.eval(_REDIS_CONSUME_LUA, 1, rkey)
    if raw is None:
        return None
    return str(raw)


async def issue_ws_ticket(user_id: str) -> str:
    uid = str(user_id)
    if _redis_ok():
        try:
            return await asyncio.to_thread(_redis_issue_sync, uid)
        except Exception as e:
            logger.error("Redis ticket issue failed, falling back to memory: %s", e)
    async with _lock:
        return _memory_issue_sync(uid)


async def consume_ws_ticket(ticket: str) -> str | None:
    if not ticket or not ticket.strip():
        return None
    key = ticket.strip()
    if _redis_ok():
        try:
            return await asyncio.to_thread(_redis_consume_sync, key)
        except Exception as e:
            logger.error("Redis ticket consume failed, falling back to memory: %s", e)
            # Ticket was likely created in Redis; memory fallback cannot redeem it
            return None
    async with _lock:
        return _memory_consume_sync(key)
