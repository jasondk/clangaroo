# clangaroo/mcp_clangd/session.py
import asyncio
import json
import logging
import fnmatch
from typing import Optional, Dict, Any, List
from pathlib import Path

from .backend import Backend
from .utils import PerformanceTimer, log_error_with_context

logger = logging.getLogger(__name__)

class ClientSession:
    """
    Handles the MCP protocol for a single client connection, acting as a stateless
    bridge to the shared Backend.
    """
    def __init__(self, backend: Backend, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.backend = backend
        self.reader = reader
        self.writer = writer
        self._is_running = True
        self.initialized = False
        self.protocol_version = "2024-11-05"

    async def run(self) -> None:
        """Main loop to read MCP requests and dispatch them."""
        while self._is_running:
            try:
                request = await self._read_request()
                if not request:
                    break
                response = await self._handle_request(request)
                if response:
                    await self._write_response(response)
            except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
                logger.info("Client disconnected.")
                break
            except Exception as e:
                logger.error(f"Unexpected error in session loop: {e}", exc_info=True)
                break
        self.close()

    async def _read_request(self) -> Optional[Dict]:
        """Reads a single JSON-RPC request from the client stream."""
        try:
            line = await self.reader.readline()
            if not line:
                return None
            line = line.decode('utf-8').strip()
            if not line:
                return None
            return json.loads(line)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON received: {e}")
            return None
        except Exception as e:
            logger.error(f"Error reading request: {e}")
            return None

    async def _write_response(self, response: Dict) -> None:
        """Writes a single JSON-RPC response to the client stream."""
        try:
            data = json.dumps(response, separators=(',', ':')).encode('utf-8') + b'\n'
            self.writer.write(data)
            await self.writer.drain()
        except Exception as e:
            logger.error(f"Error sending response: {e}")
            raise

    async def _handle_request(self, request: Dict) -> Optional[Dict]:
        """Routes a request to the appropriate handler and returns the response."""
        method = request.get('method', '')
        params = request.get('params', {})
        message_id = request.get('id')
        
        logger.debug(f"Handling MCP message: {method} (id: {message_id})")
        
        try:
            if method == 'shutdown':
                self._is_running = False
                return {"jsonrpc": "2.0", "id": message_id, "result": None}
            elif method == "initialize":
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
                    "version": "0.1.0"
                }
            }
        }

    async def _handle_ping(self, message_id: str) -> Dict[str, Any]:
        """Handle ping request"""
        # For now, just return ok status
        # TODO: Implement proper health check through backend
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "status": "ok"
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
                "description": "📍 ANALYSIS: Go to the definition of a symbol at a specific location. Use after finding symbols with search tools. Requires exact file path and position (line, column).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File path (absolute or relative to project)"},
                        "line": {"type": "integer", "description": "Line number (1-based)"},
                        "column": {"type": "integer", "description": "Column number (1-based)"}
                    },
                    "required": ["file", "line", "column"]
                }
            },
            {
                "name": "cpp_references",
                "description": "🔗 ANALYSIS: Find all references to a symbol at a specific location. Shows where functions/variables are used throughout the codebase. Great for understanding impact of changes!",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File path (absolute or relative to project)"},
                        "line": {"type": "integer", "description": "Line number (1-based)"},
                        "column": {"type": "integer", "description": "Column number (1-based)"},
                        "include_declaration": {
                            "type": "boolean",
                            "description": "Include the declaration in results (default: true)",
                            "default": True
                        }
                    },
                    "required": ["file", "line", "column"]
                }
            },
            {
                "name": "cpp_hover",
                "description": "📖 ANALYSIS: Get type information and documentation for a symbol at a specific location. Essential for understanding function signatures, parameter types, and inline documentation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File path (absolute or relative to project)"},
                        "line": {"type": "integer", "description": "Line number (1-based)"},
                        "column": {"type": "integer", "description": "Column number (1-based)"},
                        "summarize": {
                            "type": "boolean",
                            "description": "Generate AI-powered summary of documentation (default: false)",
                            "default": False
                        },
                        "context_level": {
                            "type": "string",
                            "enum": ["minimal", "local", "full"],
                            "description": "Level of surrounding context to include (minimal: just the symbol, local: current scope, full: entire file)",
                            "default": "minimal"
                        }
                    },
                    "required": ["file", "line", "column"]
                }
            },
            {
                "name": "cpp_incoming_calls",
                "description": "⬇️ CALL HIERARCHY: Find all functions that call the specified function. Traces backwards through the call chain to understand how a function is used. Perfect for impact analysis!",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File path (absolute or relative to project)"},
                        "line": {"type": "integer", "description": "Line number (1-based)"},
                        "column": {"type": "integer", "description": "Column number (1-based)"},
                        "analyze": {
                            "type": "boolean", 
                            "description": "Generate AI analysis of call patterns and usage contexts (default: false)",
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
                            "description": "Group results by usage patterns (e.g., error handling, initialization)",
                            "default": False
                        }
                    },
                    "required": ["file", "line", "column"]
                }
            },
            {
                "name": "cpp_outgoing_calls",
                "description": "⬆️ CALL HIERARCHY: Find all functions called by the specified function. Traces forward through the call chain to understand function dependencies. Essential for refactoring!",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File path (absolute or relative to project)"},
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
            timeout = self.backend.config.get('lsp_timeout', 5.0)
            result = await asyncio.wait_for(
                self._execute_tool(tool_name, arguments),
                timeout=timeout * 2  # Give some extra time for complex operations
            )
            
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
                file_path_obj = self.backend.project_root / file_path
            file_path_obj = file_path_obj.resolve()
            
            if not file_path_obj.exists():
                raise ValueError(f"File not found: {file_path}")
                
            # Ensure file is within project root
            try:
                file_path_obj.relative_to(self.backend.project_root)
            except ValueError:
                raise ValueError(f"File is outside project root: {file_path}")
                
        except Exception as e:
            raise ValueError(f"Invalid file path: {e}")

    async def _execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool and return the result"""
        
        with PerformanceTimer(f"tool_{tool_name}", tool=tool_name):
            if tool_name == "cpp_list_files":
                pattern = arguments.get("pattern", "*")
                return await self._handle_list_files(pattern)
                
            elif tool_name == "cpp_search_symbols":
                query = arguments["query"]
                file_pattern = arguments.get("file_pattern", "*")
                return await self._handle_search_symbols(query, file_pattern)
                
            elif tool_name == "cpp_definition":
                # Use backend's LSP methods
                from .lsp_methods import LSPMethods
                lsp_methods = LSPMethods(self.backend.lsp_client)
                return await lsp_methods.get_definition(**arguments)
                
            elif tool_name == "cpp_references":
                # Use backend's LSP methods
                from .lsp_methods import LSPMethods
                lsp_methods = LSPMethods(self.backend.lsp_client)
                include_declaration = arguments.get("include_declaration", True)
                return await lsp_methods.get_references(
                    arguments["file"], arguments["line"], arguments["column"],
                    include_declaration=include_declaration
                )
                
            elif tool_name == "cpp_hover":
                # Use backend's LSP methods
                from .lsp_methods import LSPMethods
                lsp_methods = LSPMethods(self.backend.lsp_client)
                summarize = arguments.get("summarize", False)
                context_level = arguments.get("context_level", "minimal")
                return await lsp_methods.get_hover(
                    arguments["file"], arguments["line"], arguments["column"],
                    summarize=summarize, context_level=context_level
                )
                
            elif tool_name == "cpp_incoming_calls":
                # Use backend's LSP methods
                from .lsp_methods import LSPMethods
                lsp_methods = LSPMethods(self.backend.lsp_client)
                analyze = arguments.get("analyze", False)
                analysis_level = arguments.get("analysis_level", "summary")
                group_by_pattern = arguments.get("group_by_pattern", False)
                return await lsp_methods.get_incoming_calls(
                    arguments["file"], arguments["line"], arguments["column"],
                    analyze=analyze, analysis_level=analysis_level,
                    group_by_pattern=group_by_pattern
                )
                
            elif tool_name == "cpp_outgoing_calls":
                # Use backend's LSP methods  
                from .lsp_methods import LSPMethods
                lsp_methods = LSPMethods(self.backend.lsp_client)
                analyze = arguments.get("analyze", False)
                analysis_level = arguments.get("analysis_level", "summary")
                show_flow = arguments.get("show_flow", True)
                return await lsp_methods.get_outgoing_calls(
                    arguments["file"], arguments["line"], arguments["column"],
                    analyze=analyze, analysis_level=analysis_level,
                    show_flow=show_flow
                )
                
            else:
                raise ValueError(f"Unknown tool: {tool_name}")

    async def _handle_list_files(self, pattern: str = "*") -> Dict[str, Any]:
        """List C++ source files in the project"""
        
        cache_key = f"files:{pattern}"
        
        async def _compute():
            project_root = self.backend.project_root
            
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
                    # Skip hidden directories and common build directories
                    if any(part.startswith('.') for part in file_path.parts):
                        continue
                    if any(part in ['build', 'cmake-build', '_build', 'out'] for part in file_path.parts):
                        continue
                        
                    relative_path = file_path.relative_to(project_root)
                    
                    # If custom pattern, ensure it's a C++ file
                    if pattern != "*":
                        suffix = file_path.suffix.lower()
                        if suffix not in ['.cpp', '.cxx', '.cc', '.c', '.hpp', '.hxx', '.h', '.hh']:
                            continue
                            
                    cpp_files.append(str(relative_path))
                except Exception:
                    continue
                    
            # Sort files for consistent output
            cpp_files.sort()
            
            return {
                "files": cpp_files,
                "total_count": len(cpp_files),
                "pattern": pattern
            }
        
        return await self.backend.get_or_compute(cache_key, _compute)

    async def _handle_search_symbols(self, query: str, file_pattern: str = "*") -> Dict[str, Any]:
        """Search for C++ symbols by name"""
        
        cache_key = f"symbols:{query}:{file_pattern}"
        
        async def _compute():
            symbols = []
            
            try:
                # First try LSP workspace/symbol
                lsp_symbols = await self.backend.execute_lsp_request(
                    'workspace/symbol', {'query': query}
                )
                
                if lsp_symbols:
                    # Convert LSP symbols to our format
                    for symbol in lsp_symbols:
                        location = symbol.get('location', {})
                        uri = location.get('uri', '')
                        if uri.startswith('file://'):
                            file_path = uri[7:]  # Remove 'file://' prefix
                            
                            # Apply file pattern filter
                            if file_pattern != "*":
                                relative_path = Path(file_path).relative_to(self.backend.project_root)
                                if not fnmatch.fnmatch(str(relative_path), file_pattern):
                                    continue
                                    
                            symbols.append({
                                'name': symbol.get('name', ''),
                                'kind': self._lsp_symbol_kind_to_string(symbol.get('kind', 0)),
                                'file': file_path,
                                'line': location.get('range', {}).get('start', {}).get('line', 0) + 1,
                                'column': location.get('range', {}).get('start', {}).get('character', 0) + 1,
                                'container': symbol.get('containerName', '')
                            })
            except Exception as e:
                logger.warning(f"LSP symbol search failed: {e}, falling back to text search")
                
            # If LSP failed or returned no results, use fallback text search
            if not symbols:
                symbols = await self._fallback_symbol_search(query, file_pattern)
                
            return {
                "symbols": symbols,
                "total_count": len(symbols),
                "query": query,
                "file_pattern": file_pattern
            }
        
        return await self.backend.get_or_compute(cache_key, _compute)

    async def _fallback_symbol_search(self, query: str, file_pattern: str = "*") -> List[Dict[str, Any]]:
        """Fallback text-based symbol search when LSP fails"""
        
        # Get list of files to search
        files_result = await self._handle_list_files(file_pattern)
        files = files_result.get('files', [])
        
        symbols = []
        query_lower = query.lower()
        
        # Common C++ symbol patterns
        patterns = [
            # Class definitions
            (r'class\s+(\w+)', 'class'),
            (r'struct\s+(\w+)', 'struct'),
            (r'enum\s+class\s+(\w+)', 'enum class'),
            (r'enum\s+(\w+)', 'enum'),
            # Function definitions (basic)
            (r'(\w+)\s*\([^)]*\)\s*{', 'function'),
            (r'(\w+)\s*\([^)]*\)\s*const\s*{', 'function'),
            # Method declarations
            (r'(\w+)\s*\([^)]*\)\s*;', 'method'),
            (r'(\w+)\s*\([^)]*\)\s*const\s*;', 'method'),
            # Variables and constants
            (r'const\s+\w+\s+(\w+)\s*=', 'constant'),
            (r'static\s+\w+\s+(\w+)\s*=', 'static variable'),
            # Typedefs
            (r'typedef\s+.*\s+(\w+);', 'typedef'),
            (r'using\s+(\w+)\s*=', 'type alias'),
        ]
        
        import re
        
        for file_path in files[:100]:  # Limit to first 100 files for performance
            try:
                full_path = self.backend.project_root / file_path
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                    
                for line_num, line in enumerate(lines, 1):
                    line_lower = line.lower()
                    
                    # Quick check if query appears in line
                    if query_lower not in line_lower:
                        continue
                        
                    # Try to match patterns
                    for pattern, kind in patterns:
                        match = re.search(pattern, line)
                        if match:
                            symbol_name = match.group(1)
                            if query_lower in symbol_name.lower():
                                # Find column position
                                column = line.find(symbol_name) + 1
                                
                                symbols.append({
                                    'name': symbol_name,
                                    'kind': kind,
                                    'file': str(full_path),
                                    'line': line_num,
                                    'column': column,
                                    'container': ''
                                })
                                break
                                
            except Exception as e:
                logger.debug(f"Error searching file {file_path}: {e}")
                continue
                
        return symbols

    def _lsp_symbol_kind_to_string(self, kind: int) -> str:
        """Convert LSP SymbolKind enum to string"""
        kind_map = {
            1: "File", 2: "Module", 3: "Namespace", 4: "Package",
            5: "Class", 6: "Method", 7: "Property", 8: "Field",
            9: "Constructor", 10: "Enum", 11: "Interface", 12: "Function",
            13: "Variable", 14: "Constant", 15: "String", 16: "Number",
            17: "Boolean", 18: "Array", 19: "Object", 20: "Key",
            21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
            25: "Operator", 26: "TypeParameter"
        }
        return kind_map.get(kind, "Unknown")
    
    def close(self):
        """Closes the client connection."""
        self._is_running = False
        if not self.writer.is_closing():
            self.writer.close()