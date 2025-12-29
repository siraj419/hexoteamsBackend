import redis
import json
import logging
from typing import Optional, Dict, Any, List, Union, Callable
from datetime import timedelta
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
    """Cache utility for user profile information - Uses CacheService internally"""
    
    CACHE_TTL = settings.S3_PRESIGNED_URL_EXPIRATION  # Match presigned URL expiration
    _cache = None
    
    @classmethod
    def _get_cache(cls):
        """Lazy initialization of cache service"""
        if cls._cache is None:
            from app.utils.redis_cache import CacheService
            cls._cache = CacheService(namespace="app", default_ttl=cls.CACHE_TTL)
        return cls._cache
    
    @staticmethod
    def get_user(user_id: str) -> Optional[Dict[str, Any]]:
        """Get user from cache"""
        return UserCache._get_cache().get(f"user:{user_id}")
    
    @staticmethod
    def set_user(user_id: str, user_data: Dict[str, Any]) -> None:
        """Cache user data"""
        UserCache._get_cache().set(f"user:{user_id}", user_data, ttl=UserCache.CACHE_TTL)
    
    @staticmethod
    def delete_user(user_id: str) -> None:
        """Invalidate user cache"""
        UserCache._get_cache().delete(f"user:{user_id}")
    
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
    """Cache utility for project summary information - Uses CacheService internally"""
    
    CACHE_TTL = 300  # 5 minutes cache for project summary
    _cache = None
    
    @classmethod
    def _get_cache(cls):
        """Lazy initialization of cache service"""
        if cls._cache is None:
            from app.utils.redis_cache import CacheService
            cls._cache = CacheService(namespace="app", default_ttl=cls.CACHE_TTL)
        return cls._cache
    
    @staticmethod
    def get_summary(project_id: str) -> Optional[Dict[str, Any]]:
        """Get project summary from cache"""
        return ProjectSummaryCache._get_cache().get(f"project_summary:{project_id}")
    
    @staticmethod
    def set_summary(project_id: str, summary_data: Dict[str, Any]) -> None:
        """Cache project summary data"""
        ProjectSummaryCache._get_cache().set(f"project_summary:{project_id}", summary_data, ttl=ProjectSummaryCache.CACHE_TTL)
    
    @staticmethod
    def delete_summary(project_id: str) -> None:
        """Invalidate project summary cache"""
        ProjectSummaryCache._get_cache().delete(f"project_summary:{project_id}")
    
    @staticmethod
    def delete_many(project_ids: List[str]) -> int:
        """Batch invalidate project summary cache"""
        keys = [f"project_summary:{pid}" for pid in project_ids]
        return ProjectSummaryCache._get_cache().delete_many(keys)


class ActiveOrganizationCache:
    """Cache utility for active organization information - Uses CacheService internally"""
    
    CACHE_TTL = 300  # 5 minutes cache for active organization
    _cache = None
    
    @classmethod
    def _get_cache(cls):
        """Lazy initialization of cache service"""
        if cls._cache is None:
            from app.utils.redis_cache import CacheService
            cls._cache = CacheService(namespace="app", default_ttl=cls.CACHE_TTL)
        return cls._cache
    
    @staticmethod
    def get_organization(user_id: str) -> Optional[Dict[str, Any]]:
        """Get active organization from cache"""
        return ActiveOrganizationCache._get_cache().get(f"active_org:{user_id}")
    
    @staticmethod
    def set_organization(user_id: str, org_data: Dict[str, Any]) -> None:
        """Cache active organization data"""
        ActiveOrganizationCache._get_cache().set(f"active_org:{user_id}", org_data, ttl=ActiveOrganizationCache.CACHE_TTL)
    
    @staticmethod
    def delete_organization(user_id: str) -> None:
        """Invalidate active organization cache"""
        ActiveOrganizationCache._get_cache().delete(f"active_org:{user_id}")
    
    @staticmethod
    def delete_many(user_ids: List[str]) -> int:
        """Batch invalidate active organization cache for multiple users"""
        keys = [f"active_org:{uid}" for uid in user_ids]
        return ActiveOrganizationCache._get_cache().delete_many(keys)


class UserMeCache:
    """Cache utility for user /me endpoint response - Uses CacheService internally"""
    
    CACHE_TTL = 300  # 5 minutes cache for user me endpoint
    _cache = None
    
    @classmethod
    def _get_cache(cls):
        """Lazy initialization of cache service"""
        if cls._cache is None:
            from app.utils.redis_cache import CacheService
            cls._cache = CacheService(namespace="app", default_ttl=cls.CACHE_TTL)
        return cls._cache
    
    @staticmethod
    def get_user(user_id: str) -> Optional[Dict[str, Any]]:
        """Get user me data from cache"""
        return UserMeCache._get_cache().get(f"user_me:{user_id}")
    
    @staticmethod
    def set_user(user_id: str, user_data: Dict[str, Any]) -> None:
        """Cache user me data"""
        UserMeCache._get_cache().set(f"user_me:{user_id}", user_data, ttl=UserMeCache.CACHE_TTL)
    
    @staticmethod
    def delete_user(user_id: str) -> None:
        """Invalidate user me cache"""
        UserMeCache._get_cache().delete(f"user_me:{user_id}")


class CacheService:
    """
    Central cache service providing a unified interface for all cache operations.
    This service can be used throughout the application for consistent caching behavior.
    
    Features:
    - Get, set, delete operations
    - Pattern-based invalidation
    - Batch operations
    - TTL management
    - Namespace support
    - Cache statistics
    - Type-safe operations
    - Graceful error handling
    """
    
    def __init__(self, namespace: str = "app", default_ttl: int = 300):
        """
        Initialize cache service with namespace and default TTL.
        
        Args:
            namespace: Prefix for all cache keys (default: "app")
            default_ttl: Default time-to-live in seconds (default: 300)
        """
        self.namespace = namespace
        self.default_ttl = default_ttl
        self._stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletes": 0,
            "errors": 0
        }
    
    def _build_key(self, key: str) -> str:
        """Build full cache key with namespace prefix."""
        return f"{self.namespace}:{key}"
    
    def _serialize(self, value: Any) -> str:
        """Serialize value to JSON string."""
        return json.dumps(value)
    
    def _deserialize(self, value: str) -> Any:
        """Deserialize JSON string to Python object."""
        return json.loads(value)
    
    def get(self, key: str, default: Any = None) -> Optional[Any]:
        """
        Get a value from cache.
        
        Args:
            key: Cache key (without namespace prefix)
            default: Default value to return if key not found
            
        Returns:
            Cached value or default if not found
        """
        if not redis_client:
            self._stats["misses"] += 1
            return default
        
        full_key = self._build_key(key)
        
        try:
            cached = redis_client.get(full_key)
            if cached is not None:
                self._stats["hits"] += 1
                return self._deserialize(cached)
            else:
                self._stats["misses"] += 1
                return default
        except Exception as e:
            self._stats["errors"] += 1
            self._stats["misses"] += 1
            logger.warning(f"Error getting cache key '{key}': {e}")
            return default
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        Set a value in cache.
        
        Args:
            key: Cache key (without namespace prefix)
            value: Value to cache (will be JSON serialized)
            ttl: Time-to-live in seconds (uses default_ttl if None)
            
        Returns:
            True if successful, False otherwise
        """
        if not redis_client:
            return False
        
        full_key = self._build_key(key)
        ttl = ttl if ttl is not None else self.default_ttl
        
        try:
            serialized = self._serialize(value)
            redis_client.setex(full_key, ttl, serialized)
            self._stats["sets"] += 1
            return True
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error setting cache key '{key}': {e}")
            return False
    
    def delete(self, key: str) -> bool:
        """
        Delete a value from cache.
        
        Args:
            key: Cache key (without namespace prefix)
            
        Returns:
            True if successful, False otherwise
        """
        if not redis_client:
            return False
        
        full_key = self._build_key(key)
        
        try:
            result = redis_client.delete(full_key)
            self._stats["deletes"] += 1
            return result > 0
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error deleting cache key '{key}': {e}")
            return False
    
    def exists(self, key: str) -> bool:
        """
        Check if a key exists in cache.
        
        Args:
            key: Cache key (without namespace prefix)
            
        Returns:
            True if key exists, False otherwise
        """
        if not redis_client:
            return False
        
        full_key = self._build_key(key)
        
        try:
            return redis_client.exists(full_key) > 0
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error checking cache key existence '{key}': {e}")
            return False
    
    def get_many(self, keys: List[str]) -> Dict[str, Any]:
        """
        Get multiple values from cache in a single operation.
        
        Args:
            keys: List of cache keys (without namespace prefix)
            
        Returns:
            Dictionary mapping keys to their cached values (missing keys are omitted)
        """
        if not redis_client or not keys:
            return {}
        
        full_keys = [self._build_key(key) for key in keys]
        
        try:
            values = redis_client.mget(full_keys)
            result = {}
            
            for key, value in zip(keys, values):
                if value is not None:
                    try:
                        result[key] = self._deserialize(value)
                        self._stats["hits"] += 1
                    except Exception as e:
                        logger.warning(f"Error deserializing cache value for key '{key}': {e}")
                        self._stats["errors"] += 1
                else:
                    self._stats["misses"] += 1
            
            return result
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error getting multiple cache keys: {e}")
            return {}
    
    def set_many(self, mapping: Dict[str, Any], ttl: Optional[int] = None) -> int:
        """
        Set multiple values in cache.
        
        Args:
            mapping: Dictionary of key-value pairs to cache
            ttl: Time-to-live in seconds (uses default_ttl if None)
            
        Returns:
            Number of keys successfully set
        """
        if not redis_client or not mapping:
            return 0
        
        ttl = ttl if ttl is not None else self.default_ttl
        count = 0
        
        try:
            pipe = redis_client.pipeline()
            for key, value in mapping.items():
                full_key = self._build_key(key)
                serialized = self._serialize(value)
                pipe.setex(full_key, ttl, serialized)
            
            results = pipe.execute()
            count = sum(1 for r in results if r)
            self._stats["sets"] += count
            
            return count
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error setting multiple cache keys: {e}")
            return count
    
    def delete_many(self, keys: List[str]) -> int:
        """
        Delete multiple values from cache.
        
        Args:
            keys: List of cache keys (without namespace prefix)
            
        Returns:
            Number of keys successfully deleted
        """
        if not redis_client or not keys:
            return 0
        
        full_keys = [self._build_key(key) for key in keys]
        
        try:
            count = redis_client.delete(*full_keys)
            self._stats["deletes"] += count
            return count
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error deleting multiple cache keys: {e}")
            return 0
    
    def invalidate_pattern(self, pattern: str) -> int:
        """
        Invalidate all keys matching a pattern.
        
        Args:
            pattern: Pattern to match (e.g., "user:*", "*:project:123")
                    Can include wildcards: * (matches any characters)
            
        Returns:
            Number of keys deleted
        """
        if not redis_client:
            return 0
        
        full_pattern = self._build_key(pattern)
        count = 0
        
        try:
            for key in redis_client.scan_iter(match=full_pattern):
                redis_client.delete(key)
                count += 1
            
            self._stats["deletes"] += count
            return count
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error invalidating cache pattern '{pattern}': {e}")
            return count
    
    def invalidate_namespace(self, sub_namespace: Optional[str] = None) -> int:
        """
        Invalidate all keys in a namespace or sub-namespace.
        
        Args:
            sub_namespace: Optional sub-namespace to invalidate.
            If None, invalidates entire namespace.
            
        Returns:
            Number of keys deleted
        """
        if sub_namespace:
            pattern = f"{sub_namespace}:*"
        else:
            pattern = "*"
        
        return self.invalidate_pattern(pattern)
    
    def get_or_set(self, key: str, callable: Callable[[], Any], ttl: Optional[int] = None) -> Any:
        """
        Get a value from cache, or set it using a callable if not found.
        This implements the cache-aside pattern.
        
        Args:
            key: Cache key (without namespace prefix)
            callable: Function to call if cache miss (should return value to cache)
            ttl: Time-to-live in seconds (uses default_ttl if None)
            
        Returns:
            Cached value or result of callable
        """
        value = self.get(key)
        if value is not None:
            return value
        
        try:
            value = callable()
            self.set(key, value, ttl)
            return value
        except Exception as e:
            logger.warning(f"Error in get_or_set callable for key '{key}': {e}")
            raise
    
    def increment(self, key: str, amount: int = 1, ttl: Optional[int] = None) -> Optional[int]:
        """
        Increment a numeric value in cache.
        If key doesn't exist, it will be initialized to 0.
        
        Args:
            key: Cache key (without namespace prefix)
            amount: Amount to increment by (default: 1)
            ttl: Time-to-live in seconds (uses default_ttl if None)
            
        Returns:
            New value after increment, or None on error
        """
        if not redis_client:
            return None
        
        full_key = self._build_key(key)
        ttl = ttl if ttl is not None else self.default_ttl
        
        try:
            if not redis_client.exists(full_key):
                redis_client.setex(full_key, ttl, "0")
            
            new_value = redis_client.incrby(full_key, amount)
            self._stats["sets"] += 1
            return new_value
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error incrementing cache key '{key}': {e}")
            return None
    
    def decrement(self, key: str, amount: int = 1, ttl: Optional[int] = None) -> Optional[int]:
        """
        Decrement a numeric value in cache.
        If key doesn't exist, it will be initialized to 0.
        
        Args:
            key: Cache key (without namespace prefix)
            amount: Amount to decrement by (default: 1)
            ttl: Time-to-live in seconds (uses default_ttl if None)
            
        Returns:
            New value after decrement, or None on error
        """
        if not redis_client:
            return None
        
        full_key = self._build_key(key)
        ttl = ttl if ttl is not None else self.default_ttl
        
        try:
            if not redis_client.exists(full_key):
                redis_client.setex(full_key, ttl, "0")
            
            new_value = redis_client.decrby(full_key, amount)
            self._stats["sets"] += 1
            return new_value
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error decrementing cache key '{key}': {e}")
            return None
    
    def get_ttl(self, key: str) -> Optional[int]:
        """
        Get the remaining TTL (time-to-live) for a key.
        
        Args:
            key: Cache key (without namespace prefix)
            
        Returns:
            Remaining TTL in seconds, -1 if key exists but has no expiration,
            -2 if key doesn't exist, None on error
        """
        if not redis_client:
            return None
        
        full_key = self._build_key(key)
        
        try:
            return redis_client.ttl(full_key)
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error getting TTL for cache key '{key}': {e}")
            return None
    
    def extend_ttl(self, key: str, additional_seconds: int) -> bool:
        """
        Extend the TTL of an existing key.
        
        Args:
            key: Cache key (without namespace prefix)
            additional_seconds: Seconds to add to current TTL
            
        Returns:
            True if successful, False otherwise
        """
        if not redis_client:
            return False
        
        full_key = self._build_key(key)
        
        try:
            current_ttl = redis_client.ttl(full_key)
            if current_ttl > 0:
                new_ttl = current_ttl + additional_seconds
                return redis_client.expire(full_key, new_ttl)
            return False
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"Error extending TTL for cache key '{key}': {e}")
            return False
    
    def clear(self) -> bool:
        """
        Clear all keys in the namespace.
        
        Returns:
            True if successful, False otherwise
        """
        return self.invalidate_namespace() >= 0
    
    def get_stats(self) -> Dict[str, int]:
        """
        Get cache statistics.
        
        Returns:
            Dictionary with cache operation statistics
        """
        total_ops = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total_ops * 100) if total_ops > 0 else 0
        
        return {
            **self._stats,
            "total_operations": total_ops,
            "hit_rate_percent": round(hit_rate, 2)
        }
    
    def reset_stats(self) -> None:
        """Reset cache statistics."""
        self._stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletes": 0,
            "errors": 0
        }
    
    def health_check(self) -> bool:
        """
        Check if cache service is healthy (Redis connection available).
        
        Returns:
            True if Redis is available, False otherwise
        """
        if not redis_client:
            return False
        
        try:
            return redis_client.ping()
        except Exception:
            return False


# Global cache service instance with default settings
cache_service = CacheService(namespace="app", default_ttl=settings.CACHE_TTL)