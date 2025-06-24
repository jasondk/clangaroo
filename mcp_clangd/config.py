"""
Configuration management for mcp-clangd server
"""

import os
import shutil
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger(__name__)


@dataclass
class Config:
    """Configuration for the MCP clangd server"""
    
    project_root: Path
    clangd_path: str = "clangd"
    log_level: str = "info"
    cache_dir: Optional[Path] = None
    cache_enabled: bool = True
    lsp_timeout: float = 5.0
    clangd_args: Optional[List[str]] = None
    compile_db_path: Optional[Path] = None
    
    # Indexing enhancements
    index_path: Optional[Path] = None          # External index location
    warmup: bool = False                       # Enable index warmup
    wait_for_index: bool = False              # Wait for indexing to complete
    index_timeout: float = 300.0              # Max wait time for indexing (5 min)
    warmup_file_limit: int = 20               # Max files to open during warmup
    
    # AI Summarization Options
    ai_enabled: bool = False
    ai_provider: str = "gemini-2.5-flash"     # "gemini-2.5-flash", "gemini-2.5-flash-lite"
    ai_api_key: Optional[str] = None
    ai_cache_days: int = 7
    ai_max_tokens: int = 150
    ai_min_content_length: int = 100
    ai_cost_limit_monthly: float = 10.0       # USD per month
    ai_analysis_level: str = "summary"        # "summary", "detailed"
    ai_context_level: str = "local"           # "minimal", "local", "full"
    
    # Call Hierarchy Options
    call_hierarchy_max_depth: int = 3         # Maximum depth for recursive call hierarchy
    call_hierarchy_max_calls: int = 100       # Maximum total calls to return
    call_hierarchy_max_per_level: int = 25    # Maximum calls per level
    
    def __post_init__(self):
        """Validate and normalize configuration"""
        
        # Resolve and validate project root
        self.project_root = Path(self.project_root).resolve()
        if not self.project_root.exists():
            raise ValueError(f"Project root does not exist: {self.project_root}")
        if not self.project_root.is_dir():
            raise ValueError(f"Project root is not a directory: {self.project_root}")
            
        # Find and validate compile_commands.json
        self._find_compile_commands()
        
        # Validate clangd executable
        self._validate_clangd()
        
        # Set up cache directory
        if self.cache_dir is None:
            self.cache_dir = Path.home() / ".cache" / "mcp-clangd"
        else:
            self.cache_dir = Path(self.cache_dir)
            
        # Create cache directory if it doesn't exist
        if self.cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            
        # Set up clangd arguments
        if self.clangd_args is None:
            self.clangd_args = self._default_clangd_args()
            
        # Validate indexing options
        self._validate_indexing_options()
        
        # Validate AI options
        self._validate_ai_options()
        
        # Validate call hierarchy options
        self._validate_call_hierarchy_options()
            
    def _find_compile_commands(self):
        """Find compile_commands.json in project"""
        
        # Check project root first
        compile_db = self.project_root / "compile_commands.json"
        if compile_db.exists():
            self.compile_db_path = compile_db
            return
            
        # Check build directory
        build_dirs = ["build", "Build", "cmake-build-debug", "cmake-build-release"]
        for build_dir in build_dirs:
            build_path = self.project_root / build_dir
            if build_path.exists() and build_path.is_dir():
                compile_db = build_path / "compile_commands.json"
                if compile_db.exists():
                    self.compile_db_path = compile_db
                    return
                    
        # Check subdirectories for common build outputs
        for item in self.project_root.iterdir():
            if item.is_dir() and item.name.startswith("build"):
                compile_db = item / "compile_commands.json"
                if compile_db.exists():
                    self.compile_db_path = compile_db
                    return
                    
        raise ValueError(
            f"No compile_commands.json found in {self.project_root} or build directories. "
            "Please generate one using your build system (e.g., cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON)"
        )
        
    def _validate_clangd(self):
        """Validate that clangd is available and get version"""
        
        # Check if clangd exists
        clangd_path = shutil.which(self.clangd_path)
        if not clangd_path:
            raise ValueError(f"clangd executable not found: {self.clangd_path}")
            
        # Update path to full path
        self.clangd_path = clangd_path
        
        # TODO: Check clangd version (should be 16+)
        # For now we'll assume it's compatible
        
    def _validate_indexing_options(self):
        """Validate indexing configuration options"""
        
        # Validate index path if provided
        if self.index_path:
            self.index_path = Path(self.index_path).resolve()
            if not self.index_path.exists():
                logger.warning(f"Index path does not exist, will be created: {self.index_path}")
                self.index_path.mkdir(parents=True, exist_ok=True)
            elif not self.index_path.is_dir():
                raise ValueError(f"Index path must be a directory: {self.index_path}")
                
        # Validate timeout values
        if self.index_timeout <= 0:
            raise ValueError("index_timeout must be positive")
            
        if self.warmup_file_limit <= 0:
            raise ValueError("warmup_file_limit must be positive")
            
        # Log indexing configuration
        if self.index_path or self.warmup or self.wait_for_index:
            logger.info("Indexing enhancements enabled:")
            if self.index_path:
                logger.info(f"  - External index: {self.index_path}")
            if self.warmup:
                logger.info(f"  - Index warmup: {self.warmup_file_limit} files")
            if self.wait_for_index:
                logger.info(f"  - Wait for indexing: {self.index_timeout}s timeout")
    
    def _validate_ai_options(self):
        """Validate AI configuration options"""
        
        if not self.ai_enabled:
            return
            
        # Validate AI provider
        valid_providers = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
        if self.ai_provider not in valid_providers:
            raise ValueError(f"Invalid AI provider: {self.ai_provider}. Must be one of {valid_providers}")
        
        # Check for API key
        if not self.ai_api_key:
            # Try to get from environment
            import os
            self.ai_api_key = os.getenv("CLANGAROO_AI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            
            if not self.ai_api_key:
                logger.warning("AI enabled but no API key provided. Set CLANGAROO_AI_API_KEY or GOOGLE_API_KEY environment variable.")
                self.ai_enabled = False
                return
        
        # Validate numeric options
        if self.ai_cache_days <= 0:
            raise ValueError("ai_cache_days must be positive")
        if self.ai_max_tokens <= 0:
            raise ValueError("ai_max_tokens must be positive")
        if self.ai_min_content_length <= 0:
            raise ValueError("ai_min_content_length must be positive")
        if self.ai_cost_limit_monthly < 0:
            raise ValueError("ai_cost_limit_monthly must be non-negative")
        
        # Validate AI analysis levels
        valid_analysis_levels = ["summary", "detailed"]
        if self.ai_analysis_level not in valid_analysis_levels:
            raise ValueError(f"Invalid ai_analysis_level: {self.ai_analysis_level}. Must be one of {valid_analysis_levels}")
        
        valid_context_levels = ["minimal", "local", "full"]
        if self.ai_context_level not in valid_context_levels:
            raise ValueError(f"Invalid ai_context_level: {self.ai_context_level}. Must be one of {valid_context_levels}")
        
        # Log AI configuration
        logger.info("AI features enabled:")
        logger.info(f"  - Provider: {self.ai_provider}")
        logger.info(f"  - Cache TTL: {self.ai_cache_days} days")
        logger.info(f"  - Max tokens: {self.ai_max_tokens}")
        logger.info(f"  - Monthly limit: ${self.ai_cost_limit_monthly}")
        logger.info(f"  - Default analysis level: {self.ai_analysis_level}")
        logger.info(f"  - Default context level: {self.ai_context_level}")
        logger.info(f"  - API key: {'configured' if self.ai_api_key else 'missing'}")
    
    def _validate_call_hierarchy_options(self):
        """Validate call hierarchy configuration options"""
        
        if self.call_hierarchy_max_depth <= 0:
            raise ValueError("call_hierarchy_max_depth must be positive")
        if self.call_hierarchy_max_depth > 10:
            logger.warning(f"call_hierarchy_max_depth of {self.call_hierarchy_max_depth} may cause performance issues")
            
        if self.call_hierarchy_max_calls <= 0:
            raise ValueError("call_hierarchy_max_calls must be positive")
        if self.call_hierarchy_max_calls > 500:
            logger.warning(f"call_hierarchy_max_calls of {self.call_hierarchy_max_calls} may cause performance issues")
            
        if self.call_hierarchy_max_per_level <= 0:
            raise ValueError("call_hierarchy_max_per_level must be positive")
            
        logger.debug(f"Call hierarchy settings: depth={self.call_hierarchy_max_depth}, "
                    f"max_calls={self.call_hierarchy_max_calls}, per_level={self.call_hierarchy_max_per_level}")
        
    def _default_clangd_args(self) -> List[str]:
        """Get default clangd arguments optimized for MCP usage"""
        
        args = [
            "--header-insertion=never",     # Don't modify code
            "--clang-tidy=false",          # Skip linting for speed
            "--completion-style=detailed",  # Rich hover info
            "--pch-storage=memory",        # Faster but more RAM
            "--log=error",                 # Reduce stderr noise
        ]
        
        # Configure indexing based on options
        if self.index_path:
            # Set environment to control clangd index location
            import os
            os.environ["XDG_CACHE_HOME"] = str(self.index_path.parent)
            args.append("--background-index")
            logger.info(f"Using external index directory: {self.index_path.parent}/clangd/index/")
        else:
            # Use background indexing (default)
            args.append("--background-index")
        
        # Add compile commands directory
        compile_db_dir = self.compile_db_path.parent
        args.append(f"--compile-commands-dir={compile_db_dir}")
        
        return args
        
    @property
    def cache_db_path(self) -> Path:
        """Get path to cache database"""
        return self.cache_dir / "cache.db"
    
    @property
    def ai_cache_db_path(self) -> Path:
        """Get path to AI cache database"""
        return self.cache_dir / "ai_cache.db"
        
    def to_dict(self) -> dict:
        """Convert config to dictionary for logging"""
        return {
            "project_root": str(self.project_root),
            "clangd_path": self.clangd_path,
            "log_level": self.log_level,
            "cache_enabled": self.cache_enabled,
            "cache_dir": str(self.cache_dir) if self.cache_dir else None,
            "compile_db_path": str(self.compile_db_path) if self.compile_db_path else None,
            "lsp_timeout": self.lsp_timeout,
            # Indexing options
            "index_path": str(self.index_path) if self.index_path else None,
            "warmup": self.warmup,
            "wait_for_index": self.wait_for_index,
            "index_timeout": self.index_timeout,
            "warmup_file_limit": self.warmup_file_limit,
            # AI options
            "ai_enabled": self.ai_enabled,
            "ai_provider": self.ai_provider,
            "ai_api_key": "***" if self.ai_api_key else None,  # Hide API key in logs
            "ai_cache_days": self.ai_cache_days,
            "ai_max_tokens": self.ai_max_tokens,
            "ai_cost_limit_monthly": self.ai_cost_limit_monthly,
            "ai_analysis_level": self.ai_analysis_level,
            "ai_context_level": self.ai_context_level,
        }