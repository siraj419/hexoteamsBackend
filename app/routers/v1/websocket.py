from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, status
from pydantic import UUID4
import json
import logging
from datetime import datetime, timezone

from app.core import supabase
from app.utils.websocket_manager import manager
from app.services.chat import ChatService
from app.schemas.chat import (
    ProjectMessageCreate,
    DirectMessageCreate,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def verify_ws_token(token: str) -> dict:
    """Verify JWT token and return user data"""
    try:
        response = supabase.auth.get_user(token)
        if not response or not response.user:
            return None
        return {"id": response.user.id, "email": response.user.email}
    except Exception as e:
        logger.error(f"Token verification failed: {e}")
        return None


async def verify_project_access(user_id: str, project_id: str) -> bool:
    """Check if user is a project member"""
    try:
        response = supabase.table('project_members').select('id').eq(
            'project_id', project_id
        ).eq('user_id', user_id).execute()
        return bool(response.data)
    except Exception:
        return False


async def verify_conversation_access(user_id: str, conversation_id: str) -> bool:
    """Check if user is a conversation participant"""
    try:
        response = supabase.table('chat_conversations').select('id').eq(
            'id', conversation_id
        ).or_(f"user1_id.eq.{user_id},user2_id.eq.{user_id}").execute()
        return bool(response.data)
    except Exception:
        return False


@router.websocket("/project/{project_id}")
async def project_chat_websocket(
    websocket: WebSocket,
    project_id: str,
    token: str = Query(None)
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
    
    await manager.connect_project(websocket, project_id, user_id)
    
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
    token: str = Query(None)
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
    
    await manager.connect_dm(websocket, conversation_id, user_id)
    
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
            UUID4(project_id),
            UUID4(user_id),
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
                UUID4(project_id),
                UUID4(user_id),
                UUID4(message_id)
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
        conv_response = supabase.table('chat_conversations').select('organization_id').eq('id', conversation_id).execute()
        if not conv_response.data:
            return
        organization_id = conv_response.data[0]['organization_id']
        
        response = chat_service.send_direct_message(
            UUID4(conversation_id),
            UUID4(user_id),
            message_data,
            UUID4(organization_id)
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
            conv_response = supabase.table('chat_conversations').select('organization_id').eq('id', conversation_id).execute()
            if not conv_response.data:
                logger.warning(f"[WS Read Receipt] DM chat - Conversation {conversation_id} not found")
                await websocket.send_text(json.dumps({
                    "type": "read_error",
                    "message": "Conversation not found"
                }))
                return
            organization_id = conv_response.data[0]['organization_id']
            
            chat_service = ChatService()
            marked_message_ids = chat_service.mark_dm_read(
                UUID4(conversation_id),
                UUID4(user_id),
                UUID4(message_id),
                UUID4(organization_id)
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
    token: str = Query(None)
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
        response = supabase.table('organization_members').select('id').eq(
            'org_id', org_id
        ).eq('user_id', user_id).execute()
        
        if not response.data:
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
    
    await manager.connect_inbox(websocket, org_id, user_id)
    
    try:
        while True:
            data = await websocket.receive_text()
            logger.debug(f"Received inbox WS message: {data}")
    except WebSocketDisconnect:
        manager.disconnect_inbox(websocket, org_id, user_id)
