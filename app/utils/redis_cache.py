import redis
import json
import logging
from typing import Optional, Dict, Any
from app.core.config import Settings

logger = logging.getLogger(__name__)
settings = Settings()

try:
    redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    redis_client.ping()
except Exception as e:
    logger.warning(f"Redis connection failed: {e}. Cache will be disabled.")
    redis_client = None


class UserCache:
    """Cache utility for user profile information"""
    
    CACHE_TTL = settings.S3_PRESIGNED_URL_EXPIRATION  # Match presigned URL expiration
    
    @staticmethod
    def get_user(user_id: str) -> Optional[Dict[str, Any]]:
        """Get user from cache"""
        if not redis_client:
            return None
        
        try:
            cached = redis_client.get(f"user:{user_id}")
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"Error getting user from cache: {e}")
        
        return None
    
    @staticmethod
    def set_user(user_id: str, user_data: Dict[str, Any]) -> None:
        """Cache user data"""
        if not redis_client:
            return
        
        try:
            redis_client.setex(
                f"user:{user_id}",
                UserCache.CACHE_TTL,
                json.dumps(user_data)
            )
        except Exception as e:
            logger.warning(f"Error caching user: {e}")
    
    @staticmethod
    def delete_user(user_id: str) -> None:
        """Invalidate user cache"""
        if not redis_client:
            return
        
        try:
            redis_client.delete(f"user:{user_id}")
        except Exception as e:
            logger.warning(f"Error deleting user from cache: {e}")
    
    @staticmethod
    def get_user_info(user_id: str) -> Optional[Dict[str, Any]]:
        """Get formatted user info from cache"""
        user = UserCache.get_user(user_id)
        if user:
            return {
                'id': user.get('id'),
                'display_name': user.get('display_name'),
                'avatar_url': user.get('avatar_url')
            }
        return None


class ProjectSummaryCache:
    """Cache utility for project summary information"""
    
    CACHE_TTL = 300  # 5 minutes cache for project summary
    
    @staticmethod
    def get_summary(project_id: str) -> Optional[Dict[str, Any]]:
        """Get project summary from cache"""
        if not redis_client:
            return None
        
        try:
            cached = redis_client.get(f"project_summary:{project_id}")
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"Error getting project summary from cache: {e}")
        
        return None
    
    @staticmethod
    def set_summary(project_id: str, summary_data: Dict[str, Any]) -> None:
        """Cache project summary data"""
        if not redis_client:
            return
        
        try:
            redis_client.setex(
                f"project_summary:{project_id}",
                ProjectSummaryCache.CACHE_TTL,
                json.dumps(summary_data)
            )
        except Exception as e:
            logger.warning(f"Error caching project summary: {e}")
    
    @staticmethod
    def delete_summary(project_id: str) -> None:
        """Invalidate project summary cache"""
        if not redis_client:
            return
        
        try:
            redis_client.delete(f"project_summary:{project_id}")
        except Exception as e:
            logger.warning(f"Error deleting project summary from cache: {e}")

