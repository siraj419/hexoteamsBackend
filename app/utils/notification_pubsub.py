import json
import logging
from typing import Dict, Any
from app.utils.redis_cache import redis_client

logger = logging.getLogger(__name__)

NOTIFICATION_CHANNEL = "notifications:inbox"


def publish_notification_event(
    user_id: str,
    org_id: str,
    notification_type: str,
    payload: Dict[str, Any]
) -> bool:
    """
    Publish a notification event to Redis Pub/Sub channel.
    
    Args:
        user_id: Target user ID
        org_id: Organization ID
        notification_type: Type of notification (e.g., 'inbox_new')
        payload: Notification payload data
        
    Returns:
        True if published successfully, False otherwise
    """
    if not redis_client:
        logger.warning("Redis client not available, cannot publish notification event")
        return False
    
    try:
        event = {
            "user_id": user_id,
            "org_id": org_id,
            "type": notification_type,
            "payload": payload
        }
        
        message = json.dumps(event)
        redis_client.publish(NOTIFICATION_CHANNEL, message)
        logger.debug(f"Published notification event for user {user_id}: {notification_type}")
        return True
    except Exception as e:
        logger.error(f"Failed to publish notification event: {e}", exc_info=True)
        return False

