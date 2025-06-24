"""
CLI entry point for mcp-clangd server
"""

import click
import asyncio
import logging
import sys
from pathlib import Path

from .config import Config
from .server import MCPClangdServer
from .utils import setup_logging


@click.command()
@click.option(
    '--project', 
    required=True, 
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help='Path to C++ project root (must contain compile_commands.json)'
)
@click.option(
    '--clangd-path', 
    default='clangd', 
    help='Path to clangd executable (default: clangd from PATH)'
)
@click.option(
    '--log-level', 
    default='info',
    type=click.Choice(['debug', 'info', 'warning', 'error'], case_sensitive=False),
    help='Logging level'
)
@click.option(
    '--cache-dir', 
    type=click.Path(path_type=Path),
    help='Cache directory (default: ~/.cache/mcp-clangd)'
)
@click.option(
    '--no-cache', 
    is_flag=True, 
    help='Disable caching'
)
@click.option(
    '--timeout',
    default=5.0,
    type=float,
    help='LSP request timeout in seconds (default: 5.0)'
)
@click.option(
    '--index-path',
    type=click.Path(path_type=Path),
    help='Path to external clangd index directory'
)
@click.option(
    '--warmup',
    is_flag=True,
    help='Pre-warm index by opening key files'
)
@click.option(
    '--wait-for-index',
    is_flag=True,
    help='Wait for background indexing to complete before serving requests'
)
@click.option(
    '--index-timeout',
    default=300.0,
    type=float,
    help='Timeout for indexing operations in seconds (default: 300)'
)
@click.option(
    '--warmup-limit',
    default=20,
    type=int,
    help='Maximum files to open during warmup (default: 20)'
)
# AI Feature Options
@click.option(
    '--ai-enabled', 
    is_flag=True, 
    help='Enable AI-powered documentation summarization'
)
@click.option(
    '--ai-provider', 
    default='gemini-2.5-flash',
    type=click.Choice(['gemini-2.5-flash', 'gemini-2.5-flash-lite']),
    help='AI provider for summarization (default: gemini-2.5-flash)'
)
@click.option(
    '--ai-api-key', 
    envvar='CLANGAROO_AI_API_KEY',
    help='API key for AI provider (or set CLANGAROO_AI_API_KEY)'
)
@click.option(
    '--ai-cache-days', 
    default=7, 
    type=int,
    help='Cache AI summaries for N days (default: 7)'
)
@click.option(
    '--ai-cost-limit', 
    default=10.0, 
    type=float,
    help='Monthly cost limit in USD (default: 10.0)'
)
@click.option(
    '--ai-analysis-level',
    default='summary',
    type=click.Choice(['summary', 'detailed']),
    help='Default AI analysis depth (default: summary)'
)
@click.option(
    '--ai-context-level',
    default='local', 
    type=click.Choice(['minimal', 'local', 'full']),
    help='Default AI context level (default: local)'
)
# Call Hierarchy Options
@click.option(
    '--call-hierarchy-depth', 
    default=3, 
    type=int,
    help='Maximum depth for recursive call hierarchy (default: 3)'
)
@click.option(
    '--call-hierarchy-max-calls', 
    default=100, 
    type=int,
    help='Maximum total calls to return (default: 100)'
)
@click.option(
    '--call-hierarchy-per-level', 
    default=25, 
    type=int,
    help='Maximum calls per level (default: 25)'
)
@click.version_option(version="0.1.0", prog_name="mcp-clangd")
def main(project, clangd_path, log_level, cache_dir, no_cache, timeout, 
         index_path, warmup, wait_for_index, index_timeout, warmup_limit,
         ai_enabled, ai_provider, ai_api_key, ai_cache_days, ai_cost_limit,
         ai_analysis_level, ai_context_level,
         call_hierarchy_depth, call_hierarchy_max_calls, call_hierarchy_per_level):
    """MCP server providing C++ code intelligence via clangd
    
    This service bridges the Model Context Protocol (MCP) with clangd's Language
    Server Protocol (LSP) to provide fast C++ code intelligence for Claude Code.
    
    Example usage:
    
        mcp-clangd --project /path/to/cpp/project
        mcp-clangd --project . --clangd-path /usr/local/bin/clangd --log-level debug
    """
    
    # Setup logging first
    setup_logging(log_level)
    logger = logging.getLogger(__name__)
    
    try:
        # Create configuration
        config = Config(
            project_root=project,
            clangd_path=clangd_path,
            log_level=log_level,
            cache_dir=cache_dir,
            cache_enabled=not no_cache,
            lsp_timeout=timeout,
            # Indexing options
            index_path=index_path,
            warmup=warmup,
            wait_for_index=wait_for_index,
            index_timeout=index_timeout,
            warmup_file_limit=warmup_limit,
            # AI options
            ai_enabled=ai_enabled,
            ai_provider=ai_provider,
            ai_api_key=ai_api_key,
            ai_cache_days=ai_cache_days,
            ai_cost_limit_monthly=ai_cost_limit,
            ai_analysis_level=ai_analysis_level,
            ai_context_level=ai_context_level,
            # Call hierarchy options
            call_hierarchy_max_depth=call_hierarchy_depth,
            call_hierarchy_max_calls=call_hierarchy_max_calls,
            call_hierarchy_max_per_level=call_hierarchy_per_level
        )
        
        logger.info(f"Starting mcp-clangd server for project: {config.project_root}")
        logger.info(f"Using clangd: {config.clangd_path}")
        logger.info(f"Cache enabled: {config.cache_enabled}")
        
        # Create and run server
        server = MCPClangdServer(config)
        asyncio.run(server.run())
        
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Server startup failed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()