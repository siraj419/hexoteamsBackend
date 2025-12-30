from fastapi import WebSocket
from typing import Dict, Optional, Set
import json
import logging
import uuid
import os
from datetime import datetime, timezone
from app.utils.redis_cache import redis_client

logger = logging.getLogger(__name__)

# Generate unique instance ID for this server instance
INSTANCE_ID = os.environ.get("INSTANCE_ID", str(uuid.uuid4()))

# Connection TTL in seconds (1 hour)
CONNECTION_TTL = 3600


class ConnectionManager:
    """Manages WebSocket connections using Redis for distributed storage"""
    
    def __init__(self):
        # Local mapping: connection_id -> websocket object
        # WebSocket objects cannot be serialized, so we keep them in memory
        self.local_connections: Dict[str, WebSocket] = {}
        # Local mapping: connection_id -> connection metadata
        self.connection_metadata: Dict[str, dict] = {}
        
        # Initialize Redis pub/sub for cross-instance communication
        self._init_pubsub()
    
    def _init_pubsub(self):
        """Initialize Redis pub/sub channels for cross-instance messaging"""
        if not redis_client:
            self.pubsub = None
            return
        
        try:
            # Note: Pub/sub listener would need to run in a background task
            # For now, we use direct Redis publish for cross-instance communication
            # Each instance can check if connections are local before publishing
            self.pubsub = None
            logger.info("Redis pub/sub available for WebSocket broadcasting")
        except Exception as e:
            logger.warning(f"Failed to initialize Redis pub/sub: {e}")
            self.pubsub = None
    
    def _generate_connection_id(self) -> str:
        """Generate unique connection ID"""
        return f"{INSTANCE_ID}:{uuid.uuid4().hex}"
    
    def _get_redis_key(self, key_type: str, identifier: str) -> str:
        """Build Redis key with namespace"""
        return f"ws:{key_type}:{identifier}"
    
    def _store_connection_metadata(
        self, 
        connection_id: str, 
        user_id: str, 
        connection_type: str,
        **kwargs
    ) -> bool:
        """Store connection metadata in Redis"""
        if not redis_client:
            return False
        
        try:
            metadata = {
                "connection_id": connection_id,
                "user_id": user_id,
                "connection_type": connection_type,
                "instance_id": INSTANCE_ID,
                "created_at": datetime.now(timezone.utc).isoformat(),
                **kwargs
            }
            
            # Store connection metadata with TTL
            key = self._get_redis_key("connection", connection_id)
            redis_client.setex(
                key,
                CONNECTION_TTL,
                json.dumps(metadata)
            )
            
            # Add to appropriate sets based on connection type
            if connection_type == "project" and "project_id" in kwargs:
                project_id = kwargs["project_id"]
                redis_client.sadd(
                    self._get_redis_key("project", project_id),
                    connection_id
                )
                redis_client.expire(
                    self._get_redis_key("project", project_id),
                    CONNECTION_TTL
                )
            
            elif connection_type == "dm" and "conversation_id" in kwargs:
                conversation_id = kwargs["conversation_id"]
                redis_client.sadd(
                    self._get_redis_key("dm", conversation_id),
                    connection_id
                )
                redis_client.expire(
                    self._get_redis_key("dm", conversation_id),
                    CONNECTION_TTL
                )
            
            elif connection_type == "inbox" and "org_id" in kwargs:
                org_id = kwargs["org_id"]
                connection_key = f"{org_id}:{user_id}"
                redis_client.sadd(
                    self._get_redis_key("inbox", connection_key),
                    connection_id
                )
                redis_client.expire(
                    self._get_redis_key("inbox", connection_key),
                    CONNECTION_TTL
                )
            
            # Always track user connections
            redis_client.sadd(
                self._get_redis_key("user", user_id),
                connection_id
            )
            redis_client.expire(
                self._get_redis_key("user", user_id),
                CONNECTION_TTL
            )
            
            return True
        except Exception as e:
            logger.error(f"Failed to store connection metadata: {e}")
            return False
    
    def _remove_connection_metadata(self, connection_id: str, metadata: dict) -> bool:
        """Remove connection metadata from Redis"""
        if not redis_client:
            return False
        
        try:
            # Remove connection metadata
            key = self._get_redis_key("connection", connection_id)
            redis_client.delete(key)
            
            connection_type = metadata.get("connection_type")
            user_id = metadata.get("user_id")
            
            # Remove from appropriate sets
            if connection_type == "project" and "project_id" in metadata:
                project_id = metadata["project_id"]
                redis_client.srem(
                    self._get_redis_key("project", project_id),
                    connection_id
                )
            
            elif connection_type == "dm" and "conversation_id" in metadata:
                conversation_id = metadata["conversation_id"]
                redis_client.srem(
                    self._get_redis_key("dm", conversation_id),
                    connection_id
                )
            
            elif connection_type == "inbox" and "org_id" in metadata:
                org_id = metadata["org_id"]
                connection_key = f"{org_id}:{user_id}"
                redis_client.srem(
                    self._get_redis_key("inbox", connection_key),
                    connection_id
                )
            
            # Remove from user connections
            if user_id:
                redis_client.srem(
                    self._get_redis_key("user", user_id),
                    connection_id
                )
            
            return True
        except Exception as e:
            logger.error(f"Failed to remove connection metadata: {e}")
            return False
    
    def _get_connection_metadata(self, connection_id: str) -> Optional[dict]:
        """Get connection metadata from Redis or local cache"""
        # Check local cache first
        if connection_id in self.connection_metadata:
            return self.connection_metadata[connection_id]
        
        if not redis_client:
            return None
        
        try:
            key = self._get_redis_key("connection", connection_id)
            data = redis_client.get(key)
            if data:
                metadata = json.loads(data)
                # Cache locally
                self.connection_metadata[connection_id] = metadata
                return metadata
        except Exception as e:
            logger.error(f"Failed to get connection metadata: {e}")
        
        return None
    
    def _get_connections_from_redis(self, key: str) -> Set[str]:
        """Get all connection IDs from a Redis set"""
        if not redis_client:
            return set()
        
        try:
            members = redis_client.smembers(key)
            return set(members) if members else set()
        except Exception as e:
            logger.error(f"Failed to get connections from Redis: {e}")
            return set()
    
    def _get_local_connections_by_type(self, connection_type: str, **filters) -> Set[str]:
        """Get local connection IDs filtered by type and optional filters"""
        local_conn_ids = set()
        
        for connection_id, metadata in self.connection_metadata.items():
            if metadata.get("connection_type") != connection_type:
                continue
            
            # Apply filters
            match = True
            for key, value in filters.items():
                if metadata.get(key) != value:
                    match = False
                    break
            
            if match and connection_id in self.local_connections:
                local_conn_ids.add(connection_id)
        
        return local_conn_ids
    
    async def connect_project(self, websocket: WebSocket, project_id: str, user_id: str):
        """Connect a user to a project chat"""
        await websocket.accept()
        
        connection_id = self._generate_connection_id()
        
        # Store websocket locally
        self.local_connections[connection_id] = websocket
        
        # Store metadata in Redis
        metadata = {
            "project_id": project_id,
            "user_id": user_id,
            "connection_type": "project"
        }
        self.connection_metadata[connection_id] = metadata
        self._store_connection_metadata(connection_id, user_id, "project", project_id=project_id)
        
        logger.info(f"User {user_id} connected to project {project_id} (connection: {connection_id})")
    
    async def connect_dm(self, websocket: WebSocket, conversation_id: str, user_id: str):
        """Connect a user to a direct message conversation"""
        await websocket.accept()
        
        conversation_id = str(conversation_id).lower().strip()
        connection_id = self._generate_connection_id()
        
        # Store websocket locally
        self.local_connections[connection_id] = websocket
        
        # Store metadata in Redis
        metadata = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "connection_type": "dm"
        }
        self.connection_metadata[connection_id] = metadata
        self._store_connection_metadata(connection_id, user_id, "dm", conversation_id=conversation_id)
        
        logger.info(f"User {user_id} connected to conversation {conversation_id} (connection: {connection_id})")
    
    def disconnect_project(self, websocket: WebSocket, project_id: str, user_id: str):
        """Disconnect a user from a project chat"""
        # Find connection ID by websocket
        connection_id = None
        for cid, ws in self.local_connections.items():
            if ws == websocket:
                connection_id = cid
                break
        
        if not connection_id:
            logger.warning(f"Connection not found for project {project_id}, user {user_id}")
            return
        
        # Get metadata
        metadata = self.connection_metadata.get(connection_id, {})
        
        # Remove from local storage
        if connection_id in self.local_connections:
            del self.local_connections[connection_id]
        if connection_id in self.connection_metadata:
            del self.connection_metadata[connection_id]
        
        # Remove from Redis
        self._remove_connection_metadata(connection_id, metadata)
        
        logger.info(f"User {user_id} disconnected from project {project_id} (connection: {connection_id})")
    
    def disconnect_dm(self, websocket: WebSocket, conversation_id: str, user_id: str):
        """Disconnect a user from a direct message conversation"""
        conversation_id = str(conversation_id).lower().strip()
        
        # Find connection ID by websocket
        connection_id = None
        for cid, ws in self.local_connections.items():
            if ws == websocket:
                connection_id = cid
                break
        
        if not connection_id:
            logger.warning(f"Connection not found for conversation {conversation_id}, user {user_id}")
            return
        
        # Get metadata
        metadata = self.connection_metadata.get(connection_id, {})
        
        # Remove from local storage
        if connection_id in self.local_connections:
            del self.local_connections[connection_id]
        if connection_id in self.connection_metadata:
            del self.connection_metadata[connection_id]
        
        # Remove from Redis
        self._remove_connection_metadata(connection_id, metadata)
        
        logger.info(f"User {user_id} disconnected from conversation {conversation_id} (connection: {connection_id})")
    
    def disconnect_inbox(self, websocket: WebSocket, org_id: str, user_id: str):
        """Disconnect a user from inbox notifications"""
        connection_key = f"inbox:{org_id}:{user_id}"
        
        # Find connection ID by websocket
        connection_id = None
        for cid, ws in self.local_connections.items():
            if ws == websocket:
                connection_id = cid
                break
        
        if not connection_id:
            logger.warning(f"Connection not found for inbox {connection_key}")
            return
        
        # Get metadata
        metadata = self.connection_metadata.get(connection_id, {})
        
        # Remove from local storage
        if connection_id in self.local_connections:
            del self.local_connections[connection_id]
        if connection_id in self.connection_metadata:
            del self.connection_metadata[connection_id]
        
        # Remove from Redis
        self._remove_connection_metadata(connection_id, metadata)
        
        logger.info(f"User {user_id} disconnected from inbox for org {org_id} (connection: {connection_id})")
    
    async def _send_to_connection(self, connection_id: str, message: dict) -> bool:
        """
        Send message to a connection if found in local storage or Redis.
        Returns True if message was sent successfully, False otherwise.
        """
        # Check local storage first - this is the source of truth for active connections
        websocket = self.local_connections.get(connection_id)
        if websocket:
            return await self._send_to_local_websocket(connection_id, websocket, message)
        
        # Not in local storage - check Redis metadata
        metadata = self._get_connection_metadata(connection_id)
        if not metadata:
            logger.debug(f"Connection {connection_id} not found in local storage or Redis")
            return False
        
        # Connection exists in Redis - check instance
        instance_id = metadata.get("instance_id")
        
        if instance_id == INSTANCE_ID:
            # Same instance but not in local storage = stale metadata
            logger.warning(f"Connection {connection_id} has metadata in Redis but websocket missing locally. Cleaning up stale metadata.")
            self._remove_connection_metadata(connection_id, metadata)
            return False
        else:
            # Connection is on another instance - can't send directly
            # In a multi-instance setup, you'd use Redis pub/sub here
            logger.debug(f"Connection {connection_id} is on instance {instance_id}, not local (current: {INSTANCE_ID})")
            return False
    
    async def _send_to_local_websocket(self, connection_id: str, websocket: WebSocket, message: dict) -> bool:
        """Send message to a local websocket connection"""
        try:
            # Check websocket state before sending
            if websocket.client_state.name != "CONNECTED":
                logger.warning(f"WebSocket {connection_id} not CONNECTED (state: {websocket.client_state.name})")
                self._cleanup_connection(connection_id)
                return False
            
            # Send message
            data = json.dumps(message)
            await websocket.send_text(data)
            logger.debug(f"Message sent to connection {connection_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send to connection {connection_id}: {e}", exc_info=True)
            self._cleanup_connection(connection_id)
            return False
    
    def _cleanup_connection(self, connection_id: str) -> None:
        """Remove connection from local storage and Redis"""
        metadata = self.connection_metadata.get(connection_id, {})
        
        if connection_id in self.local_connections:
            del self.local_connections[connection_id]
        
        if connection_id in self.connection_metadata:
            del self.connection_metadata[connection_id]
        
        if metadata:
            self._remove_connection_metadata(connection_id, metadata)
    
    async def broadcast_to_project(
        self, 
        project_id: str, 
        message: dict, 
        exclude_user: str = None, 
        sender_id: str = None
    ):
        """Broadcast message to all connections in a project"""
        # Get connections from Redis
        key = self._get_redis_key("project", project_id)
        redis_connection_ids = self._get_connections_from_redis(key)
        
        # Get local connections as fallback
        local_connection_ids = self._get_local_connections_by_type("project", project_id=project_id)
        
        # Combine both sets (Redis + local)
        connection_ids = redis_connection_ids | local_connection_ids
        
        if not connection_ids:
            return
        
        disconnected = []
        
        for connection_id in connection_ids:
            metadata = self._get_connection_metadata(connection_id)
            if not metadata:
                disconnected.append(connection_id)
                continue
            
            user_id = metadata.get("user_id")
            
            # Skip excluded user
            if exclude_user and str(user_id).lower().strip() == str(exclude_user).lower().strip():
                continue
            
            # Personalize message
            personalized_message = message.copy()
            if sender_id:
                normalized_user_id = str(user_id).lower().strip()
                normalized_sender_id = str(sender_id).lower().strip()
                is_own = (normalized_user_id == normalized_sender_id)
                
                if message.get('type') in ['message', 'message_edited', 'message_deleted']:
                    personalized_message['is_own_message'] = is_own
                else:
                    personalized_message['is_own'] = is_own
                
                personalized_message['sender_id'] = sender_id
            
            # Send message
            success = await self._send_to_connection(connection_id, personalized_message)
            if not success:
                disconnected.append(connection_id)
        
        # Clean up disconnected connections
        for connection_id in disconnected:
            metadata = self._get_connection_metadata(connection_id)
            if metadata:
                self._remove_connection_metadata(connection_id, metadata)
    
    async def broadcast_to_dm(
        self, 
        conversation_id: str, 
        message: dict, 
        exclude_user: str = None, 
        sender_id: str = None
    ):
        """Broadcast message to all connections in a DM conversation"""
        conversation_id = str(conversation_id).lower().strip()
        
        # Get connections from Redis
        key = self._get_redis_key("dm", conversation_id)
        redis_connection_ids = self._get_connections_from_redis(key)
        
        # Get local connections as fallback
        local_connection_ids = self._get_local_connections_by_type("dm", conversation_id=conversation_id)
        
        # Combine both sets (Redis + local)
        connection_ids = redis_connection_ids | local_connection_ids
        
        if not connection_ids:
            logger.debug(f"No active DM connections found for conversation_id: {conversation_id}")
            return
        
        disconnected = []
        
        for connection_id in connection_ids:
            metadata = self._get_connection_metadata(connection_id)
            if not metadata:
                disconnected.append(connection_id)
                continue
            
            user_id = metadata.get("user_id")
            
            # Skip excluded user
            if exclude_user and str(user_id).lower().strip() == str(exclude_user).lower().strip():
                continue
            
            # Personalize message
            personalized_message = message.copy()
            if sender_id:
                normalized_user_id = str(user_id).lower().strip()
                normalized_sender_id = str(sender_id).lower().strip()
                is_own = (normalized_user_id == normalized_sender_id)
                
                if message.get('type') in ['message', 'message_edited', 'message_deleted']:
                    personalized_message['is_own_message'] = is_own
                else:
                    personalized_message['is_own'] = is_own
                
                personalized_message['sender_id'] = sender_id
            
            # Send message
            success = await self._send_to_connection(connection_id, personalized_message)
            if not success:
                disconnected.append(connection_id)
        
        # Clean up disconnected connections
        for connection_id in disconnected:
            metadata = self._get_connection_metadata(connection_id)
            if metadata:
                self._remove_connection_metadata(connection_id, metadata)
    
    async def send_to_user(self, user_id: str, message: dict):
        """Send message to all connections of a user"""
        # Get connections from Redis
        key = self._get_redis_key("user", user_id)
        redis_connection_ids = self._get_connections_from_redis(key)
        
        # Get local connections as fallback
        local_connection_ids = self._get_local_connections_by_type("project", user_id=user_id)
        local_connection_ids |= self._get_local_connections_by_type("dm", user_id=user_id)
        local_connection_ids |= self._get_local_connections_by_type("inbox", user_id=user_id)
        
        # Combine both sets (Redis + local)
        connection_ids = redis_connection_ids | local_connection_ids
        
        if not connection_ids:
            return
        
        disconnected = []
        data = json.dumps(message)
        
        for connection_id in connection_ids:
            success = await self._send_to_connection(connection_id, message)
            if not success:
                disconnected.append(connection_id)
        
        # Clean up disconnected connections
        for connection_id in disconnected:
            metadata = self._get_connection_metadata(connection_id)
            if metadata:
                self._remove_connection_metadata(connection_id, metadata)
    
    async def connect_inbox(self, websocket: WebSocket, org_id: str, user_id: str):
        """Connect a user to inbox notifications"""
        await websocket.accept()
        
        connection_id = self._generate_connection_id()
        
        # Store metadata first (before websocket) to ensure consistency
        metadata = {
            "org_id": org_id,
            "user_id": user_id,
            "connection_type": "inbox"
        }
        self.connection_metadata[connection_id] = metadata
        
        # Store websocket locally
        self.local_connections[connection_id] = websocket
        
        # Store metadata in Redis
        redis_success = self._store_connection_metadata(connection_id, user_id, "inbox", org_id=org_id)
        
        if not redis_success:
            logger.warning(f"Failed to store connection metadata in Redis for {connection_id}, but websocket stored locally")
        
        # Verify storage
        if connection_id not in self.local_connections:
            logger.error(f"CRITICAL: Connection {connection_id} not found in local_connections after storage!")
        else:
            logger.info(f"User {user_id} connected to inbox for org {org_id} (connection: {connection_id}, local: {connection_id in self.local_connections}, redis: {redis_success})")
    
    async def broadcast_inbox_notification(self, org_id: str, user_id: str, message: dict):
        """Broadcast inbox notification to user's connections for specific org"""
        connection_key = f"{org_id}:{user_id}"
        
        # Get connections from Redis
        key = self._get_redis_key("inbox", connection_key)
        redis_connection_ids = self._get_connections_from_redis(key)
        
        # Get local connections as fallback
        local_connection_ids = self._get_local_connections_by_type("inbox", org_id=org_id, user_id=user_id)
        
        # Combine both sets (Redis + local)
        connection_ids = redis_connection_ids | local_connection_ids
        
        # Verify which connection IDs actually have websockets and clean up stale metadata
        valid_connection_ids = set()
        stale_connection_ids = []
        
        for conn_id in connection_ids:
            if conn_id in self.local_connections:
                valid_connection_ids.add(conn_id)
            else:
                # Connection ID exists in Redis/local metadata but websocket is missing
                logger.warning(f"Connection {conn_id} found in Redis/local metadata but websocket missing from local_connections")
                stale_connection_ids.append(conn_id)
        
        # Clean up stale metadata
        for conn_id in stale_connection_ids:
            metadata = self._get_connection_metadata(conn_id)
            if metadata:
                self._remove_connection_metadata(conn_id, metadata)
        
        if not valid_connection_ids:
            logger.info(f"No active inbox connections for inbox:{connection_key}. "
                       f"Redis connection IDs: {redis_connection_ids}, "
                       f"Local connection IDs: {local_connection_ids}, "
                       f"Total local connections: {len(self.local_connections)}, "
                       f"Cleaned up {len(stale_connection_ids)} stale connections")
            return
        
        connection_ids = valid_connection_ids
        
        disconnected = []
        connection_count = len(connection_ids)
        
        logger.info(f"Broadcasting inbox notification to {connection_count} connection(s) for {connection_key}")
        
        for connection_id in connection_ids:
            success = await self._send_to_connection(connection_id, message)
            if not success:
                disconnected.append(connection_id)
        
        # Clean up disconnected connections
        for connection_id in disconnected:
            metadata = self._get_connection_metadata(connection_id)
            if metadata:
                self._remove_connection_metadata(connection_id, metadata)
        
        if disconnected:
            logger.info(f"Cleaned up {len(disconnected)} disconnected inbox connections")
    
    def cleanup_stale_connections(self):
        """Clean up stale connections from local storage"""
        if not redis_client:
            return
        
        try:
            stale_connections = []
            
            for connection_id, websocket in list(self.local_connections.items()):
                # Check if connection metadata still exists in Redis
                key = self._get_redis_key("connection", connection_id)
                if not redis_client.exists(key):
                    stale_connections.append(connection_id)
            
            # Remove stale connections
            for connection_id in stale_connections:
                metadata = self.connection_metadata.get(connection_id, {})
                if connection_id in self.local_connections:
                    del self.local_connections[connection_id]
                if connection_id in self.connection_metadata:
                    del self.connection_metadata[connection_id]
                self._remove_connection_metadata(connection_id, metadata)
            
            if stale_connections:
                logger.info(f"Cleaned up {len(stale_connections)} stale connections")
        except Exception as e:
            logger.error(f"Error cleaning up stale connections: {e}")
    
    def get_connection_stats(self) -> dict:
        """Get statistics about active connections"""
        stats = {
            "local_connections": len(self.local_connections),
            "instance_id": INSTANCE_ID
        }
        
        if redis_client:
            try:
                # Count connections by type from Redis
                project_keys = list(redis_client.scan_iter(match="ws:project:*"))
                dm_keys = list(redis_client.scan_iter(match="ws:dm:*"))
                user_keys = list(redis_client.scan_iter(match="ws:user:*"))
                inbox_keys = list(redis_client.scan_iter(match="ws:inbox:*"))
                
                stats["redis_project_rooms"] = len(project_keys)
                stats["redis_dm_rooms"] = len(dm_keys)
                stats["redis_user_connections"] = len(user_keys)
                stats["redis_inbox_rooms"] = len(inbox_keys)
            except Exception as e:
                logger.error(f"Error getting connection stats: {e}")
        
        return stats


manager = ConnectionManager()
