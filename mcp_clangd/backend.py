# clangaroo/mcp_clangd/backend.py
import asyncio
import os
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Awaitable
import logging
import shutil
import tempfile

from .lsp_client import LSPClient
from .tree_sitter_manager import TreeSitterManager
from .lsp_methods import LSPMethods
from .index_warmup import IndexWarmup
from .cache import CacheManager
from .llm_provider import LLMProvider
from .ai_cache import EnhancedAISummaryCache

logger = logging.getLogger(__name__)

class Backend:
    """
    Manages all shared, long-lived resources for the daemon, including the
    clangd process, caches, and LSP communication.
    """
    def __init__(self, project_root: Path, config: Dict[str, Any]):
        self.project_root = project_root
        self.config = config
        self.lsp_client: Optional[LSPClient] = None
        self.lsp_methods: Optional[LSPMethods] = None
        self.tree_sitter = TreeSitterManager(project_root)
        self.ai_assistant: Optional[LLMProvider] = None
        self.ai_cache: Optional[EnhancedAISummaryCache] = None
        self.sqlite_cache: Optional[CacheManager] = None
        self.index_warmup: Optional[IndexWarmup] = None
        self.index_dir: Optional[Path] = None
        self.clangd_manager = None

        # Caching mechanism to prevent dogpiling (thundering herd) expensive computations.
        self._computation_cache: Dict[str, asyncio.Future] = {}

        # Synchronization primitives
        self._lsp_write_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        self._startup_complete = asyncio.Event()

    async def start(self) -> None:
        """Initializes all backend resources, including starting clangd."""
        try:
            self.index_dir = Path(tempfile.gettempdir()) / f"clangaroo-index-{os.getpid()}"
            self.index_dir.mkdir(exist_ok=True)

            await self._start_clangd()
            
            # Initialize LSP methods
            self.lsp_methods = LSPMethods(self.lsp_client)

            # Initialize caching if enabled
            if self.config.get('cache_enabled', True):
                from .config import Config
                from dataclasses import fields
                config_fields = {f.name for f in fields(Config)}
                filtered_config = {k: v for k, v in self.config.items() if k in config_fields}
                config_obj = Config(**filtered_config)
                self.sqlite_cache = CacheManager(config_obj)
                await self.sqlite_cache.initialize()

            # Initialize AI features if enabled
            if self.config.get('ai_enabled'):
                from .config import Config
                from dataclasses import fields
                config_fields = {f.name for f in fields(Config)}
                filtered_config = {k: v for k, v in self.config.items() if k in config_fields}
                config_obj = Config(**filtered_config)
                # Let lsp_methods.initialize_ai_features handle all AI component initialization
                # Including EnhancedAISummaryCache which needs both db_path and config
                await self.lsp_methods.initialize_ai_features(config_obj)

            # Initialize index warmup
            if self.config.get('warmup'):
                from .config import Config
                from dataclasses import fields
                config_fields = {f.name for f in fields(Config)}
                filtered_config = {k: v for k, v in self.config.items() if k in config_fields}
                config_obj = Config(**filtered_config)
                self.index_warmup = IndexWarmup(self.lsp_methods, config_obj)
                # Start warmup in background
                asyncio.create_task(self._warmup_index())

            # Signal that startup is complete and requests can be processed.
            self._startup_complete.set()
            logger.info(f"Backend for project {self.project_root} initialized successfully.")
        except Exception as e:
            logger.critical(f"Backend startup failed: {e}", exc_info=True)
            raise

    async def _warmup_index(self):
        """Perform index warmup in background"""
        try:
            if self.index_warmup:
                await self.index_warmup.warmup_project()
                logger.info("Index warmup completed")
        except Exception as e:
            logger.warning(f"Index warmup failed: {e}")

    async def _start_clangd(self) -> None:
        """Starts the clangd subprocess and initializes the LSP client."""
        from .clangd_manager import ClangdManager
        from .config import Config
        from dataclasses import fields
        
        # Filter config dict to only include fields that Config accepts
        config_fields = {f.name for f in fields(Config)}
        filtered_config = {k: v for k, v in self.config.items() if k in config_fields}
        
        # Create a proper Config object from the filtered dict
        config_obj = Config(**filtered_config)
        self.clangd_manager = ClangdManager(config_obj)
        
        # Pass index path via environment variable
        self.clangd_manager.env = os.environ.copy()
        self.clangd_manager.env['CLANGD_INDEX_STORAGE_PATH'] = str(self.index_dir)
        
        self.lsp_client = LSPClient(self.clangd_manager)
        await self.lsp_client.start()

        # Wait for indexing if requested
        if self.config.get('wait_for_index'):
            logger.info("Waiting for background indexing to complete...")
            indexing_completed = await self.lsp_client.wait_for_indexing(
                timeout=self.config.get('index_timeout', 300.0)
            )
            
            if not indexing_completed:
                logger.warning("Proceeding with partial index due to timeout")
            else:
                logger.info("Background indexing completed successfully")

        logger.info("Clangd started and initialized successfully")

    async def shutdown(self) -> None:
        """Gracefully shuts down all backend resources."""
        logger.info("Shutting down backend...")
        
        # Close all documents if document manager exists
        if self.lsp_methods and hasattr(self.lsp_methods, 'document_manager'):
            try:
                await self.lsp_methods.document_manager.close_all_documents()
            except Exception as e:
                logger.error(f"Error closing documents: {e}")
        
        # Stop LSP client
        if self.lsp_client:
            await self.lsp_client.stop()
        
        # Close SQLite cache
        if self.sqlite_cache:
            await self.sqlite_cache.close()
            
        # Clean up index directory
        if self.index_dir and self.index_dir.exists():
            shutil.rmtree(self.index_dir, ignore_errors=True)
            logger.info(f"Cleaned up index directory: {self.index_dir}")

    async def execute_lsp_request(self, method: str, params: Any) -> Any:
        """
        Executes an LSP request, ensuring serialization and that the backend is ready.
        """
        await self._startup_complete.wait()
        async with self._lsp_write_lock:
            return await self.lsp_client.request(method, params)

    async def get_or_compute(self, cache_key: str, compute_func: Callable[[], Awaitable[Any]]) -> Any:
        """
        Retrieves a result from a cache or computes it if not present.
        This implementation is safe from race conditions (thundering herd problem).
        """
        async with self._cache_lock:
            if cache_key in self._computation_cache:
                return await self._computation_cache[cache_key]

        # No future exists, so create one and add it to the cache.
        future = asyncio.get_running_loop().create_future()
        async with self._cache_lock:
            # Double-check in case another task created the future while we were unlocked.
            if cache_key in self._computation_cache:
                return await self._computation_cache[cache_key]
            self._computation_cache[cache_key] = future

        # Now, perform the computation and fulfill the future.
        try:
            result = await compute_func()
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
            # Remove the failed future from the cache so it can be retried later.
            async with self._cache_lock:
                del self._computation_cache[cache_key]
        
        return await future