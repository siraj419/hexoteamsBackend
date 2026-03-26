import base64
import logging

from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

WS_PROTOCOL_AUTH = "hexoteams-auth"
# Short-lived ticket from POST /api/v1/ws/ticket (second comma-separated token in header).
WS_PROTOCOL_TICKET = "hexoteams-ticket"


def _b64url_decode_to_str(segment: str) -> str:
    pad = "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment + pad)
    return raw.decode("utf-8")


def get_ws_ticket_from_websocket_protocol(websocket: WebSocket) -> str | None:
    """
    Parse Sec-WebSocket-Protocol from: hexoteams-ticket, <ticket>

    Returned value is used for single-use consume and as the negotiated subprotocol
    (must match a token the client offered).
    """
    header = websocket.headers.get("sec-websocket-protocol")
    if not header:
        return None
    parts = [p.strip() for p in header.split(",") if p.strip()]
    try:
        idx = parts.index(WS_PROTOCOL_TICKET)
    except ValueError:
        return None
    if idx + 1 >= len(parts):
        return None
    offered = parts[idx + 1]
    return offered if offered else None


def get_access_token_from_websocket_protocol(websocket: WebSocket) -> str | None:
    """Read JWT from Sec-WebSocket-Protocol: hexoteams-auth, <base64url(utf-8 jwt)>."""
    header = websocket.headers.get("sec-websocket-protocol")
    if not header:
        return None
    parts = [p.strip() for p in header.split(",") if p.strip()]
    if WS_PROTOCOL_AUTH not in parts:
        return None
    for part in parts:
        if part == WS_PROTOCOL_AUTH:
            continue
        try:
            return _b64url_decode_to_str(part)
        except Exception as e:
            logger.debug("WS subprotocol token decode failed: %s", e)
            return None
    return None
