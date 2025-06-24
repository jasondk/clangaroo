"""
Cache implementation for mcp-clangd server
"""

import aiosqlite
import hashlib
import json
import time
import logging
import asyncio
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .utils import log_error_with_context


logger = logging.getLogger(__name__)


class CacheManager:
    """SQLite-based cache for LSP responses"""
    
    def __init__(self, config: Config):
        self.config = config
        self.db_path = config.cache_db_path if config.cache_enabled else None
        self.ttl = 86400  # 24 hours in seconds
        self.conn: Optional[aiosqlite.Connection] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        
    async def initialize(self):
        """Initialize cache database"""
        
        if not self.config.cache_enabled or not self.db_path:
            logger.info("Cache disabled")
            return
            
        logger.info(f"Initializing cache at {self.db_path}")
        
        try:
            # Ensure cache directory exists
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Connect to database
            self.conn = await aiosqlite.connect(self.db_path)
            self.conn.row_factory = aiosqlite.Row
            
            # Create tables
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    result TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    timestamp INTEGER NOT NULL
                )
            """)
            
            # Create indexes
            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON cache(timestamp)
            """)
            
            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_file_hash ON cache(file_hash)
            """)
            
            await self.conn.commit()
            
            # Start cleanup task
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            
            logger.info("Cache initialized successfully")
            
        except Exception as e:
            log_error_with_context(logger, e, {"operation": "cache_init"})
            # Disable cache on error
            self.conn = None
            
    async def close(self):
        """Close cache connection"""
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            
        if self.conn:
            await self.conn.close()
            self.conn = None
            
        logger.info("Cache closed")
        
    def make_key(self, method: str, params: dict) -> str:
        """Generate cache key
        
        Args:
            method: Tool/method name
            params: Parameters dict
            
        Returns:
            Cache key string
        """
        
        file_path = Path(params["file"])
        file_hash = self._get_file_hash(file_path)
        
        # Create key from method, file hash, and position
        key_parts = [
            method,
            file_hash,
            str(params["line"]),
            str(params["column"])
        ]
        
        # Add other relevant parameters
        for key in sorted(params.keys()):
            if key not in ["file", "line", "column"]:
                key_parts.append(f"{key}:{params[key]}")
                
        return ":".join(key_parts)
        
    def _get_file_hash(self, file_path: Path) -> str:
        """Get file hash for cache invalidation
        
        Uses mtime and size for quick hash generation
        
        Args:
            file_path: Path to file
            
        Returns:
            Hash string
        """
        
        try:
            stat = file_path.stat()
            hash_input = f"{stat.st_mtime}:{stat.st_size}:{file_path}"
            return hashlib.md5(hash_input.encode()).hexdigest()[:12]
        except (OSError, IOError):
            # File doesn't exist or can't be accessed
            return "missing"
            
    async def get(self, key: str) -> Optional[Any]:
        """Get cached result
        
        Args:
            key: Cache key
            
        Returns:
            Cached result or None if not found/expired
        """
        
        if not self.conn:
            return None
            
        try:
            # Check for unexpired entry
            cutoff_time = int(time.time()) - self.ttl
            
            async with self.conn.execute(
                "SELECT result FROM cache WHERE key = ? AND timestamp > ?",
                (key, cutoff_time)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    result = json.loads(row["result"])
                    logger.debug(f"Cache hit for key: {key}")
                    return result
                    
        except Exception as e:
            log_error_with_context(logger, e, {"operation": "cache_get", "key": key})
            
        return None
        
    async def set(self, key: str, result: Any):
        """Set cached result
        
        Args:
            key: Cache key
            result: Result to cache
        """
        
        if not self.conn:
            return
            
        try:
            # Extract file hash from key for efficient invalidation
            parts = key.split(":")
            file_hash = parts[1] if len(parts) > 1 else "unknown"
            
            # Store result
            await self.conn.execute(
                """INSERT OR REPLACE INTO cache 
                   (key, result, file_hash, timestamp) 
                   VALUES (?, ?, ?, ?)""",
                (key, json.dumps(result, default=str), file_hash, int(time.time()))
            )
            await self.conn.commit()
            
            logger.debug(f"Cached result for key: {key}")
            
        except Exception as e:
            log_error_with_context(logger, e, {"operation": "cache_set", "key": key})
            
    async def invalidate_file(self, file_path: Path):
        """Invalidate all cache entries for a file
        
        Args:
            file_path: Path to file that changed
        """
        
        if not self.conn:
            return
            
        try:
            file_hash = self._get_file_hash(file_path)
            
            result = await self.conn.execute(
                "DELETE FROM cache WHERE file_hash = ?",
                (file_hash,)
            )
            
            await self.conn.commit()
            
            if result.rowcount > 0:
                logger.info(f"Invalidated {result.rowcount} cache entries for {file_path}")
                
        except Exception as e:
            log_error_with_context(logger, e, {
                "operation": "cache_invalidate",
                "file": str(file_path)
            })
            
    async def get_stats(self) -> dict:
        """Get cache statistics
        
        Returns:
            Dictionary with cache stats
        """
        
        if not self.conn:
            return {"enabled": False}
            
        try:
            # Get total entries
            async with self.conn.execute("SELECT COUNT(*) as count FROM cache") as cursor:
                row = await cursor.fetchone()
                total_entries = row["count"] if row else 0
                
            # Get expired entries
            cutoff_time = int(time.time()) - self.ttl
            async with self.conn.execute(
                "SELECT COUNT(*) as count FROM cache WHERE timestamp <= ?",
                (cutoff_time,)
            ) as cursor:
                row = await cursor.fetchone()
                expired_entries = row["count"] if row else 0
                
            # Get database size
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            
            return {
                "enabled": True,
                "total_entries": total_entries,
                "expired_entries": expired_entries,
                "valid_entries": total_entries - expired_entries,
                "db_size_bytes": db_size,
                "db_path": str(self.db_path),
                "ttl_hours": self.ttl / 3600
            }
            
        except Exception as e:
            log_error_with_context(logger, e, {"operation": "cache_stats"})
            return {"enabled": True, "error": str(e)}
            
    async def _periodic_cleanup(self):
        """Periodic cleanup of expired entries"""
        
        while True:
            try:
                await asyncio.sleep(3600)  # Run every hour
                
                if not self.conn:
                    continue
                    
                # Delete expired entries
                cutoff_time = int(time.time()) - self.ttl
                result = await self.conn.execute(
                    "DELETE FROM cache WHERE timestamp < ?",
                    (cutoff_time,)
                )
                
                if result.rowcount > 0:
                    logger.info(f"Cleaned up {result.rowcount} expired cache entries")
                    
                await self.conn.commit()
                
                # Vacuum database periodically (every 24 hours worth of cleanups)
                import random
                if random.randint(1, 24) == 1:
                    logger.info("Running database vacuum...")
                    await self.conn.execute("VACUUM")
                    logger.info("Database vacuum completed")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error_with_context(logger, e, {"operation": "cache_cleanup"})
                await asyncio.sleep(300)  # Wait 5 minutes before retrying