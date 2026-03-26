"""WebSocket routes.

Auth (choose one):
- Sec-WebSocket-Protocol: `hexoteams-ticket, <ticket>` (ticket from POST /api/v1/ws/ticket; preferred).
- Or query `?ticket=<ticket>` (legacy / fallback).
- Or `hexoteams-auth` with base64url(UTF-8 JWT) (legacy).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.core.security import TOKEN_TYPE_ACCESS, AuthenticatedUser, decode_token
from app.db.sync_session import SyncSessionLocal
from app.models import ChatConversation, OrganizationMember, ProjectMember
from app.routers.deps import get_current_user
from app.services.chat import ChatService
from app.utils.uuid_compat import as_uuid
from app.utils.websocket_manager import manager
from app.utils.ws_auth import (
    WS_PROTOCOL_AUTH,
    get_access_token_from_websocket_protocol,
    get_ws_ticket_from_websocket_protocol,
)
from app.utils.ws_ticket_store import consume_ws_ticket, issue_ws_ticket
from app.schemas.chat import (
    ProjectMessageCreate,
    DirectMessageCreate,
)

router = APIRouter()
logger = logging.getLogger(__name__)

WS_POLICY_VIOLATION = 1008


class WebSocketTicketResponse(BaseModel):
    ticket: str


async def verify_ws_token(token: str) -> dict | None:
    """Verify JWT access token and return user data."""
    try:
        payload = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
        return {"id": payload["sub"], "email": payload.get("email", "")}
    except ValueError as e:
        logger.error("Token verification failed: %s", e)
        return None


async def verify_project_access(user_id: str, project_id: str) -> bool:
    """Check if user is a project member"""
    try:
        from uuid import UUID

        db = SyncSessionLocal()
        try:
            uid = UUID(user_id) if isinstance(user_id, str) else user_id
            pid = UUID(project_id) if isinstance(project_id, str) else project_id
            row = db.execute(
                select(ProjectMember.id).where(
                    ProjectMember.project_id == pid,
                    ProjectMember.user_id == uid,
                )
            ).first()
            return row is not None
        finally:
            db.close()
    except Exception:
        return False


async def verify_conversation_access(user_id: str, conversation_id: str) -> bool:
    """Check if user is a conversation participant"""
    try:
        from uuid import UUID

        db = SyncSessionLocal()
        try:
            uid = UUID(user_id) if isinstance(user_id, str) else user_id
            cid = UUID(conversation_id) if isinstance(conversation_id, str) else conversation_id
            row = db.execute(
                select(ChatConversation.id).where(
                    ChatConversation.id == cid,
                    or_(
                        ChatConversation.user1_id == uid,
                        ChatConversation.user2_id == uid,
                    ),
                )
            ).first()
            return row is not None
        finally:
            db.close()
    except Exception:
        return False


@router.post("/ticket", response_model=WebSocketTicketResponse)
async def create_websocket_ticket(user: AuthenticatedUser = Depends(get_current_user)):
    ticket = await issue_ws_ticket(user.id)
    return WebSocketTicketResponse(ticket=ticket)


async def _consume_ws_ticket_from_request(
    websocket: WebSocket,
) -> tuple[dict[str, Any] | None, Literal["invalid_ticket"] | None, str | None, bool]:
    """
    Validate short-lived ticket from handshake.

    Returns (user, invalid_ticket, negotiate_subprotocol, used_ticket).
    negotiate_subprotocol: ticket string for accept() when using hexoteams-ticket, <ticket>;
    None when using ?ticket= or when not ticket auth.
    used_ticket: True if user was authenticated via ticket (query or protocol).
    """
    proto_ticket = get_ws_ticket_from_websocket_protocol(websocket)
    if proto_ticket is not None:
        user_id = await consume_ws_ticket(proto_ticket)
        if not user_id:
            return None, "invalid_ticket", None, True
        return {"id": user_id, "email": ""}, None, proto_ticket, True

    raw_ticket = websocket.query_params.get("ticket")
    if raw_ticket is not None and raw_ticket.strip():
        user_id = await consume_ws_ticket(raw_ticket.strip())
        if not user_id:
            return None, "invalid_ticket", None, True
        return {"id": user_id, "email": ""}, None, None, True

    return None, None, None, False


@router.websocket("/project/{project_id}")
async def project_chat_websocket(
    websocket: WebSocket,
    project_id: str,
):
    """
    WebSocket endpoint for project chat
    
    Events received from client:
    - {"type": "message", "body": "...", "reply_to_id": "..."}
    - {"type": "typing", "is_typing": true/false}
    - {"type": "read", "message_id": "..."}
    
    Events sent to client:
    - {"type": "message", "data": {...}}
    - {"type": "typing", "user_id": "...", "is_typing": true/false}
    - {"type": "read", "user_id": "...", "message_id": "..."}
    - {"type": "error", "message": "..."}
    """
    user, ticket_err, ticket_subproto, used_ticket = await _consume_ws_ticket_from_request(websocket)
    if ticket_err:
        await websocket.close(code=WS_POLICY_VIOLATION, reason="Invalid or expired ticket")
        return

    if not user:
        token = get_access_token_from_websocket_protocol(websocket)
        if not token:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "error", "message": "Token required"}))
            await websocket.close(code=4001)
            return

        user = await verify_ws_token(token)
        if not user:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "error", "message": "Invalid token"}))
            await websocket.close(code=4001)
            return

    user_id = user["id"]

    if not await verify_project_access(user_id, project_id):
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "error", "message": "Access denied"}))
        await websocket.close(code=4003)
        return

    negotiate = ticket_subproto if used_ticket else WS_PROTOCOL_AUTH
    await manager.connect_project(websocket, project_id, user_id, subprotocol=negotiate)
    
    try:
        while True:
            data = await websocket.receive_text()
            logger.debug(f"[WS] Received raw data from project {project_id}, user {user_id}: {data}")
            try:
                message = json.loads(data)
                logger.debug(f"[WS] Parsed message: {message}")
                await handle_project_event(project_id, user_id, message, websocket)
            except json.JSONDecodeError as e:
                logger.error(f"[WS] JSON decode error for project {project_id}: {e}, data: {data}")
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON"
                }))
            except Exception as e:
                logger.error(f"[WS] Error handling project event for project {project_id}, user {user_id}: {e}", exc_info=True)
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": str(e)
                }))
    except WebSocketDisconnect:
        manager.disconnect_project(websocket, project_id, user_id)


@router.websocket("/dm/{conversation_id}")
async def dm_chat_websocket(
    websocket: WebSocket,
    conversation_id: str,
):
    """
    WebSocket endpoint for direct message chat
    
    Events received from client:
    - {"type": "message", "body": "..."}
    - {"type": "typing", "is_typing": true/false}
    - {"type": "read", "message_id": "..."}
    
    Events sent to client:
    - {"type": "message", "data": {...}}
    - {"type": "typing", "user_id": "...", "is_typing": true/false}
    - {"type": "read", "user_id": "...", "message_id": "..."}
    - {"type": "error", "message": "..."}
    """
    user, ticket_err, ticket_subproto, used_ticket = await _consume_ws_ticket_from_request(websocket)
    if ticket_err:
        await websocket.close(code=WS_POLICY_VIOLATION, reason="Invalid or expired ticket")
        return

    if not user:
        token = get_access_token_from_websocket_protocol(websocket)
        if not token:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "error", "message": "Token required"}))
            await websocket.close(code=4001)
            return

        user = await verify_ws_token(token)
        if not user:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "error", "message": "Invalid token"}))
            await websocket.close(code=4001)
            return

    user_id = user["id"]

    if not await verify_conversation_access(user_id, conversation_id):
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "error", "message": "Access denied"}))
        await websocket.close(code=4003)
        return

    negotiate = ticket_subproto if used_ticket else WS_PROTOCOL_AUTH
    await manager.connect_dm(websocket, conversation_id, user_id, subprotocol=negotiate)
    
    try:
        while True:
            data = await websocket.receive_text()
            logger.debug(f"[WS] Received raw data from DM conversation {conversation_id}, user {user_id}: {data}")
            try:
                message = json.loads(data)
                logger.debug(f"[WS] Parsed message: {message}")
                await handle_dm_event(conversation_id, user_id, message, websocket)
            except json.JSONDecodeError as e:
                logger.error(f"[WS] JSON decode error for DM conversation {conversation_id}: {e}, data: {data}")
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON"
                }))
            except Exception as e:
                logger.error(f"[WS] Error handling DM event for conversation {conversation_id}, user {user_id}: {e}", exc_info=True)
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": str(e)
                }))
    except WebSocketDisconnect:
        manager.disconnect_dm(websocket, conversation_id, user_id)


async def handle_project_event(project_id: str, user_id: str, event: dict, websocket: WebSocket):
    """Handle incoming WebSocket event for project chat"""
    event_type = event.get("type")
    logger.debug(f"[WS] handle_project_event - event_type: {event_type}, project_id: {project_id}, user_id: {user_id}, event: {event}")
    
    if event_type == "message":
        chat_service = ChatService()
        message_data = ProjectMessageCreate(
            body=event.get("body", ""),
            reply_to_id=event.get("reply_to_id"),
            attachments=event.get("attachments")
        )
        
        response = chat_service.send_project_message(
            as_uuid(project_id),
            as_uuid(user_id),
            message_data
        )
        
        await manager.broadcast_to_project(
            project_id, 
            {
                "type": "message",
                "data": response.model_dump(mode='json')
            },
            sender_id=user_id
        )
    
    elif event_type == "typing":
        await manager.broadcast_to_project(project_id, {
            "type": "typing",
            "user_id": user_id,
            "is_typing": event.get("is_typing", False)
        }, exclude_user=user_id)
    
    elif event_type == "read":
        logger.info(f"[WS Read Receipt] Project chat - Received read event: {event}")
        # Accept both message_id and last_read_message_id for backward compatibility
        message_id = event.get("message_id") or event.get("last_read_message_id")
        logger.info(f"[WS Read Receipt] Project chat - Extracted message_id: {message_id} from event: {event}")
        
        if not message_id:
            logger.warning(f"[WS Read Receipt] Project chat - No message_id found in read event: {event}")
            await websocket.send_text(json.dumps({
                "type": "read_error",
                "message": "No message_id provided"
            }))
            return
        
        try:
            logger.info(f"[WS Read Receipt] Project chat - Processing read receipt - user_id: {user_id}, project_id: {project_id}, last_read_message_id: {message_id}")
            chat_service = ChatService()
            marked_message_ids = chat_service.mark_project_messages_read(
                as_uuid(project_id),
                as_uuid(user_id),
                as_uuid(message_id)
            )
            logger.info(f"[WS Read Receipt] Project chat - Marked {len(marked_message_ids)} messages as read for user {user_id} in project {project_id}")
            
            # Send confirmation to sender
            await websocket.send_text(json.dumps({
                "type": "read_confirmed",
                "message_id": message_id,
                "marked_count": len(marked_message_ids)
            }))
            logger.debug(f"[WS Read Receipt] Project chat - Sent confirmation to user {user_id}")
            
            # Broadcast to other users
            await manager.broadcast_to_project(project_id, {
                "type": "read",
                "user_id": user_id,
                "message_ids": marked_message_ids,
                "last_read_message_id": message_id
            }, exclude_user=user_id)
            logger.debug(f"[WS Read Receipt] Project chat - Broadcasted read receipt to project {project_id}, excluded user {user_id}")
        except Exception as e:
            logger.error(f"[WS Read Receipt] Project chat - Error processing read receipt: {e}", exc_info=True)
            await websocket.send_text(json.dumps({
                "type": "read_error",
                "message": str(e)
            }))
            raise
    else:
        logger.warning(f"[WS] Project chat - Unknown event type: {event_type}, event: {event}")


async def handle_dm_event(conversation_id: str, user_id: str, event: dict, websocket: WebSocket):
    """Handle incoming WebSocket event for DM chat"""
    event_type = event.get("type")
    logger.debug(f"[WS] handle_dm_event - event_type: {event_type}, conversation_id: {conversation_id}, user_id: {user_id}, event: {event}")
    
    if event_type == "message":
        chat_service = ChatService()
        message_data = DirectMessageCreate(
            body=event.get("body", ""),
            attachments=event.get("attachments")
        )
        
        # Get organization_id from conversation
        db = SyncSessionLocal()
        try:
            from uuid import UUID

            cid = UUID(conversation_id) if isinstance(conversation_id, str) else conversation_id
            conv = db.execute(
                select(ChatConversation.organization_id).where(ChatConversation.id == cid)
            ).first()
        finally:
            db.close()
        if not conv:
            return
        organization_id = conv[0]
        
        response = chat_service.send_direct_message(
            as_uuid(conversation_id),
            as_uuid(user_id),
            message_data,
            as_uuid(organization_id)
        )
        
        await manager.broadcast_to_dm(
            conversation_id, 
            {
                "type": "message",
                "data": response.model_dump(mode='json')
            },
            sender_id=user_id
        )
    
    elif event_type == "typing":
        await manager.broadcast_to_dm(conversation_id, {
            "type": "typing",
            "user_id": user_id,
            "is_typing": event.get("is_typing", False)
        }, exclude_user=user_id)
    
    elif event_type == "read":
        logger.info(f"[WS Read Receipt] DM chat - Received read event: {event}")
        # Accept both message_id and last_read_message_id for backward compatibility
        message_id = event.get("message_id") or event.get("last_read_message_id")
        logger.info(f"[WS Read Receipt] DM chat - Extracted message_id: {message_id} from event: {event}")
        
        if not message_id:
            logger.warning(f"[WS Read Receipt] DM chat - No message_id found in read event: {event}")
            await websocket.send_text(json.dumps({
                "type": "read_error",
                "message": "No message_id provided"
            }))
            return
        
        try:
            logger.info(f"[WS Read Receipt] DM chat - Processing read receipt - user_id: {user_id}, conversation_id: {conversation_id}, last_read_message_id: {message_id}")
            # Get organization_id from conversation
            db = SyncSessionLocal()
            try:
                from uuid import UUID

                cid = UUID(conversation_id) if isinstance(conversation_id, str) else conversation_id
                conv_row = db.execute(
                    select(ChatConversation.organization_id).where(ChatConversation.id == cid)
                ).first()
            finally:
                db.close()
            if not conv_row:
                logger.warning(f"[WS Read Receipt] DM chat - Conversation {conversation_id} not found")
                await websocket.send_text(json.dumps({
                    "type": "read_error",
                    "message": "Conversation not found"
                }))
                return
            organization_id = conv_row[0]
            
            chat_service = ChatService()
            marked_message_ids = chat_service.mark_dm_read(
                as_uuid(conversation_id),
                as_uuid(user_id),
                as_uuid(message_id),
                as_uuid(organization_id)
            )
            logger.info(f"[WS Read Receipt] DM chat - Marked {len(marked_message_ids)} messages as read for user {user_id} in conversation {conversation_id}")
            
            # Send confirmation to sender
            await websocket.send_text(json.dumps({
                "type": "read_confirmed",
                "message_id": message_id,
                "marked_count": len(marked_message_ids)
            }))
            logger.debug(f"[WS Read Receipt] DM chat - Sent confirmation to user {user_id}")
            
            # Broadcast to other participant
            await manager.broadcast_to_dm(conversation_id, {
                "type": "read",
                "user_id": user_id,
                "message_ids": marked_message_ids,
                "last_read_message_id": message_id
            }, exclude_user=user_id)
            logger.debug(f"[WS Read Receipt] DM chat - Broadcasted read receipt to conversation {conversation_id}, excluded user {user_id}")
        except Exception as e:
            logger.error(f"[WS Read Receipt] DM chat - Error processing read receipt: {e}", exc_info=True)
            await websocket.send_text(json.dumps({
                "type": "read_error",
                "message": str(e)
            }))
            raise
    else:
        logger.warning(f"[WS] DM chat - Unknown event type: {event_type}, event: {event}")


@router.websocket("/inbox/{org_id}")
async def inbox_websocket(
    websocket: WebSocket,
    org_id: str,
):
    """
    WebSocket endpoint for real-time inbox notifications
    
    Events sent to client:
    - {"type": "inbox_new", "data": {...}}
    - {"type": "inbox_read", "inbox_id": "..."}
    - {"type": "inbox_archived", "inbox_id": "..."}
    - {"type": "inbox_deleted", "inbox_id": "..."}
    - {"type": "unread_count", "count": 5}
    - {"type": "error", "message": "..."}
    """
    user, ticket_err, ticket_subproto, used_ticket = await _consume_ws_ticket_from_request(websocket)
    if ticket_err:
        await websocket.close(code=WS_POLICY_VIOLATION, reason="Invalid or expired ticket")
        return

    if not user:
        token = get_access_token_from_websocket_protocol(websocket)
        if not token:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "error", "message": "Token required"}))
            await websocket.close(code=4001)
            return

        user = await verify_ws_token(token)
        if not user:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "error", "message": "Invalid token"}))
            await websocket.close(code=4001)
            return

    user_id = user["id"]

    try:
        from uuid import UUID

        db = SyncSessionLocal()
        try:
            oid = UUID(org_id) if isinstance(org_id, str) else org_id
            uid = UUID(user_id) if isinstance(user_id, str) else user_id
            row = db.execute(
                select(OrganizationMember.id).where(
                    OrganizationMember.org_id == oid,
                    OrganizationMember.user_id == uid,
                )
            ).first()
        finally:
            db.close()

        if not row:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "error", "message": "Access denied"}))
            await websocket.close(code=4003)
            return
    except Exception as e:
        logger.error(f"Organization verification failed: {e}")
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "error", "message": "Verification failed"}))
        await websocket.close(code=4003)
        return

    negotiate = ticket_subproto if used_ticket else WS_PROTOCOL_AUTH
    await manager.connect_inbox(websocket, org_id, user_id, subprotocol=negotiate)
    
    try:
        while True:
            data = await websocket.receive_text()
            logger.debug(f"Received inbox WS message: {data}")
    except WebSocketDisconnect:
        manager.disconnect_inbox(websocket, org_id, user_id)
