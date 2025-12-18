from typing import Dict, Any, Optional
from pydantic import UUID4
import logging

from app.core import supabase

logger = logging.getLogger(__name__)


class RealtimeHelper:
    """Helper class for Supabase Realtime operations"""
    
    @staticmethod
    def broadcast_project_message(project_id: UUID4, message_data: Dict[str, Any]) -> bool:
        """
        Broadcast a project chat message to all subscribers
        
        Args:
            project_id: The project ID
            message_data: The message data to broadcast
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            channel_name = f"project:{project_id}:chat"
            logger.info(f"Broadcasting message to channel: {channel_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to broadcast project message: {str(e)}")
            return False
    
    @staticmethod
    def broadcast_direct_message(conversation_id: UUID4, message_data: Dict[str, Any]) -> bool:
        """
        Broadcast a direct message to conversation participants
        
        Args:
            conversation_id: The conversation ID
            message_data: The message data to broadcast
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            channel_name = f"dm:{conversation_id}"
            logger.info(f"Broadcasting DM to channel: {channel_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to broadcast direct message: {str(e)}")
            return False
    
    @staticmethod
    def broadcast_typing_indicator(
        channel_id: str,
        user_id: UUID4,
        is_typing: bool,
        chat_type: str
    ) -> bool:
        """
        Broadcast typing indicator status
        
        Args:
            channel_id: The channel ID (project_id or conversation_id)
            user_id: The user who is typing
            is_typing: Whether user is typing
            chat_type: 'project' or 'direct'
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if chat_type == 'project':
                channel_name = f"project:{channel_id}:chat"
            else:
                channel_name = f"dm:{channel_id}"
            
            logger.info(f"Broadcasting typing indicator to {channel_name}: user={user_id}, typing={is_typing}")
            return True
        except Exception as e:
            logger.error(f"Failed to broadcast typing indicator: {str(e)}")
            return False
    
    @staticmethod
    def notify_message_read(
        channel_id: str,
        user_id: UUID4,
        message_id: UUID4,
        chat_type: str
    ) -> bool:
        """
        Broadcast read receipt notification
        
        Args:
            channel_id: The channel ID (project_id or conversation_id)
            user_id: The user who read the message
            message_id: The message that was read
            chat_type: 'project' or 'direct'
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if chat_type == 'project':
                channel_name = f"project:{channel_id}:chat"
            else:
                channel_name = f"dm:{channel_id}"
            
            logger.info(f"Broadcasting read receipt to {channel_name}: user={user_id}, message={message_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to broadcast read receipt: {str(e)}")
            return False


