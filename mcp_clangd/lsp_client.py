"""
LSP client for communicating with clangd
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional, Callable, List, Union
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .clangd_manager import ClangdManager
from .utils import PerformanceTimer, log_error_with_context


logger = logging.getLogger(__name__)


@dataclass
class IndexingStatus:
    """Track clangd indexing progress"""
    
    is_indexing: bool = False
    progress_percentage: Optional[float] = None
    files_processed: int = 0
    total_files: Optional[int] = None
    start_time: float = field(default_factory=time.time)
    current_operation: str = ""
    
    @property
    def elapsed_time(self) -> float:
        """Get elapsed indexing time in seconds"""
        return time.time() - self.start_time
        
    @property
    def estimated_remaining(self) -> Optional[float]:
        """Estimate remaining time based on progress"""
        if self.progress_percentage and self.progress_percentage > 0:
            total_time = self.elapsed_time / (self.progress_percentage / 100)
            return total_time - self.elapsed_time
        return None
        
    def __str__(self) -> str:
        """Human-readable status"""
        if not self.is_indexing:
            return "Indexing complete"
            
        parts = [f"Indexing: {self.current_operation}"]
        
        if self.progress_percentage is not None:
            parts.append(f"{self.progress_percentage:.1f}%")
            
        if self.files_processed > 0:
            if self.total_files:
                parts.append(f"({self.files_processed}/{self.total_files} files)")
            else:
                parts.append(f"({self.files_processed} files)")
                
        elapsed = int(self.elapsed_time)
        parts.append(f"{elapsed}s")
        
        if self.estimated_remaining:
            remaining = int(self.estimated_remaining)
            parts.append(f"~{remaining}s remaining")
            
        return " ".join(parts)


@dataclass
class PendingRequest:
    """Tracks a pending LSP request"""
    
    id: str
    method: str
    future: asyncio.Future
    start_time: float
    
    @property
    def elapsed_ms(self) -> float:
        """Get elapsed time in milliseconds"""
        return (time.time() - self.start_time) * 1000


class LSPClient:
    """Language Server Protocol client for clangd"""
    
    def __init__(self, clangd_manager: ClangdManager):
        self.clangd_manager = clangd_manager
        self.request_counter = 0
        self.pending_requests: Dict[str, PendingRequest] = {}
        self.notification_handlers: Dict[str, Callable] = {}
        self.initialized = False
        self._reader_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # Server capabilities received during initialization
        self.server_capabilities: Dict[str, Any] = {}
        
        # Indexing status tracking
        self.indexing_status = IndexingStatus()
        self.indexing_complete = asyncio.Event()
        
        # Set up built-in progress handlers
        self.notification_handlers["$/progress"] = self._handle_progress_notification
        
    async def start(self):
        """Start the LSP client and initialize with clangd"""
        
        logger.info("Starting LSP client...")
        
        # Start clangd process
        await self.clangd_manager.start()
        
        # Start message reader
        self._reader_task = asyncio.create_task(self._read_messages())
        
        # Initialize LSP connection
        await self._initialize()
        
        logger.info("LSP client started successfully")
        
    async def stop(self):
        """Stop the LSP client"""
        
        logger.info("Stopping LSP client...")
        self._shutdown_event.set()
        
        # Send shutdown request if initialized
        if self.initialized:
            try:
                await self.request("shutdown", {}, timeout=2.0)
                await self.notify("exit", {})
            except Exception as e:
                logger.warning(f"Error during LSP shutdown: {e}")
                
        # Cancel reader task
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
                
        # Stop clangd
        await self.clangd_manager.stop()
        
        logger.info("LSP client stopped")
        
    async def _initialize(self):
        """Send LSP initialize request"""
        
        logger.debug("Initializing LSP connection...")
        
        init_params = {
            "processId": None,
            "clientInfo": {
                "name": "mcp-clangd",
                "version": "0.1.0"
            },
            "rootUri": f"file://{self.clangd_manager.config.project_root}",
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "hover": {"dynamicRegistration": False},
                    "callHierarchy": {"dynamicRegistration": False},
                    "typeHierarchy": {"dynamicRegistration": False},
                    "synchronization": {
                        "dynamicRegistration": False,
                        "willSave": False,
                        "willSaveWaitUntil": False,
                        "didSave": False
                    }
                },
                "workspace": {
                    "applyEdit": False,
                    "workspaceEdit": {
                        "documentChanges": False
                    },
                    "didChangeConfiguration": {
                        "dynamicRegistration": False
                    }
                }
            },
            "initializationOptions": {
                "clangdFileStatus": True,
                "fallbackFlags": []
            }
        }
        
        with PerformanceTimer("lsp_initialization", logger):
            response = await self.request("initialize", init_params, timeout=10.0)
            
        self.server_capabilities = response.get("capabilities", {})
        logger.debug(f"Server capabilities: {list(self.server_capabilities.keys())}")
        
        # Send initialized notification
        await self.notify("initialized", {})
        self.initialized = True
        
        logger.info("LSP initialization completed")
        
    async def request(self, method: str, params: Dict[str, Any], timeout: float = 5.0) -> Any:
        """Send LSP request and wait for response
        
        Args:
            method: LSP method name
            params: Request parameters
            timeout: Request timeout in seconds
            
        Returns:
            Response result
            
        Raises:
            asyncio.TimeoutError: If request times out
            RuntimeError: If LSP error occurs
        """
        
        if not self.clangd_manager.process:
            logger.error("Clangd process not started")
            raise RuntimeError("Clangd process not started - clangd may not be available or failed to start")
        
        if not self.clangd_manager.process.is_alive:
            logger.error(f"Clangd process died (return code: {self.clangd_manager.process.returncode})")
            raise RuntimeError(f"Clangd process died with return code: {self.clangd_manager.process.returncode}")
            
        request_id = str(self.request_counter)
        self.request_counter += 1
        
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params
        }
        
        future = asyncio.Future()
        pending = PendingRequest(
            id=request_id,
            method=method,
            future=future,
            start_time=time.time()
        )
        self.pending_requests[request_id] = pending
        
        try:
            await self._send_message(message)
            result = await asyncio.wait_for(future, timeout)
            return result
        except asyncio.TimeoutError:
            # Clean up pending request
            if request_id in self.pending_requests:
                del self.pending_requests[request_id]
            logger.warning(f"LSP request timeout: {method} after {timeout}s")
            raise
        except Exception as e:
            # Clean up pending request
            if request_id in self.pending_requests:
                del self.pending_requests[request_id]
            raise
            
    async def notify(self, method: str, params: Dict[str, Any]):
        """Send LSP notification (no response expected)
        
        Args:
            method: LSP method name
            params: Notification parameters
        """
        
        if not self.clangd_manager.process:
            logger.error("Clangd process not started")
            raise RuntimeError("Clangd process not started - clangd may not be available or failed to start")
        
        if not self.clangd_manager.process.is_alive:
            logger.error(f"Clangd process died (return code: {self.clangd_manager.process.returncode})")
            raise RuntimeError(f"Clangd process died with return code: {self.clangd_manager.process.returncode}")
            
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        
        await self._send_message(message)
        
    async def wait_for_indexing(self, timeout: float = 300.0) -> bool:
        """Wait for background indexing to complete
        
        Args:
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if indexing completed, False if timed out
        """
        
        logger.info(f"Waiting for indexing to complete (timeout: {timeout}s)...")
        
        # Reset indexing status
        self.indexing_status = IndexingStatus()
        self.indexing_complete.clear()
        
        try:
            # Wait for indexing to complete or timeout
            await asyncio.wait_for(self.indexing_complete.wait(), timeout)
            logger.info("Background indexing completed successfully")
            return True
        except asyncio.TimeoutError:
            logger.warning(f"Indexing did not complete within {timeout}s")
            return False
            
    async def _handle_progress_notification(self, params: Dict[str, Any]):
        """Handle $/progress notifications from clangd"""
        
        try:
            token = params.get("token", "")
            value = params.get("value", {})
            
            # Look for indexing-related progress
            title = value.get("title", "").lower()
            message = value.get("message", "").lower()
            
            if any(keyword in title or keyword in message 
                   for keyword in ["index", "parsing", "loading"]):
                   
                kind = value.get("kind", "")
                
                if kind == "begin":
                    # Indexing started
                    self.indexing_status.is_indexing = True
                    self.indexing_status.start_time = time.time()
                    self.indexing_status.current_operation = value.get("title", "Indexing")
                    self.indexing_complete.clear()
                    
                    logger.info(f"Indexing started: {self.indexing_status.current_operation}")
                    
                elif kind == "report":
                    # Progress update
                    if self.indexing_status.is_indexing:
                        percentage = value.get("percentage")
                        if percentage is not None:
                            self.indexing_status.progress_percentage = percentage
                            
                        message_text = value.get("message", "")
                        if message_text:
                            self.indexing_status.current_operation = message_text
                            
                        # Log progress occasionally
                        if percentage and int(percentage) % 10 == 0:
                            logger.info(str(self.indexing_status))
                            
                elif kind == "end":
                    # Indexing completed
                    if self.indexing_status.is_indexing:
                        self.indexing_status.is_indexing = False
                        self.indexing_status.progress_percentage = 100.0
                        self.indexing_complete.set()
                        
                        logger.info(f"Indexing completed in {self.indexing_status.elapsed_time:.1f}s")
                        
        except Exception as e:
            logger.debug(f"Error handling progress notification: {e}")
    
    async def workspace_symbols(self, query: str) -> List[Dict[str, Any]]:
        """Search for symbols in the workspace using LSP workspace/symbol"""
        
        try:
            params = {"query": query}
            
            with PerformanceTimer(f"workspace_symbols_query_{query[:20]}", logger):
                response = await self.request("workspace/symbol", params, timeout=10.0)
                
            if response is None:
                logger.debug(f"No workspace symbols response for query: {query}")
                return []
                
            # Response should be a list of SymbolInformation
            symbols = response if isinstance(response, list) else []
            
            # Convert to more usable format
            results = []
            for symbol in symbols:
                try:
                    location = symbol.get("location", {})
                    uri = location.get("uri", "")
                    range_info = location.get("range", {})
                    start = range_info.get("start", {})
                    
                    result = {
                        "name": symbol.get("name", ""),
                        "kind": symbol.get("kind", 0),
                        "kind_name": self._symbol_kind_to_name(symbol.get("kind", 0)),
                        "container_name": symbol.get("containerName", ""),
                        "file": uri.replace("file://", "") if uri else "",
                        "line": start.get("line", 0) + 1,  # Convert to 1-based
                        "column": start.get("character", 0) + 1,  # Convert to 1-based
                        "location": location
                    }
                    results.append(result)
                    
                except Exception as e:
                    logger.debug(f"Error processing symbol: {e}")
                    continue
                    
            logger.debug(f"Found {len(results)} workspace symbols for query: {query}")
            return results
            
        except Exception as e:
            logger.error(f"Error in workspace symbols search: {e}")
            return []
    
    def _symbol_kind_to_name(self, kind: int) -> str:
        """Convert LSP SymbolKind enum to human-readable name"""
        kind_names = {
            1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
            6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
            11: "Interface", 12: "Function", 13: "Variable", 14: "Constant", 15: "String",
            16: "Number", 17: "Boolean", 18: "Array", 19: "Object", 20: "Key",
            21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event", 25: "Operator",
            26: "TypeParameter"
        }
        return kind_names.get(kind, f"Unknown({kind})")
        
    async def _send_message(self, message: Dict[str, Any]):
        """Send JSON-RPC message to clangd"""
        
        content = json.dumps(message, separators=(',', ':'))
        header = f"Content-Length: {len(content)}\r\n\r\n"
        data = (header + content).encode('utf-8')
        
        self.clangd_manager.process.stdin.write(data)
        await self.clangd_manager.process.stdin.drain()
        
    async def _read_messages(self):
        """Read and process messages from clangd"""
        
        buffer = b""
        
        try:
            while not self._shutdown_event.is_set():
                if not self.clangd_manager.process or not self.clangd_manager.process.is_alive:
                    logger.error("Clangd process died during message reading")
                    break
                    
                # Read data
                try:
                    data = await asyncio.wait_for(
                        self.clangd_manager.process.stdout.read(4096),
                        timeout=1.0
                    )
                    if not data:
                        logger.warning("No data received from clangd stdout")
                        break
                except asyncio.TimeoutError:
                    continue
                    
                buffer += data
                
                # Parse messages
                while b"\r\n\r\n" in buffer:
                    try:
                        # Find header end
                        header_end = buffer.find(b"\r\n\r\n")
                        header = buffer[:header_end].decode('utf-8')
                        
                        # Parse Content-Length
                        content_length = 0
                        for line in header.split('\r\n'):
                            if line.startswith('Content-Length:'):
                                content_length = int(line.split(':', 1)[1].strip())
                                break
                                
                        if content_length == 0:
                            logger.error(f"Invalid header: {header}")
                            buffer = buffer[header_end + 4:]
                            continue
                            
                        # Check if we have the full message
                        message_start = header_end + 4
                        message_end = message_start + content_length
                        
                        if len(buffer) >= message_end:
                            content = buffer[message_start:message_end].decode('utf-8')
                            buffer = buffer[message_end:]
                            
                            # Parse and handle message
                            try:
                                message = json.loads(content)
                                asyncio.create_task(self._handle_message(message))
                            except json.JSONDecodeError as e:
                                logger.error(f"Invalid JSON from clangd: {e}")
                                logger.debug(f"Content: {content[:200]}...")
                        else:
                            break
                            
                    except Exception as e:
                        logger.error(f"Error parsing message: {e}")
                        # Skip this message and continue
                        buffer = buffer[header_end + 4:]
                        
        except Exception as e:
            if not self._shutdown_event.is_set():
                log_error_with_context(logger, e, {"operation": "message_reading"})
                
    async def _handle_message(self, message: Dict[str, Any]):
        """Handle incoming LSP message"""
        
        try:
            if "id" in message and "method" not in message:
                # Response to our request
                await self._handle_response(message)
            elif "method" in message:
                # Notification or request from server
                await self._handle_notification(message)
            else:
                logger.warning(f"Unknown message format: {message}")
                
        except Exception as e:
            log_error_with_context(logger, e, {"message": message})
            
    async def _handle_response(self, message: Dict[str, Any]):
        """Handle response to our request"""
        
        request_id = str(message["id"])
        
        if request_id not in self.pending_requests:
            logger.warning(f"Received response for unknown request: {request_id}")
            return
            
        pending = self.pending_requests[request_id]
        del self.pending_requests[request_id]
        
        # Log performance
        elapsed_ms = pending.elapsed_ms
        logger.debug(f"LSP {pending.method} completed in {elapsed_ms:.2f}ms")
        
        if "error" in message:
            error = message["error"]
            error_msg = f"LSP error in {pending.method}: {error.get('message', 'Unknown error')}"
            pending.future.set_exception(RuntimeError(error_msg))
        else:
            pending.future.set_result(message.get("result"))
            
    async def _handle_notification(self, message: Dict[str, Any]):
        """Handle notification from server"""
        
        method = message.get("method")
        params = message.get("params", {})
        
        # Handle common notifications
        if method == "textDocument/publishDiagnostics":
            # Log diagnostics but don't act on them
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])
            if diagnostics:
                logger.debug(f"Diagnostics for {uri}: {len(diagnostics)} issues")
        elif method == "window/logMessage":
            # Log messages from clangd
            level = params.get("type", 1)
            message_text = params.get("message", "")
            if level == 1:  # Error
                logger.error(f"clangd: {message_text}")
            elif level == 2:  # Warning
                logger.warning(f"clangd: {message_text}")
            else:  # Info/Log
                logger.debug(f"clangd: {message_text}")
        elif method in ["$/progress", "window/workDoneProgress/create"]:
            # Progress notifications - ignore for now
            pass
        else:
            logger.debug(f"Unhandled notification: {method}")
            
        # Call registered handlers
        if method in self.notification_handlers:
            try:
                await self.notification_handlers[method](params)
            except Exception as e:
                logger.error(f"Error in notification handler for {method}: {e}")


def path_to_uri(path: Path) -> str:
    """Convert file path to LSP URI"""
    return f"file://{path.resolve()}"


def uri_to_path(uri: str) -> Path:
    """Convert LSP URI to file path"""
    if uri.startswith("file://"):
        return Path(uri[7:])
    return Path(uri)