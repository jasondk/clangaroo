"""
Tree-sitter manager for ultra-fast C++ syntax analysis
"""

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Any, Union
from dataclasses import dataclass
from enum import Enum
import hashlib

try:
    import tree_sitter
    import tree_sitter_cpp
    import tree_sitter_c
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    tree_sitter = None
    tree_sitter_cpp = None
    tree_sitter_c = None

from .utils import PerformanceTimer

logger = logging.getLogger(__name__)


class SymbolKind(Enum):
    """C++ symbol kinds identified by Tree-sitter"""
    FUNCTION = "function"
    CLASS = "class"
    STRUCT = "struct"
    ENUM = "enum"
    NAMESPACE = "namespace"
    VARIABLE = "variable"
    FIELD = "field"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    DESTRUCTOR = "destructor"
    TEMPLATE = "template"
    TYPEDEF = "typedef"
    MACRO = "macro"
    UNKNOWN = "unknown"


@dataclass
class SymbolInfo:
    """Information about a symbol found by Tree-sitter"""
    name: str
    kind: SymbolKind
    file_path: Path
    line: int  # 1-based
    column: int  # 1-based
    end_line: int  # 1-based
    end_column: int  # 1-based
    signature: Optional[str] = None
    scope: Optional[str] = None  # Namespace or class scope
    access_modifier: Optional[str] = None  # public, private, protected
    is_template: bool = False
    is_virtual: bool = False
    is_static: bool = False
    is_const: bool = False


@dataclass
class ContextBlock:
    """A semantically meaningful block of code"""
    content: str
    start_line: int  # 1-based
    end_line: int  # 1-based
    start_column: int  # 1-based
    end_column: int  # 1-based
    block_type: str  # "function", "class", "namespace", etc.
    symbol_name: Optional[str] = None
    parent_scope: Optional[str] = None


@dataclass 
class FunctionInfo:
    """Detailed information about a function"""
    name: str
    signature: str
    return_type: str
    parameters: List[Dict[str, str]]  # [{"name": "param", "type": "int"}]
    file_path: Path
    line: int
    column: int
    end_line: int
    end_column: int
    scope: Optional[str] = None
    is_template: bool = False
    is_virtual: bool = False
    is_static: bool = False
    is_const: bool = False
    is_constructor: bool = False
    is_destructor: bool = False


@dataclass
class ClassInfo:
    """Detailed information about a class or struct"""
    name: str
    kind: str  # "class" or "struct"
    file_path: Path
    line: int
    column: int
    end_line: int
    end_column: int
    scope: Optional[str] = None
    base_classes: List[str] = None
    is_template: bool = False
    access_modifier: str = "public"  # Default for structs


class TreeSitterManager:
    """Manages Tree-sitter parsers and syntax trees for ultra-fast C++ analysis"""
    
    def __init__(self, project_root: Path, max_file_size: int = 10 * 1024 * 1024):
        """Initialize Tree-sitter manager
        
        Args:
            project_root: Root directory of the C++ project
            max_file_size: Maximum file size to parse (in bytes)
        """
        self.project_root = project_root.resolve()
        self.max_file_size = max_file_size
        
        # Check if Tree-sitter is available
        if not TREE_SITTER_AVAILABLE:
            logger.warning("Tree-sitter not available. Install tree-sitter and tree-sitter-cpp.")
            self.available = False
            return
            
        self.available = True
        
        # Initialize C++ parser (skip C parser due to version incompatibility)
        self.cpp_parser = tree_sitter.Parser()
        
        try:
            self.cpp_parser.language = tree_sitter.Language(tree_sitter_cpp.language())
            logger.info("Tree-sitter C++ parser initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Tree-sitter C++ language: {e}")
            self.available = False
            return
            
        # Cache for parsed trees
        self.syntax_trees: Dict[Path, tree_sitter.Tree] = {}
        self.file_versions: Dict[Path, str] = {}  # File content hash
        self.file_mtimes: Dict[Path, float] = {}  # Modification times
        
        # Cache for extracted symbols
        self._symbol_cache: Dict[Path, List[SymbolInfo]] = {}
        self._function_cache: Dict[Path, List[FunctionInfo]] = {}
        self._class_cache: Dict[Path, List[ClassInfo]] = {}
        
        # Performance tracking
        self.parse_times: List[float] = []
        self.query_times: List[float] = []
        
        logger.info(f"TreeSitterManager initialized for project: {self.project_root}")
    
    def is_available(self) -> bool:
        """Check if Tree-sitter is available and working"""
        return self.available
    
    async def parse_file(self, file_path: Path) -> Optional[tree_sitter.Tree]:
        """Parse a C++ file and return syntax tree
        
        Args:
            file_path: Path to the C++ file to parse
            
        Returns:
            Parsed syntax tree or None if parsing failed
        """
        if not self.available:
            return None
            
        file_path = file_path.resolve()
        
        with PerformanceTimer("tree_sitter_parse", logger, file=str(file_path)):
            # Check if file exists and is readable
            if not file_path.exists() or not file_path.is_file():
                logger.debug(f"File not found or not a file: {file_path}")
                return None
                
            # Check file size
            if file_path.stat().st_size > self.max_file_size:
                logger.warning(f"File too large to parse: {file_path}")
                return None
                
            # Check if we need to reparse (file changed)
            current_mtime = file_path.stat().st_mtime
            if (file_path in self.syntax_trees and 
                file_path in self.file_mtimes and 
                self.file_mtimes[file_path] >= current_mtime):
                logger.debug(f"Using cached syntax tree for: {file_path}")
                return self.syntax_trees[file_path]
            
            # Read file content
            try:
                content = file_path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                # Try with latin-1 for files with unusual encoding
                try:
                    content = file_path.read_text(encoding='latin-1')
                except Exception as e:
                    logger.warning(f"Could not read file {file_path}: {e}")
                    return None
            except Exception as e:
                logger.warning(f"Could not read file {file_path}: {e}")
                return None
            
            # Choose appropriate parser
            parser = self._get_parser_for_file(file_path)
            if not parser:
                logger.debug(f"No suitable parser for file: {file_path}")
                return None
            
            # Parse the file
            start_time = time.time()
            try:
                tree = parser.parse(content.encode('utf-8'))
                parse_time = time.time() - start_time
                self.parse_times.append(parse_time)
                
                # Cache the result
                self.syntax_trees[file_path] = tree
                self.file_mtimes[file_path] = current_mtime
                self.file_versions[file_path] = hashlib.md5(content.encode('utf-8')).hexdigest()
                
                # Clear dependent caches
                self._clear_caches_for_file(file_path)
                
                logger.debug(f"Parsed {file_path} in {parse_time:.3f}s")
                return tree
                
            except Exception as e:
                logger.warning(f"Failed to parse {file_path}: {e}")
                return None
    
    def _get_parser_for_file(self, file_path: Path) -> Optional[tree_sitter.Parser]:
        """Get appropriate parser for a file based on extension"""
        suffix = file_path.suffix.lower()
        
        # Use C++ parser for all C/C++ files (C parser has version incompatibility)
        if suffix in ['.cpp', '.cc', '.cxx', '.c++', '.hpp', '.hxx', '.h++', '.c', '.h']:
            return self.cpp_parser
            
        # Default to C++ for unknown extensions
        if suffix in ['.tcc', '.inc']:  # Template implementation files
            return self.cpp_parser
            
        return None
    
    def _clear_caches_for_file(self, file_path: Path):
        """Clear cached data for a file when it's reparsed"""
        if file_path in self._symbol_cache:
            del self._symbol_cache[file_path]
        if file_path in self._function_cache:
            del self._function_cache[file_path]
        if file_path in self._class_cache:
            del self._class_cache[file_path]
    
    async def get_functions(self, file_path: Path) -> List[FunctionInfo]:
        """Extract all function definitions from a file
        
        Args:
            file_path: Path to the C++ file
            
        Returns:
            List of function information
        """
        if not self.available:
            return []
            
        file_path = file_path.resolve()
        
        # Check cache first
        if file_path in self._function_cache:
            current_mtime = file_path.stat().st_mtime if file_path.exists() else 0
            cached_mtime = self.file_mtimes.get(file_path, 0)
            if cached_mtime >= current_mtime:
                return self._function_cache[file_path]
        
        with PerformanceTimer("tree_sitter_functions", logger, file=str(file_path)):
            tree = await self.parse_file(file_path)
            if not tree:
                return []
            
            functions = []
            
            # Query for function definitions
            cpp_language = tree_sitter.Language(tree_sitter_cpp.language())
            query = cpp_language.query("""
                (function_definition
                    type: (_) @return_type
                    declarator: (function_declarator
                        declarator: (_) @name
                        parameters: (parameter_list) @params
                    )
                ) @function

                (function_definition
                    type: (_) @return_type
                    declarator: (pointer_declarator
                        declarator: (function_declarator
                            declarator: (_) @name
                            parameters: (parameter_list) @params
                        )
                    )
                ) @function
            """)
            
            captures = query.captures(tree.root_node)
            
            # Extract function nodes from captures
            function_nodes = captures.get("function", [])
            for node in function_nodes:
                func_info = self._extract_function_info(node, file_path)
                if func_info:
                    functions.append(func_info)
            
            # Cache the results
            self._function_cache[file_path] = functions
            
            logger.debug(f"Found {len(functions)} functions in {file_path}")
            return functions
    
    def _extract_function_info(self, node: tree_sitter.Node, file_path: Path) -> Optional[FunctionInfo]:
        """Extract detailed information from a function node"""
        try:
            # Get basic position info
            start_line = node.start_point[0] + 1
            start_column = node.start_point[1] + 1
            end_line = node.end_point[0] + 1
            end_column = node.end_point[1] + 1
            
            # Extract function name
            name_node = None
            return_type_node = None
            params_node = None
            
            for child in node.children:
                if child.type == "function_declarator":
                    # Find the function name
                    for subchild in child.children:
                        if subchild.type in ["identifier", "field_identifier", "qualified_identifier"]:
                            name_node = subchild
                        elif subchild.type == "parameter_list":
                            params_node = subchild
                elif child.type in ["primitive_type", "type_identifier", "qualified_identifier"]:
                    return_type_node = child
            
            if not name_node:
                return None
                
            name = name_node.text.decode('utf-8')
            return_type = return_type_node.text.decode('utf-8') if return_type_node else "void"
            
            # Extract parameters
            parameters = []
            if params_node:
                for param_child in params_node.children:
                    if param_child.type == "parameter_declaration":
                        param_info = self._extract_parameter_info(param_child)
                        if param_info:
                            parameters.append(param_info)
            
            # Build signature
            param_str = ", ".join([f"{p['type']} {p['name']}" for p in parameters])
            signature = f"{return_type} {name}({param_str})"
            
            # Detect function characteristics
            is_constructor = name.split("::")[-1] in [cls.split("::")[-1] for cls in self._get_enclosing_classes(node)]
            is_destructor = name.startswith("~")
            is_virtual = self._has_keyword(node, "virtual")
            is_static = self._has_keyword(node, "static")
            is_const = signature.endswith(" const")
            
            return FunctionInfo(
                name=name,
                signature=signature,
                return_type=return_type,
                parameters=parameters,
                file_path=file_path,
                line=start_line,
                column=start_column,
                end_line=end_line,
                end_column=end_column,
                scope=self._get_scope_for_node(node),
                is_constructor=is_constructor,
                is_destructor=is_destructor,
                is_virtual=is_virtual,
                is_static=is_static,
                is_const=is_const
            )
            
        except Exception as e:
            logger.warning(f"Failed to extract function info: {e}")
            return None
    
    def _extract_parameter_info(self, param_node: tree_sitter.Node) -> Optional[Dict[str, str]]:
        """Extract parameter information from a parameter declaration node"""
        try:
            param_type = ""
            param_name = ""
            
            for child in param_node.children:
                if child.type in ["primitive_type", "type_identifier", "qualified_identifier"]:
                    param_type = child.text.decode('utf-8')
                elif child.type == "identifier":
                    param_name = child.text.decode('utf-8')
                elif child.type == "pointer_declarator":
                    # Handle pointer parameters
                    param_type += "*"
                    for subchild in child.children:
                        if subchild.type == "identifier":
                            param_name = subchild.text.decode('utf-8')
            
            if param_type:
                return {"name": param_name or "unnamed", "type": param_type}
                
        except Exception as e:
            logger.debug(f"Failed to extract parameter info: {e}")
            
        return None
    
    def _has_keyword(self, node: tree_sitter.Node, keyword: str) -> bool:
        """Check if a node has a specific keyword modifier"""
        # This is a simplified check - in reality we'd need more sophisticated parsing
        try:
            text = node.text.decode('utf-8')
            return keyword in text.split()
        except:
            return False
    
    def _get_enclosing_classes(self, node: tree_sitter.Node) -> List[str]:
        """Get the list of enclosing classes for a node"""
        classes = []
        current = node.parent
        
        while current:
            if current.type in ["class_specifier", "struct_specifier"]:
                # Find class name
                for child in current.children:
                    if child.type == "type_identifier":
                        classes.append(child.text.decode('utf-8'))
                        break
            current = current.parent
            
        return list(reversed(classes))  # Outermost first
    
    def _get_scope_for_node(self, node: tree_sitter.Node) -> Optional[str]:
        """Get the full scope (namespace + class) for a node"""
        scope_parts = []
        current = node.parent
        
        while current:
            if current.type == "namespace_definition":
                # Find namespace name
                for child in current.children:
                    if child.type == "identifier":
                        scope_parts.append(child.text.decode('utf-8'))
                        break
            elif current.type in ["class_specifier", "struct_specifier"]:
                # Find class name
                for child in current.children:
                    if child.type == "type_identifier":
                        scope_parts.append(child.text.decode('utf-8'))
                        break
            current = current.parent
        
        return "::".join(reversed(scope_parts)) if scope_parts else None
    
    async def get_classes(self, file_path: Path) -> List[ClassInfo]:
        """Extract all class and struct definitions from a file
        
        Args:
            file_path: Path to the C++ file
            
        Returns:
            List of class information
        """
        if not self.available:
            return []
            
        file_path = file_path.resolve()
        
        # Check cache first
        if file_path in self._class_cache:
            current_mtime = file_path.stat().st_mtime if file_path.exists() else 0
            cached_mtime = self.file_mtimes.get(file_path, 0)
            if cached_mtime >= current_mtime:
                return self._class_cache[file_path]
        
        with PerformanceTimer("tree_sitter_classes", logger, file=str(file_path)):
            tree = await self.parse_file(file_path)
            if not tree:
                return []
            
            classes = []
            
            # Query for class and struct definitions
            cpp_language = tree_sitter.Language(tree_sitter_cpp.language())
            query = cpp_language.query("""
                (class_specifier
                    name: (type_identifier) @name
                ) @class

                (struct_specifier
                    name: (type_identifier) @name
                ) @struct
            """)
            
            captures = query.captures(tree.root_node)
            
            # Extract class and struct nodes from captures
            class_nodes = captures.get("class", [])
            struct_nodes = captures.get("struct", [])
            name_nodes = captures.get("name", [])
            
            # Process class and struct nodes
            for class_node in class_nodes + struct_nodes:
                # Find the corresponding name node for this class
                for name_node in name_nodes:
                    # Check if the name node is a child of this class node
                    if (name_node.start_point[0] >= class_node.start_point[0] and 
                        name_node.end_point[0] <= class_node.end_point[0]):
                        class_info = self._extract_class_info(class_node, name_node, file_path)
                        if class_info:
                            classes.append(class_info)
                        break
            
            # Cache the results
            self._class_cache[file_path] = classes
            
            logger.debug(f"Found {len(classes)} classes/structs in {file_path}")
            return classes
    
    def _extract_class_info(self, class_node: tree_sitter.Node, name_node: tree_sitter.Node, 
                           file_path: Path) -> Optional[ClassInfo]:
        """Extract detailed information from a class/struct node"""
        try:
            # Get basic position info
            start_line = class_node.start_point[0] + 1
            start_column = class_node.start_point[1] + 1
            end_line = class_node.end_point[0] + 1
            end_column = class_node.end_point[1] + 1
            
            name = name_node.text.decode('utf-8')
            kind = "class" if class_node.type == "class_specifier" else "struct"
            
            # Extract base classes (inheritance)
            base_classes = []
            for child in class_node.children:
                if child.type == "base_class_clause":
                    base_classes.extend(self._extract_base_classes(child))
            
            # Detect if this is a template class
            is_template = False
            parent = class_node.parent
            if parent and parent.type == "template_declaration":
                is_template = True
            
            return ClassInfo(
                name=name,
                kind=kind,
                file_path=file_path,
                line=start_line,
                column=start_column,
                end_line=end_line,
                end_column=end_column,
                scope=self._get_scope_for_node(class_node),
                base_classes=base_classes,
                is_template=is_template,
                access_modifier="public" if kind == "struct" else "private"
            )
            
        except Exception as e:
            logger.warning(f"Failed to extract class info: {e}")
            return None
    
    def _extract_base_classes(self, base_clause_node: tree_sitter.Node) -> List[str]:
        """Extract base class names from a base class clause"""
        base_classes = []
        
        for child in base_clause_node.children:
            if child.type in ["type_identifier", "qualified_identifier"]:
                base_classes.append(child.text.decode('utf-8'))
        
        return base_classes
    
    async def extract_context_block(self, file_path: Path, line: int, 
                                  context_type: str = "auto") -> Optional[ContextBlock]:
        """Extract semantically meaningful context around a position
        
        Args:
            file_path: Path to the C++ file
            line: Line number (1-based)
            context_type: Type of context ("function", "class", "namespace", "auto")
            
        Returns:
            Context block or None if extraction failed
        """
        if not self.available:
            return None
            
        with PerformanceTimer("tree_sitter_context", logger, file=str(file_path)):
            tree = await self.parse_file(file_path)
            if not tree:
                return None
            
            # Convert line to 0-based for Tree-sitter
            target_line = line - 1
            
            # Find the node at the target position
            target_node = tree.root_node.descendant_for_point_range((target_line, 0), (target_line, 1000))
            if not target_node:
                return None
            
            # Walk up the tree to find the appropriate context block
            current = target_node
            while current:
                block_type = self._get_block_type(current)
                if block_type and (context_type == "auto" or block_type == context_type):
                    return self._create_context_block(current, file_path, block_type)
                current = current.parent
            
            return None
    
    def _get_block_type(self, node: tree_sitter.Node) -> Optional[str]:
        """Determine the block type for a node"""
        type_mapping = {
            "function_definition": "function",
            "method_definition": "function", 
            "class_specifier": "class",
            "struct_specifier": "class",
            "namespace_definition": "namespace",
            "template_declaration": "template",
            "enum_specifier": "enum"
        }
        
        return type_mapping.get(node.type)
    
    def _create_context_block(self, node: tree_sitter.Node, file_path: Path, 
                             block_type: str) -> ContextBlock:
        """Create a context block from a Tree-sitter node"""
        start_line = node.start_point[0] + 1
        start_column = node.start_point[1] + 1  
        end_line = node.end_point[0] + 1
        end_column = node.end_point[1] + 1
        
        content = node.text.decode('utf-8')
        
        # Extract symbol name
        symbol_name = None
        for child in node.children:
            if child.type in ["identifier", "type_identifier", "field_identifier"]:
                symbol_name = child.text.decode('utf-8')
                break
        
        return ContextBlock(
            content=content,
            start_line=start_line,
            end_line=end_line,
            start_column=start_column,
            end_column=end_column,
            block_type=block_type,
            symbol_name=symbol_name,
            parent_scope=self._get_scope_for_node(node)
        )
    
    async def find_symbols(self, pattern: str, file_pattern: str = "*", 
                          symbol_kinds: Optional[List[SymbolKind]] = None) -> List[SymbolInfo]:
        """Syntax-aware symbol search across project
        
        Args:
            pattern: Symbol name pattern (supports wildcards)
            file_pattern: File glob pattern to search in
            symbol_kinds: List of symbol kinds to search for
            
        Returns:
            List of matching symbols
        """
        if not self.available:
            return []
        
        # TODO: Implement efficient project-wide symbol search
        # This would involve:
        # 1. Finding all matching files using file_pattern
        # 2. Parsing each file with Tree-sitter
        # 3. Extracting symbols using Tree-sitter queries
        # 4. Filtering by pattern and symbol_kinds
        # 5. Returning sorted results
        
        logger.debug(f"Symbol search not yet implemented: {pattern}")
        return []
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics for Tree-sitter operations"""
        if not self.parse_times and not self.query_times:
            return {}
        
        return {
            "parse_times": {
                "count": len(self.parse_times),
                "avg": sum(self.parse_times) / len(self.parse_times) if self.parse_times else 0,
                "min": min(self.parse_times) if self.parse_times else 0,
                "max": max(self.parse_times) if self.parse_times else 0,
            },
            "query_times": {
                "count": len(self.query_times),
                "avg": sum(self.query_times) / len(self.query_times) if self.query_times else 0,
                "min": min(self.query_times) if self.query_times else 0,
                "max": max(self.query_times) if self.query_times else 0,
            },
            "cache_stats": {
                "trees_cached": len(self.syntax_trees),
                "symbols_cached": len(self._symbol_cache),
                "functions_cached": len(self._function_cache),
                "classes_cached": len(self._class_cache),
            }
        }
    
    def clear_caches(self):
        """Clear all caches (useful for testing or memory management)"""
        self.syntax_trees.clear()
        self.file_versions.clear()
        self.file_mtimes.clear()
        self._symbol_cache.clear()
        self._function_cache.clear()
        self._class_cache.clear()
        logger.info("Tree-sitter caches cleared")