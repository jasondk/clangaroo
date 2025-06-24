"""
Rich-Context MCP Service for C++ Code Intelligence

A Python-based bridge between MCP (Model Context Protocol) and clangd's 
LSP (Language Server Protocol) capabilities, providing Claude Code with 
fast, reliable C++ code intelligence.
"""

__version__ = "0.1.0"
__author__ = "Rich Context Team"

from .server import MCPClangdServer
from .config import Config

__all__ = ["MCPClangdServer", "Config"]