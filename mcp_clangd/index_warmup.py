"""
Index warmup functionality for faster clangd startup
"""

import asyncio
import logging
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .lsp_methods import LSPMethods
    from .config import Config

logger = logging.getLogger(__name__)


class IndexWarmup:
    """Intelligent index warmup for faster code intelligence"""
    
    def __init__(self, lsp_methods: 'LSPMethods', config: 'Config'):
        self.lsp_methods = lsp_methods
        self.config = config
        
    async def warmup_project(self) -> None:
        """Warm up index by strategically opening key files"""
        
        if not self.config.warmup:
            return
            
        logger.info(f"Starting index warmup (limit: {self.config.warmup_file_limit} files)...")
        
        try:
            # Find key files to open
            key_files = self.find_key_files()
            files_to_open = key_files[:self.config.warmup_file_limit]
            
            logger.info(f"Opening {len(files_to_open)} key files for warmup")
            
            # Open files in priority order with small delays
            opened_count = 0
            for file_path in files_to_open:
                try:
                    await self.lsp_methods.document_manager.ensure_document_open(file_path)
                    opened_count += 1
                    
                    # Small delay to avoid overwhelming clangd
                    await asyncio.sleep(0.1)
                    
                    # Log progress
                    if opened_count % 5 == 0 or opened_count == len(files_to_open):
                        logger.info(f"Warmup progress: {opened_count}/{len(files_to_open)} files opened")
                        
                except Exception as e:
                    logger.debug(f"Could not open {file_path} during warmup: {e}")
                    continue
                    
            logger.info(f"Index warmup completed: {opened_count} files opened")
            
        except Exception as e:
            logger.error(f"Index warmup failed: {e}")
            
    def find_key_files(self) -> List[Path]:
        """Find the most important files to open for indexing
        
        Prioritizes files likely to be referenced by many others:
        1. Main entry points (main.cpp, main.c)
        2. Public headers (include/**/*.h)
        3. Common headers (*.h, *.hpp)
        4. Implementation files (*.cpp, *.cc, *.c)
        
        Returns:
            List of file paths in priority order
        """
        
        project_root = self.config.project_root
        key_files: List[Path] = []
        
        # Priority 1: Entry points
        entry_patterns = [
            "main.cpp", "main.c", "main.cc",
            "src/main.cpp", "src/main.c",
            "app.cpp", "app.c"
        ]
        
        for pattern in entry_patterns:
            matches = list(project_root.glob(pattern))
            key_files.extend(matches)
            
        # Priority 2: Public headers (likely most referenced)
        public_header_patterns = [
            "include/**/*.h",
            "include/**/*.hpp", 
            "include/**/*.hxx",
            "inc/**/*.h",
            "headers/**/*.h"
        ]
        
        for pattern in public_header_patterns:
            matches = list(project_root.glob(pattern))
            key_files.extend(matches)
            
        # Priority 3: Common headers in project root and src
        header_patterns = [
            "*.h", "*.hpp", "*.hxx",
            "src/*.h", "src/*.hpp", 
            "lib/*.h", "lib/*.hpp"
        ]
        
        for pattern in header_patterns:
            matches = list(project_root.glob(pattern))
            key_files.extend(matches)
            
        # Priority 4: Implementation files
        impl_patterns = [
            "*.cpp", "*.cc", "*.cxx", "*.c",
            "src/*.cpp", "src/*.cc", "src/*.c",
            "lib/*.cpp", "lib/*.cc", "lib/*.c"
        ]
        
        for pattern in impl_patterns:
            matches = list(project_root.glob(pattern))
            key_files.extend(matches)
            
        # Remove duplicates while preserving order
        seen = set()
        unique_files = []
        for file_path in key_files:
            if file_path not in seen and file_path.is_file():
                seen.add(file_path)
                unique_files.append(file_path)
                
        # Filter files that exist in compile_commands.json for relevance
        relevant_files = self._filter_by_compile_commands(unique_files)
        
        logger.debug(f"Found {len(relevant_files)} key files for warmup")
        return relevant_files
        
    def _filter_by_compile_commands(self, files: List[Path]) -> List[Path]:
        """Filter files to only include those in compile_commands.json"""
        
        try:
            import json
            
            with open(self.config.compile_db_path, 'r') as f:
                compile_db = json.load(f)
                
            # Extract file paths from compile commands
            compiled_files = set()
            for entry in compile_db:
                file_path = Path(entry.get("file", ""))
                if file_path.is_absolute():
                    compiled_files.add(file_path)
                else:
                    # Relative to directory
                    directory = Path(entry.get("directory", self.config.project_root))
                    compiled_files.add(directory / file_path)
                    
            # Filter to only include files that are compiled
            relevant_files = []
            for file_path in files:
                # Check exact match or if any compiled file is in the same directory tree
                if (file_path in compiled_files or 
                    any(str(file_path).startswith(str(cf.parent)) for cf in compiled_files)):
                    relevant_files.append(file_path)
                    
            return relevant_files
            
        except Exception as e:
            logger.debug(f"Could not filter by compile commands: {e}")
            # Return original list if filtering fails
            return files