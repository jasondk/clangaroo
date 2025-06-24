"""
Context provider for real-time file access and AI summarization
"""

import re
import aiofiles
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

from .llm_provider import ContextData

logger = logging.getLogger(__name__)


class ContextProvider:
    """Provides real-time context for AI summarization"""
    
    def __init__(self, config, lsp_methods):
        self.config = config
        self.lsp_methods = lsp_methods
    
    async def get_context(self, file: str, line: int, column: int, 
                         context_level: str) -> ContextData:
        """Get context based on specified level
        
        Args:
            file: File path relative to project root
            line: Line number (1-based)
            column: Column number (1-based)
            context_level: "minimal", "local", or "full"
            
        Returns:
            ContextData with appropriate level of information
        """
        
        if context_level == "minimal":
            return await self._get_minimal_context(file, line, column)
        elif context_level == "local":
            return await self._get_local_context(file, line, column)
        elif context_level == "full":
            return await self._get_full_context(file, line, column)
        else:
            raise ValueError(f"Unknown context level: {context_level}")
    
    async def _get_minimal_context(self, file: str, line: int, column: int) -> ContextData:
        """Minimal context: clangd hover docs only"""
        hover_result = await self.lsp_methods.get_hover(file, line, column)
        
        contents = hover_result.get("contents", {})
        if isinstance(contents, dict):
            doc_text = contents.get("value", "")
        else:
            doc_text = str(contents)
        
        return ContextData(
            primary_content=doc_text,
            symbol_name=self._extract_symbol_name(doc_text),
            symbol_kind=self._extract_symbol_kind(doc_text),
            context_level="minimal",
            source="clangd_index"
        )
    
    async def _get_local_context(self, file: str, line: int, column: int) -> ContextData:
        """Local context: surrounding code + clangd docs"""
        # Get clangd documentation
        minimal_context = await self._get_minimal_context(file, line, column)
        
        # Read file for surrounding context
        file_content = await self._read_file_safely(file)
        if not file_content:
            return minimal_context
        
        # Extract local context
        local_info = self._extract_local_context(file_content, line, column)
        
        minimal_context.surrounding_code = local_info["surrounding_lines"]
        minimal_context.class_context = local_info["class_definition"]
        minimal_context.function_signature = local_info["function_signature"]
        minimal_context.context_level = "local"
        minimal_context.source = "live_file_and_index"
        
        return minimal_context
    
    async def _get_full_context(self, file: str, line: int, column: int) -> ContextData:
        """Full context: entire file + dependencies"""
        # Start with local context
        local_context = await self._get_local_context(file, line, column)
        
        # Read entire file
        file_content = await self._read_file_safely(file)
        if file_content:
            local_context.primary_content = file_content
            local_context.imports = self._extract_imports(file_content)
            local_context.related_headers = await self._get_related_headers(file, file_content)
        
        local_context.context_level = "full"
        local_context.source = "live_codebase"
        
        return local_context
    
    async def _read_file_safely(self, file_path: str) -> str:
        """Read file content with error handling"""
        try:
            full_path = self.config.project_root / file_path
            async with aiofiles.open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                return await f.read()
        except Exception as e:
            logger.warning(f"Failed to read {file_path}: {e}")
            return ""
    
    def _extract_local_context(self, content: str, line: int, column: int) -> Dict[str, str]:
        """Extract surrounding code context"""
        lines = content.split('\n')
        
        # Get surrounding lines (±25 lines)
        start = max(0, line - 25)
        end = min(len(lines), line + 25)
        surrounding = '\n'.join(lines[start:end])
        
        # Try to find class definition
        class_def = self._find_class_definition(lines, line)
        
        # Try to find function signature
        func_sig = self._find_function_signature(lines, line)
        
        return {
            "surrounding_lines": surrounding,
            "class_definition": class_def,
            "function_signature": func_sig
        }
    
    def _find_class_definition(self, lines: List[str], target_line: int) -> Optional[str]:
        """Find the class definition that contains the target line"""
        # Look backwards from target line for class definition
        for i in range(target_line - 1, max(0, target_line - 50), -1):
            line = lines[i].strip()
            if re.match(r'^\s*class\s+\w+', line):
                return line
        return None
    
    def _find_function_signature(self, lines: List[str], target_line: int) -> Optional[str]:
        """Find the function signature that contains the target line"""
        # Look backwards from target line for function definition
        for i in range(target_line - 1, max(0, target_line - 10), -1):
            line = lines[i].strip()
            # Simple function pattern matching
            if re.match(r'^\s*\w+.*\(.*\)\s*{?\s*$', line) and not line.startswith('//'):
                return line
        return None
    
    def _extract_imports(self, content: str) -> List[str]:
        """Extract #include statements"""
        includes = []
        for line in content.split('\n'):
            stripped = line.strip()
            if stripped.startswith('#include'):
                includes.append(stripped)
        return includes[:20]  # Limit to prevent explosion
    
    async def _get_related_headers(self, file: str, content: str) -> List[str]:
        """Get content of related header files"""
        includes = self._extract_imports(content)
        headers = []
        
        for include in includes[:5]:  # Limit to prevent too much context
            header_path = self._resolve_include_path(include, file)
            if header_path:
                header_content = await self._read_file_safely(str(header_path))
                if header_content:
                    headers.append(f"// {include}\n{header_content[:5000]}")  # Limit size
        
        return headers
    
    def _resolve_include_path(self, include_line: str, current_file: str) -> Optional[Path]:
        """Resolve #include path to actual file"""
        # Extract include filename
        match = re.search(r'#include\s*[<"](.*?)[>"]', include_line)
        if not match:
            return None
            
        include_name = match.group(1)
        
        # Try relative to current file directory
        current_dir = Path(current_file).parent
        relative_path = self.config.project_root / current_dir / include_name
        if relative_path.exists():
            return relative_path
        
        # Try relative to project root
        project_path = self.config.project_root / include_name
        if project_path.exists():
            return project_path
        
        # Try common include directories
        common_dirs = ["include", "src", "inc", "headers"]
        for dir_name in common_dirs:
            test_path = self.config.project_root / dir_name / include_name
            if test_path.exists():
                return test_path
        
        return None
    
    def _extract_symbol_name(self, doc_text: str) -> str:
        """Extract symbol name from clangd documentation"""
        # Try to find symbol name in various formats
        lines = doc_text.split('\n')
        for line in lines[:3]:  # Check first few lines
            # Look for patterns like "function: myFunc" or "class MyClass"
            if ':' in line:
                parts = line.split(':')
                if len(parts) >= 2:
                    symbol = parts[1].strip()
                    # Clean up common decorations
                    symbol = re.sub(r'\(.*\).*', '', symbol)  # Remove function params
                    symbol = symbol.split()[0]  # Take first word
                    if symbol and symbol.isidentifier():
                        return symbol
        
        # Fallback: try to extract any identifier-like string
        identifiers = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', doc_text)
        return identifiers[0] if identifiers else "unknown"
    
    def _extract_symbol_kind(self, doc_text: str) -> str:
        """Extract symbol kind from clangd documentation"""
        doc_lower = doc_text.lower()
        
        # Common patterns
        if 'function' in doc_lower or '(' in doc_text:
            return "function"
        elif 'class' in doc_lower:
            return "class"
        elif 'template' in doc_lower:
            return "template"
        elif 'macro' in doc_lower or '#define' in doc_text:
            return "macro"
        elif 'variable' in doc_lower or 'var' in doc_lower:
            return "variable"
        elif 'struct' in doc_lower:
            return "struct"
        elif 'enum' in doc_lower:
            return "enum"
        elif 'typedef' in doc_lower:
            return "typedef"
        else:
            return "symbol"