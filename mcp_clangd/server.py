"""
MCP server for C++ code intelligence via clangd
"""

import sys
import json
import asyncio
import logging
import glob
import fnmatch
from typing import Dict, Any, Optional, List
from pathlib import Path

from . import __version__
from .config import Config
from .clangd_manager import ClangdManager
from .lsp_client import LSPClient
from .lsp_methods import LSPMethods
from .index_warmup import IndexWarmup
from .utils import PerformanceTimer, log_error_with_context


logger = logging.getLogger(__name__)


class MCPClangdServer:
    """MCP server providing C++ code intelligence via clangd"""
    
    def __init__(self, config: Config):
        self.config = config
        
        # Core components
        self.clangd_manager = ClangdManager(config)
        self.lsp_client = LSPClient(self.clangd_manager)
        self.lsp_methods = LSPMethods(self.lsp_client)
        self.index_warmup = IndexWarmup(self.lsp_methods, config)
        
        # State
        self.initialized = False
        self._shutdown_event = asyncio.Event()
        
        # MCP protocol version
        self.protocol_version = "2024-11-05"
        
    async def run(self):
        """Run the MCP server"""
        
        logger.info("Starting MCP clangd server...")
        logger.debug(f"Configuration: {self.config.to_dict()}")
        
        try:
            # Start LSP client (which starts clangd)
            await self.lsp_client.start()
            
            # Initialize AI features if enabled
            await self.lsp_methods.initialize_ai_features(self.config)
            
            # Handle indexing enhancements
            await self._handle_indexing_startup()
            
            # Main message loop
            await self._message_loop()
            
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        except Exception as e:
            log_error_with_context(logger, e, {"operation": "server_run"})
            raise
        finally:
            await self.shutdown()
            
    async def shutdown(self):
        """Shutdown the server gracefully"""
        
        logger.info("Shutting down MCP server...")
        self._shutdown_event.set()
        
        try:
            # Close documents and stop LSP client
            if hasattr(self.lsp_methods, 'document_manager'):
                await self.lsp_methods.document_manager.close_all_documents()
            await self.lsp_client.stop()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            
        logger.info("MCP server shutdown complete")
        
    async def _handle_indexing_startup(self):
        """Handle indexing enhancements during startup"""
        
        try:
            # Index warmup (runs in parallel with other startup)
            warmup_task = None
            if self.config.warmup:
                logger.info("Starting index warmup...")
                warmup_task = asyncio.create_task(self.index_warmup.warmup_project())
                
            # Wait for indexing if requested
            if self.config.wait_for_index:
                logger.info("Waiting for background indexing to complete...")
                indexing_completed = await self.lsp_client.wait_for_indexing(
                    timeout=self.config.index_timeout
                )
                
                if not indexing_completed:
                    logger.warning("Proceeding with partial index due to timeout")
                else:
                    logger.info("Background indexing completed successfully")
                    
            # Wait for warmup to complete if it was started
            if warmup_task:
                try:
                    await asyncio.wait_for(warmup_task, timeout=60.0)
                except asyncio.TimeoutError:
                    logger.warning("Index warmup timed out")
                    warmup_task.cancel()
                except Exception as e:
                    logger.error(f"Index warmup failed: {e}")
                    
            logger.info("Indexing startup phase completed")
            
        except Exception as e:
            log_error_with_context(logger, e, {"operation": "indexing_startup"})
            logger.warning("Continuing with degraded indexing performance")
        
    async def _message_loop(self):
        """Main MCP message processing loop"""
        
        while not self._shutdown_event.is_set():
            try:
                message = await self._read_message()
                if not message:
                    logger.debug("No message received, continuing...")
                    continue
                    
                response = await self._handle_message(message)
                if response:
                    await self._send_message(response)
                    
            except EOFError:
                logger.info("Client disconnected")
                break
            except Exception as e:
                log_error_with_context(logger, e, {"operation": "message_processing"})
                
                # Send error response if we have message ID
                error_response = {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32603,
                        "message": f"Internal error: {str(e)}"
                    }
                }
                
                try:
                    await self._send_message(error_response)
                except Exception:
                    # If we can't send error response, we're in trouble
                    logger.error("Failed to send error response")
                    break
                    
    async def _handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle incoming MCP message"""
        
        method = message.get("method")
        params = message.get("params", {})
        message_id = message.get("id")
        
        logger.debug(f"Handling MCP message: {method} (id: {message_id})")
        
        try:
            if method == "initialize":
                return await self._handle_initialize(params, message_id)
            elif method == "tools/list":
                return await self._handle_list_tools(message_id)
            elif method == "tools/call":
                return await self._handle_tool_call(params, message_id)
            elif method == "ping":
                return await self._handle_ping(message_id)
            elif method == "resources/list":
                return await self._handle_list_resources(message_id)
            elif method == "prompts/list":
                return await self._handle_list_prompts(message_id)
            else:
                logger.warning(f"Unknown method: {method}")
                return {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}"
                    }
                } if message_id else None
                
        except Exception as e:
            log_error_with_context(logger, e, {"method": method, "params": params})
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {
                    "code": -32603,
                    "message": f"Error handling {method}: {str(e)}"
                }
            } if message_id else None
            
    async def _handle_initialize(self, params: Dict[str, Any], message_id: str) -> Dict[str, Any]:
        """Handle MCP initialize request"""
        
        logger.info("Initializing MCP connection...")
        
        client_info = params.get("clientInfo", {})
        logger.info(f"Client: {client_info.get('name', 'Unknown')} v{client_info.get('version', 'Unknown')}")
        
        self.initialized = True
        
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": self.protocol_version,
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "mcp-clangd",
                    "version": __version__
                }
            }
        }
        
    async def _handle_ping(self, message_id: str) -> Dict[str, Any]:
        """Handle ping request"""
        
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "status": "ok",
                "clangd_health": await self.clangd_manager.health_check()
            }
        }
    
    async def _handle_list_resources(self, message_id: str) -> Dict[str, Any]:
        """Handle resources/list request"""
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "resources": []
            }
        }
    
    async def _handle_list_prompts(self, message_id: str) -> Dict[str, Any]:
        """Handle prompts/list request"""
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "prompts": []
            }
        }
        
    async def _handle_list_tools(self, message_id: str) -> Dict[str, Any]:
        """Handle tools/list request"""
        
        tools = [
            {
                "name": "cpp_list_files", 
                "description": "🔍 DISCOVERY: List C++ source files in the project. Use this first to explore the codebase structure and understand what files are available before diving into specific symbols.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Optional glob pattern to filter files (e.g., '*.cpp', 'src/*')",
                            "default": "*"
                        }
                    }
                }
            },
            {
                "name": "cpp_search_symbols",
                "description": "🔍 DISCOVERY: Search for C++ symbols (functions, classes, variables) by name. Use this FIRST to locate symbols before using analysis tools. Provides hybrid Tree-sitter + clangd search with graceful fallback - always finds something!",
                "inputSchema": {
                    "type": "object", 
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Symbol name or partial name to search for"
                        },
                        "file_pattern": {
                            "type": "string", 
                            "description": "Optional file pattern to limit search (e.g., 'src/*.cpp')",
                            "default": "*"
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "cpp_definition",
                "description": "📍 ANALYSIS: Find where a C++ symbol is defined. WORKFLOW: First use cpp_search_symbols to locate the symbol, then use this tool with the exact file/line/column coordinates.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string", 
                            "description": "Relative path to the source file from project root"
                        },
                        "line": {
                            "type": "integer", 
                            "description": "Line number (1-based)"
                        },
                        "column": {
                            "type": "integer", 
                            "description": "Column number (1-based)"
                        }
                    },
                    "required": ["file", "line", "column"]
                }
            },
            {
                "name": "cpp_references",
                "description": "🔗 ANALYSIS: Find all references to a C++ symbol across the codebase. WORKFLOW: First use cpp_search_symbols to locate the symbol, then use this for comprehensive usage analysis.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "Relative path to the source file from project root"},
                        "line": {"type": "integer", "description": "Line number (1-based)"},
                        "column": {"type": "integer", "description": "Column number (1-based)"},
                        "include_declaration": {
                            "type": "boolean", 
                            "description": "Include declaration in results",
                            "default": True
                        }
                    },
                    "required": ["file", "line", "column"]
                }
            },
            {
                "name": "cpp_hover",
                "description": "📚 ANALYSIS: Get type information and documentation for a C++ symbol. WORKFLOW: Use cpp_search_symbols first to locate the symbol, then get detailed type info and docs.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "Relative path to the source file from project root"},
                        "line": {"type": "integer", "description": "Line number (1-based)"},
                        "column": {"type": "integer", "description": "Column number (1-based)"},
                        "summarize": {
                            "type": "boolean", 
                            "description": "Generate AI summary of documentation (default: false)",
                            "default": False
                        },
                        "context_level": {
                            "type": "string",
                            "enum": ["minimal", "local", "full"],
                            "description": "Context level for AI: minimal (docs only), local (surrounding code), full (entire file + deps)",
                            "default": "minimal"
                        }
                    },
                    "required": ["file", "line", "column"]
                }
            },
            {
                "name": "cpp_incoming_calls",
                "description": "📞 CALL ANALYSIS: Find functions that call this C++ function. WORKFLOW: First use cpp_search_symbols to locate the function, then analyze its callers and usage patterns.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "Relative path to the source file from project root"},
                        "line": {"type": "integer", "description": "Line number (1-based)"},
                        "column": {"type": "integer", "description": "Column number (1-based)"},
                        "analyze": {
                            "type": "boolean", 
                            "description": "Generate AI analysis of call patterns and purposes (default: false)",
                            "default": False
                        },
                        "analysis_level": {
                            "type": "string",
                            "enum": ["summary", "detailed"],
                            "description": "Level of AI analysis: summary (quick overview) or detailed (comprehensive)",
                            "default": "summary"
                        },
                        "group_by_pattern": {
                            "type": "boolean",
                            "description": "Group calls by programming patterns (validation, error handling, etc.)",
                            "default": True
                        }
                    },
                    "required": ["file", "line", "column"]
                }
            },
            {
                "name": "cpp_outgoing_calls",
                "description": "📤 CALL ANALYSIS: Find functions that this C++ function calls. WORKFLOW: First use cpp_search_symbols to locate the function, then understand what it calls and its dependencies.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "Relative path to the source file from project root"},
                        "line": {"type": "integer", "description": "Line number (1-based)"},
                        "column": {"type": "integer", "description": "Column number (1-based)"},
                        "analyze": {
                            "type": "boolean", 
                            "description": "Generate AI analysis of call flow and dependencies (default: false)",
                            "default": False
                        },
                        "analysis_level": {
                            "type": "string",
                            "enum": ["summary", "detailed"],
                            "description": "Level of AI analysis: summary (quick overview) or detailed (comprehensive)",
                            "default": "summary"
                        },
                        "show_flow": {
                            "type": "boolean",
                            "description": "Show logical execution flow and call sequence analysis",
                            "default": True
                        }
                    },
                    "required": ["file", "line", "column"]
                }
            }
        ]
        
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {"tools": tools}
        }
        
    async def _handle_tool_call(self, params: Dict[str, Any], message_id: str) -> Dict[str, Any]:
        """Handle tools/call request"""
        
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        if not self.initialized:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {
                    "code": -32002,
                    "message": "Server not initialized"
                }
            }
            
        logger.info(f"Executing tool: {tool_name}")
        logger.debug(f"Tool arguments: {arguments}")
        
        try:
            # Validate arguments
            self._validate_tool_arguments(tool_name, arguments)
            
            # Execute tool with timeout
            result = await self._execute_tool(tool_name, arguments)
            
            logger.debug(f"Tool {tool_name} completed successfully")
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2)
                        }
                    ]
                }
            }
            
        except asyncio.TimeoutError:
            logger.warning(f"Tool {tool_name} timed out")
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {
                    "code": -32603,
                    "message": f"Tool {tool_name} timed out"
                }
            }
        except ValueError as e:
            logger.error(f"Invalid arguments for {tool_name}: {e}")
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {
                    "code": -32602,
                    "message": str(e)
                }
            }
        except Exception as e:
            log_error_with_context(logger, e, {"tool": tool_name, "arguments": arguments})
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {
                    "code": -32603,
                    "message": f"Tool execution failed: {str(e)}"
                }
            }
            
    def _validate_tool_arguments(self, tool_name: str, arguments: Dict[str, Any]):
        """Validate tool arguments"""
        
        # Discovery tools have different validation requirements
        if tool_name == "cpp_list_files":
            # Optional pattern argument
            if "pattern" in arguments and not isinstance(arguments["pattern"], str):
                raise ValueError("pattern must be a string")
            return
            
        elif tool_name == "cpp_search_symbols":
            # Required query argument
            if "query" not in arguments:
                raise ValueError("Missing required argument: query")
            if not isinstance(arguments["query"], str):
                raise ValueError("query must be a string")
            # Optional file_pattern argument  
            if "file_pattern" in arguments and not isinstance(arguments["file_pattern"], str):
                raise ValueError("file_pattern must be a string")
            return
        
        # Position-based tools require file, line, column
        required_fields = ["file", "line", "column"]
        for field in required_fields:
            if field not in arguments:
                raise ValueError(f"Missing required argument: {field}")
                
        # Validate types
        file_path = arguments["file"]
        if not isinstance(file_path, str):
            raise ValueError("file must be a string")
            
        line = arguments["line"]
        if not isinstance(line, int) or line < 1:
            raise ValueError("line must be a positive integer")
            
        column = arguments["column"]
        if not isinstance(column, int) or column < 1:
            raise ValueError("column must be a positive integer")
            
        # Check if file exists and is within project
        try:
            file_path_obj = Path(file_path)
            if not file_path_obj.is_absolute():
                file_path_obj = self.config.project_root / file_path
            file_path_obj = file_path_obj.resolve()
            
            if not file_path_obj.exists():
                raise ValueError(f"File not found: {file_path}")
                
            # Check if file is within project root
            try:
                file_path_obj.relative_to(self.config.project_root)
            except ValueError:
                raise ValueError(f"File must be within project root: {file_path}")
                
            # Update arguments with resolved path
            arguments["file"] = str(file_path_obj)
            
        except Exception as e:
            raise ValueError(f"Invalid file path: {e}")
            
    async def _execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Execute a tool with appropriate timeout"""
        
        # Set timeout based on tool type
        if tool_name in ["cpp_definition", "cpp_hover"]:
            timeout = 10.0  # Fast operations (increased for Claude Code compatibility)
        elif tool_name in ["cpp_list_files", "cpp_search_symbols"]:
            timeout = 10.0  # Discovery operations (need more time for indexing)
        else:
            timeout = 15.0  # Slower operations (references, call hierarchy)
            
        with PerformanceTimer(f"tool_{tool_name}", logger):
            try:
                # Define the task to execute
                async def _tool_task():
                    if tool_name == "cpp_list_files":
                        return await self._handle_list_files(**arguments)
                    elif tool_name == "cpp_search_symbols":
                        return await self._handle_search_symbols(**arguments)
                    elif tool_name == "cpp_definition":
                        return await self.lsp_methods.get_definition(**arguments)
                    elif tool_name == "cpp_references":
                        include_declaration = arguments.get("include_declaration", True)
                        return await self.lsp_methods.get_references(
                            arguments["file"], arguments["line"], arguments["column"], include_declaration
                        )
                    elif tool_name == "cpp_hover":
                        summarize = arguments.get("summarize", False)
                        context_level = arguments.get("context_level", "minimal")
                        return await self.lsp_methods.get_hover(
                            arguments["file"], arguments["line"], arguments["column"],
                            summarize=summarize, context_level=context_level
                        )
                    elif tool_name == "cpp_incoming_calls":
                        analyze = arguments.get("analyze", False)
                        analysis_level = arguments.get("analysis_level", "summary")
                        group_by_pattern = arguments.get("group_by_pattern", True)
                        return await self.lsp_methods.get_incoming_calls(
                            arguments["file"], arguments["line"], arguments["column"],
                            analyze=analyze, analysis_level=analysis_level, group_by_pattern=group_by_pattern
                        )
                    elif tool_name == "cpp_outgoing_calls":
                        analyze = arguments.get("analyze", False)
                        analysis_level = arguments.get("analysis_level", "summary")
                        show_flow = arguments.get("show_flow", True)
                        return await self.lsp_methods.get_outgoing_calls(
                            arguments["file"], arguments["line"], arguments["column"],
                            analyze=analyze, analysis_level=analysis_level, show_flow=show_flow
                        )
                    else:
                        raise ValueError(f"Unknown tool: {tool_name}")
                
                # Use asyncio.wait_for for Python 3.10+ compatibility instead of asyncio.timeout
                return await asyncio.wait_for(_tool_task(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"Tool {tool_name} timed out after {timeout}s")
                raise
                
    async def _read_message(self) -> Optional[Dict[str, Any]]:
        """Read MCP message from stdin"""
        
        try:
            # Read line from stdin
            line = await asyncio.get_event_loop().run_in_executor(
                None, sys.stdin.readline
            )
            
            if not line:
                return None
                
            line = line.strip()
            if not line:
                return None
                
            # Parse JSON
            try:
                message = json.loads(line)
                return message
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON received: {e}")
                logger.debug(f"Raw line: {repr(line)}")
                return None
                
        except Exception as e:
            logger.error(f"Error reading message: {e}")
            return None
    
    async def _handle_list_files(self, pattern: str = "*") -> Dict[str, Any]:
        """List C++ source files in the project"""
        
        try:
            project_root = Path(self.config.project_root)
            
            # Common C++ file extensions
            cpp_extensions = ["*.cpp", "*.cxx", "*.cc", "*.c", "*.hpp", "*.hxx", "*.h", "*.hh"]
            
            files = []
            
            if pattern == "*":
                # List all C++ files
                for ext in cpp_extensions:
                    files.extend(project_root.rglob(ext))
            else:
                # Use provided pattern
                files.extend(project_root.rglob(pattern))
            
            # Convert to relative paths and filter C++ files if pattern was custom
            cpp_files = []
            for file_path in files:
                try:
                    rel_path = file_path.relative_to(project_root)
                    # Only include if it's a C++ file or matches pattern exactly
                    if pattern != "*" or any(fnmatch.fnmatch(file_path.name, ext) for ext in cpp_extensions):
                        cpp_files.append(str(rel_path))
                except ValueError:
                    # Skip files outside project root
                    continue
            
            # Sort and limit results
            cpp_files.sort()
            if len(cpp_files) > 100:
                cpp_files = cpp_files[:100]  # Limit to first 100 files
                
            return {
                "files": cpp_files,
                "total_found": len(cpp_files),
                "pattern_used": pattern,
                "project_root": str(project_root)
            }
            
        except Exception as e:
            logger.error(f"Error listing files: {e}")
            return {
                "files": [],
                "error": str(e),
                "pattern_used": pattern
            }
    
    async def _handle_search_symbols(self, query: str, file_pattern: str = "*") -> Dict[str, Any]:
        """Search for C++ symbols using LSP workspace symbol search"""
        
        try:
            # Use LSP workspace symbol search 
            symbols = await self.lsp_client.workspace_symbols(query)
            
            # Filter by file pattern if specified
            if file_pattern != "*":
                project_root = Path(self.config.project_root)
                filtered_symbols = []
                for symbol in symbols:
                    file_path = Path(symbol.get('file', ''))
                    if file_path.is_absolute():
                        try:
                            rel_path = file_path.relative_to(project_root)
                            if fnmatch.fnmatch(str(rel_path), file_pattern):
                                filtered_symbols.append(symbol)
                        except ValueError:
                            continue
                    else:
                        # File is already relative path
                        if fnmatch.fnmatch(symbol.get('file', ''), file_pattern):
                            filtered_symbols.append(symbol)
                symbols = filtered_symbols
            
            # If we got results from LSP, return them
            if symbols:
                return {
                    "symbols": symbols[:50],  # Limit to 50 results
                    "query": query,
                    "file_pattern": file_pattern,
                    "total_found": len(symbols),
                    "method": "lsp_workspace_symbols"
                }
            else:
                # Fallback: simple text search in files
                logger.info(f"No LSP symbols found for '{query}', trying text search fallback")
                return await self._fallback_symbol_search(query, file_pattern)
                
        except Exception as e:
            logger.error(f"Error searching symbols: {e}")
            # Try fallback on error
            return await self._fallback_symbol_search(query, file_pattern)
    
    async def _fallback_symbol_search(self, query: str, file_pattern: str = "*") -> Dict[str, Any]:
        """Fallback symbol search using simple text matching"""
        
        try:
            project_root = Path(self.config.project_root)
            results = []
            
            # Get files to search
            files_result = await self._handle_list_files(file_pattern)
            files_to_search = files_result.get("files", [])[:20]  # Limit to 20 files
            
            # Simple text search for the query
            for rel_file_path in files_to_search:
                file_path = project_root / rel_file_path
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        lines = f.readlines()
                        
                    for line_num, line in enumerate(lines, 1):
                        if query.lower() in line.lower():
                            # Extract the part around the match
                            context = line.strip()
                            if len(context) > 100:
                                # Find query position and show context around it
                                pos = context.lower().find(query.lower())
                                start = max(0, pos - 30)
                                end = min(len(context), pos + len(query) + 30)
                                context = context[start:end]
                                if start > 0:
                                    context = "..." + context
                                if end < len(line.strip()):
                                    context = context + "..."
                            
                            results.append({
                                "name": query,
                                "file": rel_file_path,
                                "line": line_num,
                                "context": context,
                                "kind": "text_match"
                            })
                            
                            if len(results) >= 20:  # Limit total results
                                break
                                
                except Exception as e:
                    logger.debug(f"Error reading {file_path}: {e}")
                    continue
                    
                if len(results) >= 20:
                    break
            
            return {
                "symbols": results,
                "query": query,
                "file_pattern": file_pattern,
                "total_found": len(results),
                "method": "text_search"
            }
            
        except Exception as e:
            logger.error(f"Error in fallback symbol search: {e}")
            return {
                "symbols": [],
                "error": str(e),
                "query": query,
                "file_pattern": file_pattern
            }
            
    async def _send_message(self, message: Dict[str, Any]):
        """Send MCP message to stdout"""
        
        try:
            # Convert to JSON and send
            json_str = json.dumps(message, separators=(',', ':'))
            sys.stdout.write(json_str + '\n')
            sys.stdout.flush()
            
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            raise