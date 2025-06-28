# clangaroo/mcp_clangd/__main__.py
import argparse
import asyncio
import os
import sys
import logging
import fcntl
from pathlib import Path

from .daemon import ClangarooDaemon
from .proxy import StdioProxy
from .utils import project_socket_path, is_socket_active

def run_daemon_entrypoint(project_root: Path, config: dict):
    """
    Entry point for starting the daemon. Uses a file lock to ensure only one
    process can attempt to start the daemon for a given project at a time.
    """
    
    socket_path = project_socket_path(project_root)
    lock_path = socket_path + ".lock"
    
    # Ensure socket directory exists
    socket_dir = os.path.dirname(socket_path)
    os.makedirs(socket_dir, mode=0o700, exist_ok=True)
    
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY)

    try:
        # Acquire an exclusive, non-blocking lock on the lock file.
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError) as e:
        logger.info("Another process is starting the daemon. Waiting...")
        # Simple wait; the proxy will handle retries if this fails.
        import time
        time.sleep(5) 
        if not is_socket_active(socket_path):
            logger.error("Waited for daemon, but it did not become active.")
            sys.exit(1)
        return
    
    try:
        # We have the lock. Double-check that the daemon isn't already running.
        if is_socket_active(socket_path):
            logger.info("Daemon is already active.")
            return

        # Fork to run the daemon in the background.
        if not config.get('debug', False):
            pid = os.fork()
            if pid > 0:
                # Parent process - wait a bit to ensure child starts
                import time
                time.sleep(0.5)
                sys.exit(0)
            else:
                # Child process - create new session and detach
                os.setsid()
                # Redirect file descriptors before closing
                devnull_in = os.open(os.devnull, os.O_RDONLY)
                devnull_out = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull_in, 0)  # stdin
                os.dup2(devnull_out, 1)  # stdout  
                os.dup2(devnull_out, 2)  # stderr
                os.close(devnull_in)
                os.close(devnull_out)
                
                # Re-initialize logging to use the new stderr
                from .utils import setup_logging
                setup_logging(config.get('log_level', 'info'))
        
        # Now run the daemon with a fresh event loop
        try:
            # Write startup log
            import datetime
            with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
                f.write(f"\n=== {datetime.datetime.now()} ===\n")
                f.write(f"Starting daemon for: {project_root}\n")
                f.write(f"Socket path: {socket_path}\n")
                f.flush()
                
            # Check if project has compile_commands.json before starting
            from .config import Config
            try:
                # This will validate the project has compile_commands.json
                test_config = Config(**{k: v for k, v in config.items() if k != 'debug'})
                with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
                    f.write(f"Config validated successfully\n")
                    f.write(f"Compile DB: {test_config.compile_db_path}\n")
            except Exception as e:
                with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
                    f.write(f"Config validation error: {e}\n")
                raise
                
            daemon = ClangarooDaemon(project_root, config)
            
            with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
                f.write(f"Starting daemon.start()...\n")
                
            asyncio.run(daemon.start())
        except Exception as e:
            # Log any startup errors
            with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
                f.write(f"ERROR: {type(e).__name__}: {e}\n")
                import traceback
                f.write(traceback.format_exc())
                f.flush()
            raise

    finally:
        # This block ensures the lock is always released.
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        if os.path.exists(lock_path):
            os.unlink(lock_path)

def main():
    parser = argparse.ArgumentParser(description="Clangaroo: A multi-client C++ MCP server.")
    parser.add_argument('--project', type=Path, required=True, help='Path to the C++ project root.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--daemon', action='store_true', help='Run in background daemon mode.')
    group.add_argument('--proxy', action='store_true', default=True, help='Run as a stdio proxy to the daemon (default).')
    
    # Add other arguments (log_level, ai_enabled, etc.)
    parser.add_argument(
        '--clangd-path', 
        default='clangd', 
        help='Path to clangd executable (default: clangd from PATH)'
    )
    parser.add_argument(
        '--log-level', 
        default='info',
        choices=['debug', 'info', 'warning', 'error'],
        help='Logging level'
    )
    parser.add_argument(
        '--cache-dir', 
        type=Path,
        help='Cache directory (default: ~/.cache/mcp-clangd)'
    )
    parser.add_argument(
        '--no-cache', 
        action='store_true', 
        help='Disable caching'
    )
    parser.add_argument(
        '--timeout',
        default=5.0,
        type=float,
        help='LSP request timeout in seconds (default: 5.0)'
    )
    parser.add_argument(
        '--index-path',
        type=Path,
        help='Path to external clangd index directory'
    )
    parser.add_argument(
        '--warmup',
        action='store_true',
        help='Pre-warm index by opening key files'
    )
    parser.add_argument(
        '--wait-for-index',
        action='store_true',
        help='Wait for background indexing to complete before serving requests'
    )
    parser.add_argument(
        '--index-timeout',
        default=300.0,
        type=float,
        help='Timeout for indexing operations in seconds (default: 300)'
    )
    parser.add_argument(
        '--warmup-limit',
        default=20,
        type=int,
        help='Maximum files to open during warmup (default: 20)'
    )
    # AI Feature Options
    parser.add_argument(
        '--ai-enabled', 
        action='store_true', 
        help='Enable AI-powered documentation summarization'
    )
    parser.add_argument(
        '--ai-provider', 
        default='gemini-2.5-flash',
        choices=['gemini-2.5-flash', 'gemini-2.5-flash-lite'],
        help='AI provider for summarization (default: gemini-2.5-flash)'
    )
    parser.add_argument(
        '--ai-api-key', 
        help='API key for AI provider (or set CLANGAROO_AI_API_KEY)'
    )
    parser.add_argument(
        '--ai-cache-days', 
        default=7, 
        type=int,
        help='Cache AI summaries for N days (default: 7)'
    )
    parser.add_argument(
        '--ai-cost-limit', 
        default=10.0, 
        type=float,
        help='Monthly cost limit in USD (default: 10.0)'
    )
    parser.add_argument(
        '--ai-analysis-level',
        default='summary',
        choices=['summary', 'detailed'],
        help='Default AI analysis depth (default: summary)'
    )
    parser.add_argument(
        '--ai-context-level',
        default='local', 
        choices=['minimal', 'local', 'full'],
        help='Default AI context level (default: local)'
    )
    # Call Hierarchy Options
    parser.add_argument(
        '--call-hierarchy-depth', 
        default=3, 
        type=int,
        help='Maximum depth for recursive call hierarchy (default: 3)'
    )
    parser.add_argument(
        '--call-hierarchy-max-calls', 
        default=100, 
        type=int,
        help='Maximum total calls to return (default: 100)'
    )
    parser.add_argument(
        '--call-hierarchy-per-level', 
        default=25, 
        type=int,
        help='Maximum calls per level (default: 25)'
    )
    parser.add_argument('--debug', action='store_true', help='Run in debug mode (no forking for daemon)')

    args = parser.parse_args()
    
    # Resolve project path to absolute
    project_path = args.project.resolve()
    
    # Build config dict from args and environment variables
    config = {
        'project_root': project_path,
        'clangd_path': args.clangd_path,
        'log_level': args.log_level,
        'cache_dir': args.cache_dir,
        'cache_enabled': not args.no_cache,
        'lsp_timeout': args.timeout,
        'index_path': args.index_path,
        'warmup': args.warmup,
        'wait_for_index': args.wait_for_index,
        'index_timeout': args.index_timeout,
        'warmup_file_limit': args.warmup_limit,
        'ai_enabled': args.ai_enabled,
        'ai_provider': args.ai_provider,
        'ai_api_key': args.ai_api_key or os.getenv('CLANGAROO_AI_API_KEY'),
        'ai_cache_days': args.ai_cache_days,
        'ai_cost_limit_monthly': args.ai_cost_limit,
        'ai_analysis_level': args.ai_analysis_level,
        'ai_context_level': args.ai_context_level,
        'call_hierarchy_max_depth': args.call_hierarchy_depth,
        'call_hierarchy_max_calls': args.call_hierarchy_max_calls,
        'call_hierarchy_max_per_level': args.call_hierarchy_per_level,
        'debug': args.debug
    }
    
    from .utils import setup_logging
    setup_logging(args.log_level)
    global logger
    logger = logging.getLogger(__name__)

    if args.daemon:
        run_daemon_entrypoint(project_path, config)
    else: # Default is proxy mode
        proxy = StdioProxy(project_path, config)
        asyncio.run(proxy.run())

if __name__ == '__main__':
    main()