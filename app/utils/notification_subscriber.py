import json
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from app.utils.redis_cache import redis_client
from app.utils.websocket_manager import manager
from app.utils.notification_pubsub import NOTIFICATION_CHANNEL

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notification_subscriber")


async def start_notification_subscriber():
    """
    Start Redis subscriber for notification events.
    This runs as a background task and listens for notification events
    published by Celery tasks, then forwards them to WebSocket connections.
    """
    if not redis_client:
        logger.warning("Redis client not available, notification subscriber will not start")
        return
    
    try:
        pubsub = redis_client.pubsub()
        pubsub.subscribe(NOTIFICATION_CHANNEL)
        logger.info(f"Notification subscriber started, listening on channel: {NOTIFICATION_CHANNEL}")
        
        loop = asyncio.get_event_loop()
        
        while True:
            try:
                message = await loop.run_in_executor(
                    _executor,
                    lambda: pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                )
                
                if message is None:
                    continue
                
                if message['type'] == 'message':
                    await _handle_notification_event(message['data'])
                    
            except asyncio.CancelledError:
                logger.info("Notification subscriber cancelled")
                break
            except Exception as e:
                logger.error(f"Error processing notification event: {e}", exc_info=True)
                await asyncio.sleep(1)
                
    except Exception as e:
        logger.error(f"Notification subscriber error: {e}", exc_info=True)
    finally:
        if 'pubsub' in locals():
            pubsub.close()
        logger.info("Notification subscriber stopped")


async def _handle_notification_event(data: str):
    """
    Handle a notification event received from Redis Pub/Sub.
    
    Args:
        data: JSON string containing event data
    """
    try:
        event = json.loads(data)
        user_id = event.get('user_id')
        org_id = event.get('org_id')
        notification_type = event.get('type')
        payload = event.get('payload', {})
        
        if not user_id or not org_id or not notification_type:
            logger.warning(f"Invalid notification event: missing required fields")
            return
        
        logger.debug(f"Received notification event: user={user_id}, org={org_id}, type={notification_type}")
        
        message = {
            "type": notification_type,
            **payload
        }
        
        await manager.broadcast_inbox_notification(
            org_id=org_id,
            user_id=user_id,
            message=message
        )
        
        logger.debug(f"Notification delivered via WebSocket to user {user_id}")
        
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse notification event JSON: {e}")
    except Exception as e:
        logger.error(f"Failed to handle notification event: {e}", exc_info=True)

