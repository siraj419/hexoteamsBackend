from fastapi import WebSocket
from typing import Dict, Set
from pydantic import UUID4
import json
import logging

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections for real-time chat"""
    
    def __init__(self):
        # project_id -> set of (user_id, websocket)
        self.project_connections: Dict[str, Set[tuple]] = {}
        # conversation_id -> set of (user_id, websocket)
        self.dm_connections: Dict[str, Set[tuple]] = {}
        # user_id -> list of websockets (user can have multiple tabs)
        self.user_connections: Dict[str, list] = {}
    
    async def connect_project(self, websocket: WebSocket, project_id: str, user_id: str):
        await websocket.accept()
        if project_id not in self.project_connections:
            self.project_connections[project_id] = set()
        self.project_connections[project_id].add((user_id, websocket))
        self._track_user_connection(user_id, websocket)
        logger.info(f"User {user_id} connected to project {project_id}")
    
    async def connect_dm(self, websocket: WebSocket, conversation_id: str, user_id: str):
        await websocket.accept()
        # Normalize conversation_id for consistent storage
        conversation_id = str(conversation_id).lower().strip()
        if conversation_id not in self.dm_connections:
            self.dm_connections[conversation_id] = set()
        self.dm_connections[conversation_id].add((user_id, websocket))
        self._track_user_connection(user_id, websocket)
        logger.info(f"User {user_id} connected to conversation {conversation_id}")
    
    def disconnect_project(self, websocket: WebSocket, project_id: str, user_id: str):
        if project_id in self.project_connections:
            self.project_connections[project_id].discard((user_id, websocket))
            if not self.project_connections[project_id]:
                del self.project_connections[project_id]
        self._untrack_user_connection(user_id, websocket)
        logger.info(f"User {user_id} disconnected from project {project_id}")
    
    def disconnect_dm(self, websocket: WebSocket, conversation_id: str, user_id: str):
        # Normalize conversation_id for consistent lookup
        conversation_id = str(conversation_id).lower().strip()
        if conversation_id in self.dm_connections:
            self.dm_connections[conversation_id].discard((user_id, websocket))
            if not self.dm_connections[conversation_id]:
                del self.dm_connections[conversation_id]
        self._untrack_user_connection(user_id, websocket)
        logger.info(f"User {user_id} disconnected from conversation {conversation_id}")
    
    async def broadcast_to_project(
        self, project_id: str, message: dict, exclude_user: str = None, sender_id: str = None):
        if project_id not in self.project_connections:
            return
        
        disconnected = []
        
        for user_id, websocket in self.project_connections[project_id]:
            if exclude_user and str(user_id).lower().strip() == str(exclude_user).lower().strip():
                continue
            
            personalized_message = message.copy()
            if sender_id:
                # Normalize both IDs to lowercase strings for comparison
                normalized_user_id = str(user_id).lower().strip()
                normalized_sender_id = str(sender_id).lower().strip()
                is_own = (normalized_user_id == normalized_sender_id)
                
                # Set appropriate field based on message type
                if message.get('type') in ['message', 'message_edited', 'message_deleted']:
                    personalized_message['is_own_message'] = is_own
                else:
                    personalized_message['is_own'] = is_own
                
                personalized_message['sender_id'] = sender_id
            
            data = json.dumps(personalized_message)
            try:
                await websocket.send_text(data)
            except Exception as e:
                logger.error(f"Failed to send to user {user_id}: {e}")
                disconnected.append((user_id, websocket))
        
        for conn in disconnected:
            self.project_connections[project_id].discard(conn)
    
    async def broadcast_to_dm(self, conversation_id: str, message: dict, exclude_user: str = None, sender_id: str = None):
        # Normalize conversation_id to string and lowercase for consistent lookup
        conversation_id = str(conversation_id).lower().strip()
        
        if conversation_id not in self.dm_connections:
            logger.debug(f"No active DM connections found for conversation_id: {conversation_id}")
            return
        
        disconnected = []
        
        for user_id, websocket in self.dm_connections[conversation_id]:
            if exclude_user and str(user_id).lower().strip() == str(exclude_user).lower().strip():
                continue
            
            personalized_message = message.copy()
            if sender_id:
                # Normalize both IDs to lowercase strings for comparison
                normalized_user_id = str(user_id).lower().strip()
                normalized_sender_id = str(sender_id).lower().strip()
                is_own = (normalized_user_id == normalized_sender_id)
                
                # Set appropriate field based on message type
                if message.get('type') in ['message', 'message_edited', 'message_deleted']:
                    personalized_message['is_own_message'] = is_own
                else:
                    personalized_message['is_own'] = is_own
                
                personalized_message['sender_id'] = sender_id
            
            data = json.dumps(personalized_message)
            try:
                await websocket.send_text(data)
            except Exception as e:
                logger.error(f"Failed to send to user {user_id}: {e}")
                disconnected.append((user_id, websocket))
        
        for conn in disconnected:
            self.dm_connections[conversation_id].discard(conn)
    
    async def send_to_user(self, user_id: str, message: dict):
        if user_id not in self.user_connections:
            return
        
        data = json.dumps(message)
        disconnected = []
        
        for websocket in self.user_connections[user_id]:
            try:
                await websocket.send_text(data)
            except Exception:
                disconnected.append(websocket)
        
        for ws in disconnected:
            self.user_connections[user_id].remove(ws)
    
    def _track_user_connection(self, user_id: str, websocket: WebSocket):
        if user_id not in self.user_connections:
            self.user_connections[user_id] = []
        self.user_connections[user_id].append(websocket)
    
    def _untrack_user_connection(self, user_id: str, websocket: WebSocket):
        if user_id in self.user_connections:
            if websocket in self.user_connections[user_id]:
                self.user_connections[user_id].remove(websocket)
            if not self.user_connections[user_id]:
                del self.user_connections[user_id]
    
    async def broadcast_inbox_notification(self, org_id: str, user_id: str, message: dict):
        """Broadcast inbox notification to user's connections for specific org"""
        if not hasattr(self, 'inbox_connections'):
            self.inbox_connections = {}
        
        connection_key = f"inbox:{org_id}:{user_id}"
        
        if connection_key not in self.inbox_connections:
            logger.debug(f"No inbox connections for {connection_key}")
            return
        
        data = json.dumps(message)
        disconnected = []
        
        for websocket in self.inbox_connections[connection_key]:
            try:
                await websocket.send_text(data)
            except Exception as e:
                logger.error(f"Failed to send inbox notification to user {user_id}: {e}")
                disconnected.append(websocket)
        
        for ws in disconnected:
            self.inbox_connections[connection_key].remove(ws)
            if not self.inbox_connections[connection_key]:
                del self.inbox_connections[connection_key]


manager = ConnectionManager()

