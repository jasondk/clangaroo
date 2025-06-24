"""
Enhanced AI summary cache with context-aware invalidation
"""

import aiosqlite
import hashlib
import time
import logging
from pathlib import Path
from typing import Optional

from .llm_provider import SummaryResponse, ContextData

logger = logging.getLogger(__name__)


class EnhancedAISummaryCache:
    """AI summary cache with context-aware invalidation"""
    
    def __init__(self, db_path: str, config):
        self.db_path = db_path
        self.config = config
        self.ttl = 7 * 24 * 3600  # 7 days in seconds
    
    async def initialize(self):
        """Initialize cache database"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ai_summaries (
                    cache_key TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    symbol_name TEXT NOT NULL,
                    symbol_kind TEXT NOT NULL,
                    context_level TEXT NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    accessed_at INTEGER NOT NULL
                )
            """)
            
            # Index for cleanup
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_accessed_at 
                ON ai_summaries(accessed_at)
            """)
            
            # Index for context level queries
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_context_level 
                ON ai_summaries(context_level)
            """)
            
            # Create call analysis cache table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS call_analysis_cache (
                    cache_key TEXT PRIMARY KEY,
                    analysis_summary TEXT NOT NULL,
                    patterns TEXT NOT NULL,
                    architectural_insights TEXT NOT NULL,
                    data_flow_analysis TEXT NOT NULL,
                    performance_notes TEXT NOT NULL,
                    target_function TEXT NOT NULL,
                    analysis_type TEXT NOT NULL,
                    analysis_level TEXT NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    accessed_at INTEGER NOT NULL
                )
            """)
            
            # Index for call analysis cleanup
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_call_accessed_at 
                ON call_analysis_cache(accessed_at)
            """)
            
            await db.commit()
    
    def _generate_cache_key(self, file: str, line: int, column: int, 
                           context_level: str, context_data: ContextData) -> str:
        """Generate cache key that includes context level and content state"""
        
        if context_level == "minimal":
            # Use existing file_hash (clangd index based)
            file_hash = self._get_file_hash(Path(file))
            return f"ai_summary:minimal:{file_hash}:{line}:{column}"
            
        elif context_level == "local":
            # Hash the surrounding code area
            local_hash = self._get_local_content_hash(file, line, column)
            return f"ai_summary:local:{local_hash}:{line}:{column}"
            
        elif context_level == "full":
            # Hash entire file + dependencies
            full_hash = self._get_full_context_hash(file, context_data)
            return f"ai_summary:full:{full_hash}:{line}:{column}"
        else:
            # Fallback to minimal
            file_hash = self._get_file_hash(Path(file))
            return f"ai_summary:unknown:{file_hash}:{line}:{column}"
    
    def _get_file_hash(self, file_path: Path) -> str:
        """Get file hash for cache invalidation (uses mtime and size)"""
        try:
            if not file_path.is_absolute():
                file_path = self.config.project_root / file_path
            stat = file_path.stat()
            hash_input = f"{stat.st_mtime}:{stat.st_size}:{file_path}"
            return hashlib.md5(hash_input.encode()).hexdigest()[:12]
        except (OSError, IOError):
            return "missing"
    
    def _get_local_content_hash(self, file: str, line: int, column: int) -> str:
        """Hash content around the symbol for local context invalidation"""
        try:
            file_path = self.config.project_root / file
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            
            # Extract ±25 lines around the target
            start = max(0, line - 25)
            end = min(len(lines), line + 25)
            context_lines = lines[start:end]
            
            # Include file mtime for invalidation
            mtime = file_path.stat().st_mtime
            hash_input = f"{mtime}:{''.join(context_lines)}"
            return hashlib.md5(hash_input.encode()).hexdigest()[:12]
            
        except Exception:
            return "error"
    
    def _get_full_context_hash(self, file: str, context_data: ContextData) -> str:
        """Hash entire file and dependencies for full context invalidation"""
        try:
            file_path = self.config.project_root / file
            
            # Main file hash
            main_stat = file_path.stat()
            main_hash = f"{main_stat.st_mtime}:{main_stat.st_size}"
            
            # Include header dependencies
            dep_hashes = []
            if context_data.related_headers:
                for header_content in context_data.related_headers[:5]:
                    # Hash header content (already includes file info)
                    dep_hash = hashlib.md5(header_content.encode()).hexdigest()[:8]
                    dep_hashes.append(dep_hash)
            
            hash_input = f"{main_hash}:{'|'.join(dep_hashes)}"
            return hashlib.md5(hash_input.encode()).hexdigest()[:12]
            
        except Exception:
            return "error"
    
    async def get_by_key(self, cache_key: str) -> Optional[SummaryResponse]:
        """Get cached summary by cache key"""
        current_time = int(time.time())
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT summary, tokens_used, provider, context_level, created_at
                FROM ai_summaries 
                WHERE cache_key = ? AND created_at > ?
            """, (cache_key, current_time - self.ttl))
            
            row = await cursor.fetchone()
            if row:
                # Update access time
                await db.execute("""
                    UPDATE ai_summaries 
                    SET accessed_at = ? 
                    WHERE cache_key = ?
                """, (current_time, cache_key))
                await db.commit()
                
                return SummaryResponse(
                    summary=row[0],
                    tokens_used=row[1],
                    provider=row[2],
                    cached=True,
                    context_level=row[3]
                )
        return None
    
    async def store_with_key(self, cache_key: str, response: SummaryResponse, 
                            context_data: ContextData):
        """Store summary with cache key"""
        current_time = int(time.time())
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO ai_summaries 
                (cache_key, summary, symbol_name, symbol_kind, context_level, 
                 tokens_used, provider, created_at, accessed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (cache_key, response.summary, context_data.symbol_name, 
                 context_data.symbol_kind, context_data.context_level,
                 response.tokens_used, response.provider, current_time, current_time))
            await db.commit()
    
    async def get(self, content: str, symbol_name: str, context_level: str = "minimal") -> Optional[SummaryResponse]:
        """Get cached summary (legacy method for backward compatibility)"""
        content_hash = self._hash_content(content, symbol_name, context_level)
        current_time = int(time.time())
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT summary, tokens_used, provider, context_level, created_at
                FROM ai_summaries 
                WHERE cache_key = ? AND created_at > ?
            """, (content_hash, current_time - self.ttl))
            
            row = await cursor.fetchone()
            if row:
                # Update access time
                await db.execute("""
                    UPDATE ai_summaries 
                    SET accessed_at = ? 
                    WHERE cache_key = ?
                """, (current_time, content_hash))
                await db.commit()
                
                return SummaryResponse(
                    summary=row[0],
                    tokens_used=row[1],
                    provider=row[2],
                    cached=True,
                    context_level=row[3]
                )
        return None
    
    async def store(self, content: str, symbol_name: str, symbol_kind: str, 
                   response: SummaryResponse):
        """Store summary (legacy method for backward compatibility)"""
        content_hash = self._hash_content(content, symbol_name, response.context_level)
        current_time = int(time.time())
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO ai_summaries 
                (cache_key, summary, symbol_name, symbol_kind, context_level,
                 tokens_used, provider, created_at, accessed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (content_hash, response.summary, symbol_name, symbol_kind,
                 response.context_level, response.tokens_used, response.provider, 
                 current_time, current_time))
            await db.commit()
    
    def _hash_content(self, content: str, symbol_name: str, context_level: str = "minimal") -> str:
        """Create hash key for content + symbol + context level"""
        key = f"{context_level}:{symbol_name}:{content}"
        return f"ai_summary:legacy:{hashlib.sha256(key.encode()).hexdigest()[:16]}"
    
    async def cleanup_expired(self):
        """Clean up expired entries"""
        cutoff_time = int(time.time()) - self.ttl
        
        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute("""
                DELETE FROM ai_summaries WHERE created_at < ?
            """, (cutoff_time,))
            
            if result.rowcount > 0:
                logger.info(f"Cleaned up {result.rowcount} expired AI summaries")
            
            await db.commit()
    
    async def get_stats(self) -> dict:
        """Get cache statistics"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Total entries
                cursor = await db.execute("SELECT COUNT(*) FROM ai_summaries")
                total = (await cursor.fetchone())[0]
                
                # Entries by context level
                cursor = await db.execute("""
                    SELECT context_level, COUNT(*) 
                    FROM ai_summaries 
                    GROUP BY context_level
                """)
                by_level = dict(await cursor.fetchall())
                
                # Expired entries
                cutoff_time = int(time.time()) - self.ttl
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM ai_summaries WHERE created_at < ?
                """, (cutoff_time,))
                expired = (await cursor.fetchone())[0]
                
                return {
                    "total_entries": total,
                    "valid_entries": total - expired,
                    "expired_entries": expired,
                    "by_context_level": by_level,
                    "ttl_days": self.ttl / (24 * 3600)
                }
        except Exception as e:
            logger.error(f"Error getting AI cache stats: {e}")
            return {"error": str(e)}
    
    async def get_call_analysis(self, function_name: str, file: str, line: int, column: int,
                               analysis_type: str, analysis_level: str, calls_hash: str) -> Optional['CallAnalysisResponse']:
        """Get cached call analysis result
        
        Args:
            function_name: Target function name
            file: File path
            line: Line number  
            column: Column number
            analysis_type: "incoming" or "outgoing"
            analysis_level: "summary" or "detailed"
            calls_hash: Hash of the call hierarchy data
            
        Returns:
            CallAnalysisResponse if cached and not expired, None otherwise
        """
        cache_key = self._generate_call_cache_key(function_name, file, line, column, 
                                                 analysis_type, analysis_level, calls_hash)
        current_time = int(time.time())
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT analysis_summary, patterns, architectural_insights, 
                       data_flow_analysis, performance_notes, tokens_used, provider
                FROM call_analysis_cache 
                WHERE cache_key = ? AND created_at > ?
            """, (cache_key, current_time - self.ttl))
            
            row = await cursor.fetchone()
            if row:
                # Update access time
                await db.execute("""
                    UPDATE call_analysis_cache 
                    SET accessed_at = ? 
                    WHERE cache_key = ?
                """, (current_time, cache_key))
                await db.commit()
                
                # Import and create response
                from .llm_provider import CallAnalysisResponse, CallPattern
                import json
                
                # Parse patterns from JSON
                patterns = []
                try:
                    patterns_data = json.loads(row[1])
                    for pattern_data in patterns_data:
                        pattern = CallPattern(
                            pattern_type=pattern_data.get("pattern_type", "unknown"),
                            calls=[],
                            description=pattern_data.get("description", ""),
                            confidence=pattern_data.get("confidence", 0.5)
                        )
                        patterns.append(pattern)
                except json.JSONDecodeError:
                    patterns = []
                
                return CallAnalysisResponse(
                    analysis_summary=row[0],
                    patterns=patterns,
                    architectural_insights=row[2],
                    data_flow_analysis=row[3],
                    performance_notes=row[4],
                    tokens_used=row[5],
                    provider=row[6],
                    cached=True
                )
        return None
    
    async def store_call_analysis(self, function_name: str, file: str, line: int, column: int,
                                 analysis_type: str, analysis_level: str, calls_hash: str,
                                 response: 'CallAnalysisResponse'):
        """Store call analysis result in cache
        
        Args:
            function_name: Target function name
            file: File path
            line: Line number
            column: Column number
            analysis_type: "incoming" or "outgoing" 
            analysis_level: "summary" or "detailed"
            calls_hash: Hash of the call hierarchy data
            response: CallAnalysisResponse to cache
        """
        cache_key = self._generate_call_cache_key(function_name, file, line, column,
                                                 analysis_type, analysis_level, calls_hash)
        current_time = int(time.time())
        
        # Serialize patterns to JSON
        import json
        patterns_json = json.dumps([
            {
                "pattern_type": pattern.pattern_type,
                "description": pattern.description,
                "confidence": pattern.confidence
            } for pattern in response.patterns
        ])
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO call_analysis_cache 
                (cache_key, analysis_summary, patterns, architectural_insights,
                 data_flow_analysis, performance_notes, target_function, 
                 analysis_type, analysis_level, tokens_used, provider, created_at, accessed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (cache_key, response.analysis_summary, patterns_json, 
                 response.architectural_insights, response.data_flow_analysis,
                 response.performance_notes, function_name, analysis_type, 
                 analysis_level, response.tokens_used, response.provider,
                 current_time, current_time))
            await db.commit()
    
    def _generate_call_cache_key(self, function_name: str, file: str, line: int, column: int,
                                analysis_type: str, analysis_level: str, calls_hash: str) -> str:
        """Generate cache key for call analysis
        
        Args:
            function_name: Target function name
            file: File path
            line: Line number
            column: Column number
            analysis_type: "incoming" or "outgoing"
            analysis_level: "summary" or "detailed"
            calls_hash: Hash of the call hierarchy data
            
        Returns:
            Cache key string
        """
        # Include file modification time for invalidation
        try:
            file_path = self.config.project_root / file
            mtime = file_path.stat().st_mtime
        except:
            mtime = 0
        
        key_parts = [
            "call_analysis",
            analysis_type,
            analysis_level,
            function_name,
            str(mtime),
            calls_hash[:16]  # Truncate hash for readability
        ]
        
        return ":".join(key_parts)
    
    async def cleanup_expired_call_analysis(self):
        """Clean up expired call analysis entries"""
        cutoff_time = int(time.time()) - self.ttl
        
        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute("""
                DELETE FROM call_analysis_cache WHERE created_at < ?
            """, (cutoff_time,))
            
            if result.rowcount > 0:
                logger.info(f"Cleaned up {result.rowcount} expired call analysis entries")
            
            await db.commit()