"""
Utility functions for mcp-clangd server
"""

import logging
import json
import sys
from datetime import datetime
from typing import Any, Dict
from pathlib import Path
import urllib.parse


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging"""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON"""
        
        log_obj = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_obj['exception'] = self.formatException(record.exc_info)
            
        # Add extra fields if present
        if hasattr(record, 'extra'):
            log_obj.update(record.extra)
            
        return json.dumps(log_obj, default=str)


def setup_logging(level: str = "info", use_json: bool = False):
    """Setup logging configuration
    
    Args:
        level: Log level (debug, info, warning, error)
        use_json: Whether to use JSON formatting
    """
    
    log_level = getattr(logging, level.upper())
    
    # Remove existing handlers
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Create console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)
    
    if use_json:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    console_handler.setFormatter(formatter)
    
    # Configure root logger
    root_logger.setLevel(log_level)
    root_logger.addHandler(console_handler)
    
    # Silence noisy libraries
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    
    # Log startup message
    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured at {level.upper()} level")


def log_performance(operation: str, duration_ms: float, **kwargs):
    """Log performance metrics
    
    Args:
        operation: Name of the operation
        duration_ms: Duration in milliseconds
        **kwargs: Additional context
    """
    
    logger = logging.getLogger('performance')
    extra_data = {
        'operation': operation,
        'duration_ms': duration_ms,
        'performance_log': True,
        **kwargs
    }
    
    logger.info(f"{operation} completed in {duration_ms:.2f}ms", extra=extra_data)


def log_error_with_context(logger: logging.Logger, error: Exception, context: Dict[str, Any]):
    """Log error with additional context
    
    Args:
        logger: Logger instance
        error: Exception that occurred
        context: Additional context information
    """
    
    extra_data = {
        'error_type': type(error).__name__,
        'error_context': context,
        'error_log': True
    }
    
    logger.error(f"Error occurred: {error}", exc_info=True, extra=extra_data)


class PerformanceTimer:
    """Context manager for timing operations"""
    
    def __init__(self, operation: str, logger: logging.Logger = None, **context):
        self.operation = operation
        self.logger = logger or logging.getLogger('performance')
        self.context = context
        self.start_time = None
        
    def __enter__(self):
        import time
        self.start_time = time.perf_counter()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        import time
        duration_ms = (time.perf_counter() - self.start_time) * 1000
        
        if exc_type is None:
            log_performance(self.operation, duration_ms, **self.context)
        else:
            self.logger.error(
                f"{self.operation} failed after {duration_ms:.2f}ms",
                extra={'operation': self.operation, 'duration_ms': duration_ms, **self.context}
            )


def path_to_uri(path: str) -> str:
    """Convert a file path to a URI
    
    Args:
        path: File path to convert
        
    Returns:
        URI string
    """
    # Convert to absolute path
    abs_path = Path(path).resolve()
    
    # Convert to URI format
    uri = abs_path.as_uri()
    
    return uri


def uri_to_path(uri: str) -> str:
    """Convert a URI to a file path
    
    Args:
        uri: URI string to convert
        
    Returns:
        File path string
    """
    # Parse URI and convert to path
    parsed = urllib.parse.urlparse(uri)
    
    if parsed.scheme == 'file':
        # Decode URL-encoded characters
        path = urllib.parse.unquote(parsed.path)
        return path
    else:
        # Not a file URI, return as-is
        return uri


def project_socket_path(project_root: Path) -> str:
    """
    Generates a deterministic, project-specific Unix socket path in a secure,
    temporary directory.
    """
    import hashlib
    import tempfile
    # Use a hash of the resolved absolute path for consistency.
    digest = hashlib.sha1(str(project_root.resolve()).encode()).hexdigest()[:12]
    socket_dir = Path(tempfile.gettempdir()) / "clangaroo-sockets"
    # Ensure the directory exists with secure permissions (owner access only).
    socket_dir.mkdir(mode=0o700, exist_ok=True)
    return str(socket_dir / f"clangaroo-{digest}.sock")


def is_socket_active(socket_path: str) -> bool:
    """
    Checks if a process is actively listening on the given Unix socket path.
    Returns True if active, False otherwise.
    """
    import os
    import stat
    import socket
    
    if not os.path.exists(socket_path):
        return False

    try:
        if not stat.S_ISSOCK(os.stat(socket_path).st_mode):
            logger.warning(f"Path exists but is not a socket: {socket_path}. Will treat as inactive.")
            return False
    except FileNotFoundError:
        return False

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # Set a short timeout to avoid blocking if the daemon is hung.
        s.settimeout(0.1)
        s.connect(socket_path)
        s.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        # This indicates a stale socket file where no process is listening.
        return False
    except (PermissionError, socket.timeout):
        # A timeout or permission error likely means the daemon is active but busy.
        return True
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Unexpected error while checking socket {socket_path}: {e}")
        return False # Err on the side of caution.


def cleanup_stale_socket(socket_path: str):
    """
    Removes a socket file if and only if it is determined to be stale (i.e.,
    no process is listening on it).
    """
    import os
    
    if os.path.exists(socket_path) and not is_socket_active(socket_path):
        try:
            os.unlink(socket_path)
            logger = logging.getLogger(__name__)
            logger.info(f"Removed stale socket file: {socket_path}")
        except OSError as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Error removing stale socket {socket_path}: {e}")