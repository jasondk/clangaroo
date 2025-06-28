"""
High-level LSP method implementations for C++ code intelligence
"""

import logging
import time
from pathlib import Path
from typing import List, Optional, Dict, Any

from .lsp_client import LSPClient, path_to_uri, uri_to_path
from .utils import PerformanceTimer
# from .type_hierarchy_methods import TypeHierarchyMethods  # TODO: Module not implemented yet


logger = logging.getLogger(__name__)


class DocumentManager:
    """Manages document state with clangd"""
    
    def __init__(self, lsp_client: LSPClient):
        self.lsp_client = lsp_client
        self.open_documents: Dict[Path, dict] = {}
        
    async def ensure_document_open(self, file_path: Path) -> bool:
        """Ensure document is open in clangd
        
        Args:
            file_path: Path to the file
            
        Returns:
            True if document was opened, False if already open
        """
        
        file_path = file_path.resolve()
        
        if file_path in self.open_documents:
            return False
            
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
            
        # Read file content
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            # Try with latin-1 for binary files
            with open(file_path, 'r', encoding='latin-1') as f:
                content = f.read()
                
        # Determine language ID
        suffix = file_path.suffix.lower()
        if suffix in ['.cpp', '.cc', '.cxx', '.c++']:
            language_id = 'cpp'
        elif suffix in ['.c']:
            language_id = 'c'
        elif suffix in ['.h', '.hpp', '.hxx', '.h++']:
            # Could be C or C++, let clangd decide
            language_id = 'cpp'
        else:
            language_id = 'cpp'  # Default to C++
            
        # Send didOpen notification
        await self.lsp_client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": path_to_uri(file_path),
                "languageId": language_id,
                "version": 1,
                "text": content
            }
        })
        
        self.open_documents[file_path] = {
            "version": 1,
            "language_id": language_id,
            "size": len(content)
        }
        
        logger.debug(f"Opened document: {file_path}")
        return True
        
    async def close_document(self, file_path: Path):
        """Close document in clangd"""
        
        file_path = file_path.resolve()
        
        if file_path not in self.open_documents:
            return
            
        await self.lsp_client.notify("textDocument/didClose", {
            "textDocument": {
                "uri": path_to_uri(file_path)
            }
        })
        
        del self.open_documents[file_path]
        logger.debug(f"Closed document: {file_path}")
        
    async def close_all_documents(self):
        """Close all open documents"""
        
        for file_path in list(self.open_documents.keys()):
            await self.close_document(file_path)


class LSPMethods:
    """High-level LSP method implementations"""
    
    def __init__(self, lsp_client: LSPClient):
        self.lsp_client = lsp_client
        self.document_manager = DocumentManager(lsp_client)
        # self.type_hierarchy = TypeHierarchyMethods(lsp_client, self.document_manager)  # TODO: Module not implemented yet
        self.type_hierarchy = None
        
        # AI components (initialized later if AI features are enabled)
        self.llm_provider = None
        self.ai_summary_cache = None
        self.context_provider = None
    
    async def initialize_ai_features(self, config):
        """Initialize AI features if enabled
        
        Args:
            config: Configuration object with AI settings
        """
        if not getattr(config, 'ai_enabled', False):
            return
            
        try:
            # Import AI modules
            from .ai_cache import EnhancedAISummaryCache
            from .context_provider import ContextProvider
            from .providers import GeminiFlashProvider
            
            # Initialize AI cache
            if hasattr(config, 'ai_cache_db_path'):
                self.ai_summary_cache = EnhancedAISummaryCache(config.ai_cache_db_path, config)
                await self.ai_summary_cache.initialize()
                logger.info("AI summary cache initialized")
            
            # Initialize context provider
            self.context_provider = ContextProvider(config, self)
            logger.info("Context provider initialized")
            
            # Initialize LLM provider
            if hasattr(config, 'ai_api_key') and config.ai_api_key:
                provider_type = getattr(config, 'ai_provider', 'gemini-flash')
                if provider_type.startswith('gemini'):
                    self.llm_provider = GeminiFlashProvider(
                        api_key=config.ai_api_key,
                        model=provider_type
                    )
                    logger.info(f"LLM provider initialized: {provider_type}")
                else:
                    logger.warning(f"Unknown AI provider: {provider_type}")
            else:
                logger.warning("AI enabled but no API key provided")
                
        except Exception as e:
            logger.error(f"Failed to initialize AI features: {e}")
            # Disable AI components on error
            self.llm_provider = None
            self.ai_summary_cache = None
            self.context_provider = None
        
        # Propagate AI components to type hierarchy
        # self._update_type_hierarchy_ai_components()  # TODO: Module not implemented yet
        
    async def get_definition(self, file: str, line: int, column: int) -> Optional[List[Dict[str, Any]]]:
        """Get symbol definition
        
        Args:
            file: File path
            line: Line number (1-based)
            column: Column number (1-based)
            
        Returns:
            List of definition locations or None
        """
        
        file_path = Path(file).resolve()
        
        logger.debug(f"get_definition called for {file}:{line}:{column}")
        logger.debug(f"Resolved file path: {file_path}")
        
        with PerformanceTimer("get_definition", logger, file=str(file_path)):
            # Ensure document is open
            logger.debug(f"Ensuring document is open: {file_path}")
            await self.document_manager.ensure_document_open(file_path)
            logger.debug(f"Document opened successfully")
            
            # Send definition request with better error handling
            try:
                logger.debug(f"Sending definition request for {file_path}:{line}:{column}")
                result = await self.lsp_client.request("textDocument/definition", {
                    "textDocument": {"uri": path_to_uri(file_path)},
                    "position": {"line": line - 1, "character": column - 1}
                }, timeout=5.0)  # Increased timeout from 2.0 to 5.0
                logger.debug(f"Definition request completed successfully")
            except Exception as e:
                logger.error(f"Definition request failed: {e}")
                raise
            
            if not result:
                return None
                
            # Normalize result (can be single location or array)
            locations = result if isinstance(result, list) else [result]
            
            # Convert to our format
            definitions = []
            for location in locations:
                if "uri" in location and "range" in location:
                    def_path = uri_to_path(location["uri"])
                    definitions.append({
                        "file": str(def_path),
                        "line": location["range"]["start"]["line"] + 1,
                        "column": location["range"]["start"]["character"] + 1,
                        "end_line": location["range"]["end"]["line"] + 1,
                        "end_column": location["range"]["end"]["character"] + 1
                    })
                    
            return definitions if definitions else None
            
    async def get_references(self, file: str, line: int, column: int, include_declaration: bool = True) -> List[Dict[str, Any]]:
        """Get all references to symbol
        
        Args:
            file: File path
            line: Line number (1-based) 
            column: Column number (1-based)
            include_declaration: Whether to include declaration
            
        Returns:
            List of reference locations
        """
        
        file_path = Path(file).resolve()
        
        with PerformanceTimer("get_references", logger, file=str(file_path)):
            # Ensure document is open
            await self.document_manager.ensure_document_open(file_path)
            
            # Send references request
            result = await self.lsp_client.request("textDocument/references", {
                "textDocument": {"uri": path_to_uri(file_path)},
                "position": {"line": line - 1, "character": column - 1},
                "context": {"includeDeclaration": include_declaration}
            }, timeout=3.0)
            
            if not result:
                return []
                
            # Convert to our format and cap at 150 results
            references = []
            for i, location in enumerate(result[:150]):
                if "uri" in location and "range" in location:
                    ref_path = uri_to_path(location["uri"])
                    ref_line = location["range"]["start"]["line"]
                    
                    # Get line preview
                    preview = await self._get_line_preview(ref_path, ref_line)
                    
                    references.append({
                        "file": str(ref_path),
                        "line": ref_line + 1,
                        "column": location["range"]["start"]["character"] + 1,
                        "end_line": location["range"]["end"]["line"] + 1,
                        "end_column": location["range"]["end"]["character"] + 1,
                        "preview": preview
                    })
                    
            logger.debug(f"Found {len(references)} references (capped at 150)")
            return references
            
    async def get_hover(self, file: str, line: int, column: int, 
                       summarize: bool = False, context_level: str = "minimal") -> Optional[Dict[str, Any]]:
        """Get hover information with optional AI summarization
        
        Args:
            file: File path
            line: Line number (1-based)
            column: Column number (1-based)
            summarize: Whether to generate AI summary
            context_level: Context level for AI summarization ("minimal", "local", "full")
            
        Returns:
            Hover information with optional AI summary or None
        """
        
        file_path = Path(file).resolve()
        
        with PerformanceTimer("get_hover", logger, file=str(file_path)):
            # Ensure document is open  
            await self.document_manager.ensure_document_open(file_path)
            
            # Send hover request
            result = await self.lsp_client.request("textDocument/hover", {
                "textDocument": {"uri": path_to_uri(file_path)},
                "position": {"line": line - 1, "character": column - 1}
            }, timeout=2.0)
            
            if not result or not result.get("contents"):
                return None
                
            contents = result["contents"]
            
            # Extract information from hover contents
            type_info = ""
            documentation = ""
            
            if isinstance(contents, dict):
                if contents.get("kind") == "markdown":
                    documentation = contents["value"]
                    type_info = self._extract_type_from_markdown(documentation)
                elif contents.get("kind") == "plaintext":
                    documentation = contents["value"]
                    type_info = documentation.split('\n')[0] if documentation else ""
            elif isinstance(contents, str):
                documentation = contents
                type_info = contents.split('\n')[0] if contents else ""
            elif isinstance(contents, list):
                # Array of marked strings
                parts = []
                for item in contents:
                    if isinstance(item, dict) and "value" in item:
                        parts.append(item["value"])
                    elif isinstance(item, str):
                        parts.append(item)
                documentation = "\n".join(parts)
                type_info = parts[0] if parts else ""
            else:
                documentation = str(contents)
                type_info = documentation.split('\n')[0] if documentation else ""
                
            hover_result = {
                "type": type_info.strip(),
                "documentation": documentation.strip(),
                "range": result.get("range", {})
            }
            
            # Add AI summarization if requested
            if summarize and hasattr(self, 'ai_summary_cache') and hasattr(self, 'llm_provider'):
                hover_result = await self._add_ai_summary(hover_result, file, line, column, context_level)
            
            return hover_result
            
    async def get_incoming_calls(self, file: str, line: int, column: int,
                                analyze: bool = False, analysis_level: str = "summary",
                                group_by_pattern: bool = True, context_level: str = "local",
                                depth: Optional[int] = None) -> Dict[str, Any]:
        """Get incoming calls (callers) with optional AI analysis
        
        Args:
            file: File path
            line: Line number (1-based)
            column: Column number (1-based)
            analyze: Whether to perform AI analysis of call patterns
            analysis_level: Level of AI analysis ("summary" or "detailed")
            group_by_pattern: Whether to group calls by programming patterns
            context_level: Context level for AI analysis ("minimal", "local", "full")
            depth: Maximum depth for recursive call hierarchy traversal
            
        Returns:
            Dictionary with call locations and optional AI analysis
        """
        
        file_path = Path(file).resolve()
        
        with PerformanceTimer("get_incoming_calls", logger, file=str(file_path)):
            # Use config default if depth not specified
            if depth is None:
                config = getattr(self.lsp_client, 'clangd_manager', None)
                if config and hasattr(config, 'config'):
                    depth = getattr(config.config, 'call_hierarchy_max_depth', 3)
                else:
                    depth = 3
            
            logger.debug(f"Using call hierarchy depth: {depth}")
            
            # Get recursive incoming calls
            calls = await self._get_recursive_incoming_calls(file, line, column, depth)
            
            # Build result with call data
            result = {
                "calls": calls,
                "total_found": len(calls),
                "target_function": self._extract_function_name_from_position(file, line, column),
                "analysis_enabled": analyze
            }
            
            # Add AI analysis if requested
            if analyze and calls and hasattr(self, 'llm_provider') and self.llm_provider:
                result = await self._add_call_analysis(result, file, line, column, 
                                                      "incoming", analysis_level, group_by_pattern, context_level)
            
            return result
            
    async def get_outgoing_calls(self, file: str, line: int, column: int,
                                analyze: bool = False, analysis_level: str = "summary",
                                show_flow: bool = True, context_level: str = "local", 
                                depth: Optional[int] = None) -> Dict[str, Any]:
        """Get outgoing calls (callees) with optional AI analysis
        
        Args:
            file: File path
            line: Line number (1-based)
            column: Column number (1-based)
            analyze: Whether to perform AI analysis of call flow
            analysis_level: Level of AI analysis ("summary" or "detailed") 
            show_flow: Whether to show logical execution flow analysis
            context_level: Context level for AI analysis ("minimal", "local", "full")
            depth: Maximum depth for recursive call hierarchy traversal
            
        Returns:
            Dictionary with call locations and optional AI analysis
        """
        
        file_path = Path(file).resolve()
        
        with PerformanceTimer("get_outgoing_calls", logger, file=str(file_path)):
            # Use config default if depth not specified
            if depth is None:
                config = getattr(self.lsp_client, 'clangd_manager', None)
                if config and hasattr(config, 'config'):
                    depth = getattr(config.config, 'call_hierarchy_max_depth', 3)
                else:
                    depth = 3
            
            logger.debug(f"Using call hierarchy depth: {depth}")
            
            # Get recursive outgoing calls
            calls = await self._get_recursive_outgoing_calls(file, line, column, depth)
            
            # Build result with call data
            result = {
                "calls": calls,
                "total_found": len(calls),
                "source_function": self._extract_function_name_from_position(file, line, column),
                "analysis_enabled": analyze
            }
            
            # Add AI analysis if requested
            if analyze and calls and hasattr(self, 'llm_provider') and self.llm_provider:
                result = await self._add_call_analysis(result, file, line, column, 
                                                      "outgoing", analysis_level, show_flow, context_level)
            
            return result
            
    def _extract_type_from_markdown(self, markdown: str) -> str:
        """Extract type information from markdown hover text"""
        
        lines = markdown.split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('```cpp') or line.startswith('```c'):
                continue
            if line.startswith('```') and not line.startswith('```cpp') and not line.startswith('```c'):
                continue
            if line == '```':
                continue
            if line and not line.startswith('#') and not line.startswith('*'):
                # This looks like type information
                return line
                
        # Fallback: return first non-empty line
        for line in lines:
            line = line.strip()
            if line and not line.startswith('```') and not line.startswith('#'):
                return line
                
        return ""
        
    async def _get_line_preview(self, file_path: Path, line_number: int) -> str:
        """Get a preview of a specific line
        
        Args:
            file_path: Path to the file
            line_number: Line number (0-based)
            
        Returns:
            Line content or empty string if not found
        """
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                if 0 <= line_number < len(lines):
                    return lines[line_number].strip()
        except (IOError, UnicodeDecodeError):
            pass
            
        return ""
    
    async def _add_ai_summary(self, hover_result: Dict[str, Any], file: str, line: int, 
                             column: int, context_level: str) -> Dict[str, Any]:
        """Add AI summary to hover result if applicable
        
        Args:
            hover_result: Original hover result from clangd
            file: File path
            line: Line number
            column: Column number
            context_level: Context level for AI
            
        Returns:
            Enhanced hover result with AI summary
        """
        try:
            # Check if AI features are available
            if not self.context_provider or not self.llm_provider or not self.ai_summary_cache:
                logger.debug("AI features not available, returning original hover result")
                return hover_result
                
            # Get context data
            context_data = await self.context_provider.get_context(file, line, column, context_level)
            
            # Check if we should summarize
            if not self.llm_provider.should_summarize(context_data):
                return hover_result
            
            # Generate cache key
            cache_key = self.ai_summary_cache._generate_cache_key(file, line, column, context_level, context_data)
            
            # Check cache first
            cached_summary = await self.ai_summary_cache.get_by_key(cache_key)
            if cached_summary:
                return self._add_summary_to_result(hover_result, cached_summary, context_data)
            
            # Generate new summary
            summary_response = await self.llm_provider.summarize_with_context(context_data)
            if summary_response:
                # Cache the result
                await self.ai_summary_cache.store_with_key(cache_key, summary_response, context_data)
                return self._add_summary_to_result(hover_result, summary_response, context_data)
                
        except Exception as e:
            logger.warning(f"AI summarization failed: {e}")
        
        # Return original result if AI fails
        return hover_result
    
    def _add_summary_to_result(self, hover_result: Dict, summary: 'SummaryResponse', 
                              context_data: 'ContextData') -> Dict:
        """Add AI summary to hover result with context info
        
        Args:
            hover_result: Original hover result
            summary: AI summary response
            context_data: Context data used for summarization
            
        Returns:
            Enhanced hover result
        """
        result = hover_result.copy()
        result["ai_summary"] = {
            "summary": summary.summary,
            "tokens_used": summary.tokens_used,
            "provider": summary.provider,
            "cached": summary.cached,
            "context_level": context_data.context_level,
            "context_source": context_data.source
        }
        return result
    
    async def _add_call_analysis(self, result: Dict, file: str, line: int, column: int,
                                analysis_type: str, analysis_level: str, extra_param: bool, context_level: str = "local") -> Dict:
        """Add AI call analysis to result
        
        Args:
            result: Original call hierarchy result
            file: File path
            line: Line number
            column: Column number
            analysis_type: "incoming" or "outgoing"
            analysis_level: "summary" or "detailed"
            extra_param: group_by_pattern for incoming, show_flow for outgoing
            context_level: Context level for AI analysis ("minimal", "local", "full")
            
        Returns:
            Enhanced result with AI analysis
        """
        try:
            # Import AI classes
            from .llm_provider import CallAnalysisRequest
            import hashlib
            import json
            
            target_function = result.get("target_function") or result.get("source_function", "unknown")
            
            # Generate hash of call hierarchy for cache key
            calls_data = json.dumps(result["calls"], sort_keys=True)
            calls_hash = hashlib.md5(calls_data.encode()).hexdigest()
            
            # Check cache first
            if self.ai_summary_cache:
                cached_analysis = await self.ai_summary_cache.get_call_analysis(
                    target_function, file, line, column, analysis_type, analysis_level, calls_hash
                )
                if cached_analysis:
                    result["ai_analysis"] = {
                        "summary": cached_analysis.analysis_summary,
                        "architectural_insights": cached_analysis.architectural_insights,
                        "data_flow_analysis": cached_analysis.data_flow_analysis,
                        "performance_notes": cached_analysis.performance_notes,
                        "patterns": [
                            {
                                "type": pattern.pattern_type,
                                "description": pattern.description,
                                "confidence": pattern.confidence
                            } for pattern in cached_analysis.patterns
                        ],
                        "tokens_used": cached_analysis.tokens_used,
                        "provider": cached_analysis.provider,
                        "cached": True,
                        "analysis_level": analysis_level,
                        "analysis_type": analysis_type
                    }
                    return result
            
            # Get context data for enhanced AI analysis
            context_data = None
            if hasattr(self, 'context_provider') and self.context_provider:
                try:
                    context_data = await self.context_provider.get_context(file, line, column, context_level)
                    logger.debug(f"Got context data for AI analysis: {context_level} level")
                except Exception as e:
                    logger.warning(f"Failed to get context data: {e}")
            
            # Create analysis request
            request = CallAnalysisRequest(
                target_function=target_function,
                target_file=file,
                target_line=line,
                target_column=column,
                calls=result["calls"],
                analysis_level=analysis_level,
                analysis_type=analysis_type
            )
            
            # Get AI analysis with context
            analysis_response = await self.llm_provider.analyze_call_hierarchy(request, context_data)
            if analysis_response:
                # Store in cache
                if self.ai_summary_cache:
                    await self.ai_summary_cache.store_call_analysis(
                        target_function, file, line, column, analysis_type, 
                        analysis_level, calls_hash, analysis_response
                    )
                result["ai_analysis"] = {
                    "summary": analysis_response.analysis_summary,
                    "architectural_insights": analysis_response.architectural_insights,
                    "data_flow_analysis": analysis_response.data_flow_analysis,
                    "performance_notes": analysis_response.performance_notes,
                    "patterns": [
                        {
                            "type": pattern.pattern_type,
                            "description": pattern.description,
                            "confidence": pattern.confidence
                        } for pattern in analysis_response.patterns
                    ],
                    "tokens_used": analysis_response.tokens_used,
                    "provider": analysis_response.provider,
                    "cached": analysis_response.cached,
                    "analysis_level": analysis_level,
                    "analysis_type": analysis_type
                }
                
        except Exception as e:
            logger.warning(f"Call analysis failed: {e}")
        
        return result
    
    def _extract_function_name_from_position(self, file: str, line: int, column: int) -> str:
        """Extract function name from file position (best effort)
        
        Args:
            file: File path
            line: Line number
            column: Column number
            
        Returns:
            Function name or "unknown"
        """
        try:
            file_path = Path(file)
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            
            if 0 <= line - 1 < len(lines):
                target_line = lines[line - 1]
                # Simple heuristic: look for function-like patterns
                import re
                
                # Look for patterns like "functionName(" or "Class::method("
                patterns = [
                    r'(\w+)\s*\(',                    # functionName(
                    r'(\w+::\w+)\s*\(',              # Class::method(
                    r'(\w+)\s*=.*\(',                # var = function(
                    r'return\s+(\w+)\s*\(',          # return function(
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, target_line)
                    if match:
                        return match.group(1)
                
                # Fallback: extract any identifier near the column position
                words = re.findall(r'\w+', target_line)
                if words:
                    return words[0]
            
        except Exception as e:
            logger.debug(f"Failed to extract function name: {e}")
        
        return "unknown"
    
    async def _get_recursive_incoming_calls(self, file: str, line: int, column: int, max_depth: int) -> List[Dict[str, Any]]:
        """Get incoming calls recursively up to max_depth
        
        Args:
            file: File path
            line: Line number (1-based)
            column: Column number (1-based)
            max_depth: Maximum recursion depth
            
        Returns:
            List of call locations with depth information
        """
        if max_depth <= 0:
            logger.debug(f"Max depth {max_depth} <= 0, returning empty list")
            return []
        
        file_path = Path(file).resolve()
        logger.debug(f"Getting recursive incoming calls for {file_path}:{line}:{column} with max_depth={max_depth}")
        
        # Ensure document is open
        await self.document_manager.ensure_document_open(file_path)
        
        # First, prepare call hierarchy
        prepare_result = await self.lsp_client.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": path_to_uri(file_path)},
            "position": {"line": line - 1, "character": column - 1}
        }, timeout=2.0)
        
        if not prepare_result:
            logger.debug("No call hierarchy items found in prepare step")
            return []
        
        logger.debug(f"Found {len(prepare_result)} call hierarchy items")
        
        all_calls = []
        visited = set()  # Track visited functions to avoid infinite recursion
        
        # Get config values safely
        config = getattr(self.lsp_client, 'clangd_manager', None)
        if config and hasattr(config, 'config'):
            max_calls_per_level = getattr(config.config, 'call_hierarchy_max_per_level', 25)
            max_total_calls = getattr(config.config, 'call_hierarchy_max_calls', 100)
        else:
            max_calls_per_level = 25
            max_total_calls = 100
        
        logger.debug(f"Using limits: max_calls_per_level={max_calls_per_level}, max_total_calls={max_total_calls}")
        
        async def _get_calls_at_depth(items, current_depth):
            logger.debug(f"Processing {len(items)} items at depth {current_depth}")
            
            if current_depth >= max_depth:
                logger.debug(f"Reached max depth {max_depth}, stopping")
                return
            
            items_to_recurse = []
            
            for item in items[:max_calls_per_level]:  # Limit items per level
                item_key = f"{item.get('uri', '')}:{item.get('range', {}).get('start', {}).get('line', 0)}"
                if item_key in visited:
                    logger.debug(f"Already visited {item_key}, skipping")
                    continue
                visited.add(item_key)
                
                try:
                    incoming = await self.lsp_client.request("callHierarchy/incomingCalls", {
                        "item": item
                    }, timeout=3.0)
                    
                    if incoming:
                        logger.debug(f"Found {len(incoming)} incoming calls for item at depth {current_depth}")
                        for call in incoming[:max_calls_per_level]:  # Limit calls per item
                            from_info = call["from"]
                            ranges = call["fromRanges"]
                            
                            # Add each call range
                            for range_info in ranges[:5]:  # Limit ranges per call
                                caller_path = uri_to_path(from_info["uri"])
                                call_data = {
                                    "name": from_info["name"],
                                    "file": str(caller_path),
                                    "line": from_info["range"]["start"]["line"] + 1,
                                    "column": from_info["range"]["start"]["character"] + 1,
                                    "end_line": from_info["range"]["end"]["line"] + 1,
                                    "end_column": from_info["range"]["end"]["character"] + 1,
                                    "detail": from_info.get("detail", ""),
                                    "kind": from_info.get("kind", 0),
                                    "call_line": range_info["start"]["line"] + 1,
                                    "call_column": range_info["start"]["character"] + 1,
                                    "depth": current_depth + 1
                                }
                                all_calls.append(call_data)
                                
                                # Check global limit
                                if len(all_calls) >= max_total_calls:
                                    logger.debug(f"Reached max total calls limit {max_total_calls}")
                                    return
                                
                            # Collect items for next level recursion
                            if current_depth + 1 < max_depth:
                                items_to_recurse.append(from_info)
                    else:
                        logger.debug(f"No incoming calls found for item at depth {current_depth}")
                                        
                except Exception as e:
                    logger.warning(f"Error getting incoming calls at depth {current_depth}: {e}")
                    continue
            
            # Recurse to next depth level
            if items_to_recurse and current_depth + 1 < max_depth and len(all_calls) < max_total_calls:
                await _get_calls_at_depth(items_to_recurse, current_depth + 1)
        
        # Start recursive traversal
        await _get_calls_at_depth(prepare_result, 0)
        
        logger.debug(f"Found total of {len(all_calls)} calls across all depths")
        
        # Sort by depth, then by file/line
        all_calls.sort(key=lambda x: (x.get('depth', 0), x['file'], x['line']))
        
        # Apply global limit
        return all_calls[:max_total_calls]
    
    async def _get_recursive_outgoing_calls(self, file: str, line: int, column: int, max_depth: int) -> List[Dict[str, Any]]:
        """Get outgoing calls recursively up to max_depth
        
        Args:
            file: File path
            line: Line number (1-based)
            column: Column number (1-based)
            max_depth: Maximum recursion depth
            
        Returns:
            List of call locations with depth information
        """
        if max_depth <= 0:
            logger.debug(f"Max depth {max_depth} <= 0, returning empty list")
            return []
        
        file_path = Path(file).resolve()
        logger.debug(f"Getting recursive outgoing calls for {file_path}:{line}:{column} with max_depth={max_depth}")
        
        # Ensure document is open
        await self.document_manager.ensure_document_open(file_path)
        
        # First, prepare call hierarchy
        prepare_result = await self.lsp_client.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": path_to_uri(file_path)},
            "position": {"line": line - 1, "character": column - 1}
        }, timeout=2.0)
        
        if not prepare_result:
            logger.debug("No call hierarchy items found in prepare step")
            return []
        
        logger.debug(f"Found {len(prepare_result)} call hierarchy items")
        
        all_calls = []
        visited = set()  # Track visited functions to avoid infinite recursion
        
        # Get config values safely
        config = getattr(self.lsp_client, 'clangd_manager', None)
        if config and hasattr(config, 'config'):
            max_calls_per_level = getattr(config.config, 'call_hierarchy_max_per_level', 25)
            max_total_calls = getattr(config.config, 'call_hierarchy_max_calls', 100)
        else:
            max_calls_per_level = 25
            max_total_calls = 100
        
        logger.debug(f"Using limits: max_calls_per_level={max_calls_per_level}, max_total_calls={max_total_calls}")
        
        async def _get_calls_at_depth(items, current_depth):
            logger.debug(f"Processing {len(items)} items at depth {current_depth}")
            
            if current_depth >= max_depth:
                logger.debug(f"Reached max depth {max_depth}, stopping")
                return
            
            items_to_recurse = []
            
            for item in items[:max_calls_per_level]:  # Limit items per level
                item_key = f"{item.get('uri', '')}:{item.get('range', {}).get('start', {}).get('line', 0)}"
                if item_key in visited:
                    logger.debug(f"Already visited {item_key}, skipping")
                    continue
                visited.add(item_key)
                
                try:
                    outgoing = await self.lsp_client.request("callHierarchy/outgoingCalls", {
                        "item": item
                    }, timeout=3.0)
                    
                    if outgoing:
                        logger.debug(f"Found {len(outgoing)} outgoing calls for item at depth {current_depth}")
                        for call in outgoing[:max_calls_per_level]:  # Limit calls per item
                            to_info = call["to"]
                            ranges = call["fromRanges"]
                            
                            # Add each call range
                            for range_info in ranges[:5]:  # Limit ranges per call
                                callee_path = uri_to_path(to_info["uri"])
                                call_data = {
                                    "name": to_info["name"],
                                    "file": str(callee_path),
                                    "line": to_info["range"]["start"]["line"] + 1,
                                    "column": to_info["range"]["start"]["character"] + 1,
                                    "end_line": to_info["range"]["end"]["line"] + 1,
                                    "end_column": to_info["range"]["end"]["character"] + 1,
                                    "detail": to_info.get("detail", ""),
                                    "kind": to_info.get("kind", 0),
                                    "call_line": range_info["start"]["line"] + 1,
                                    "call_column": range_info["start"]["character"] + 1,
                                    "depth": current_depth + 1
                                }
                                all_calls.append(call_data)
                                
                                # Check global limit
                                if len(all_calls) >= max_total_calls:
                                    logger.debug(f"Reached max total calls limit {max_total_calls}")
                                    return
                                
                            # Collect items for next level recursion
                            if current_depth + 1 < max_depth:
                                items_to_recurse.append(to_info)
                    else:
                        logger.debug(f"No outgoing calls found for item at depth {current_depth}")
                                        
                except Exception as e:
                    logger.warning(f"Error getting outgoing calls at depth {current_depth}: {e}")
                    continue
            
            # Recurse to next depth level
            if items_to_recurse and current_depth + 1 < max_depth and len(all_calls) < max_total_calls:
                await _get_calls_at_depth(items_to_recurse, current_depth + 1)
        
        # Start recursive traversal
        await _get_calls_at_depth(prepare_result, 0)
        
        logger.debug(f"Found total of {len(all_calls)} calls across all depths")
        
        # Sort by depth, then by file/line
        all_calls.sort(key=lambda x: (x.get('depth', 0), x['file'], x['line']))
        
        # Apply global limit
        return all_calls[:max_total_calls]    
    # Type Hierarchy Methods
    
    async def prepare_type_hierarchy(self, file: str, line: int, column: int):
        """Prepare type hierarchy items for a symbol"""
        # TODO: TypeHierarchyMethods not implemented yet
        return None

    async def get_supertypes(self, file: str, line: int, column: int,
                           analyze: bool = False, analysis_level: Optional[str] = None,
                           context_level: Optional[str] = None):
        """Get supertypes (base classes) for a type"""
        # Use config defaults if not specified
        if analysis_level is None:
            config = getattr(self.lsp_client, "clangd_manager", None)
            if config and hasattr(config, "config"):
                analysis_level = getattr(config.config, "ai_analysis_level", "summary")
            else:
                analysis_level = "summary"
        
        if context_level is None:
            config = getattr(self.lsp_client, "clangd_manager", None)
            if config and hasattr(config, "config"):
                context_level = getattr(config.config, "ai_context_level", "local")
            else:
                context_level = "local"
        
        # TODO: TypeHierarchyMethods not implemented yet
        return []

    async def get_subtypes(self, file: str, line: int, column: int,
                          analyze: bool = False, analysis_level: Optional[str] = None,
                          context_level: Optional[str] = None):
        """Get subtypes (derived classes) for a type"""
        # Use config defaults if not specified
        if analysis_level is None:
            config = getattr(self.lsp_client, "clangd_manager", None)
            if config and hasattr(config, "config"):
                analysis_level = getattr(config.config, "ai_analysis_level", "summary")
            else:
                analysis_level = "summary"
        
        if context_level is None:
            config = getattr(self.lsp_client, "clangd_manager", None)
            if config and hasattr(config, "config"):
                context_level = getattr(config.config, "ai_context_level", "local")
            else:
                context_level = "local"
        
        # TODO: TypeHierarchyMethods not implemented yet
        return []
    
    def _update_type_hierarchy_ai_components(self):
        """Update type hierarchy methods with AI components"""
        if self.type_hierarchy:
            self.type_hierarchy.llm_provider = self.llm_provider
            self.type_hierarchy.ai_summary_cache = self.ai_summary_cache
            self.type_hierarchy.context_provider = self.context_provider
