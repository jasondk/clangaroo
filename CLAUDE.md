# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is **Clangaroo** 🦘 - a Python-based MCP (Model Context Protocol) server that bridges clangd's LSP (Language Server Protocol) capabilities to provide Claude with fast, reliable C++ code intelligence. The name combines "clangd" + "kangaroo" representing the ability to jump around C++ codebases efficiently.

## Key Architecture Components

1. **MCP Server** (`mcp_clangd/server.py`) - Handles MCP protocol, tool registration, and request routing
2. **Clangd Manager** (`mcp_clangd/clangd_manager.py`) - Spawns, monitors, and manages clangd processes
3. **LSP Client** (`mcp_clangd/lsp_client.py`) - Communicates with clangd using Language Server Protocol
4. **Cache Layer** (`mcp_clangd/cache.py`) - SQLite-based caching for sub-50ms response times
5. **Configuration** (`mcp_clangd/config.py`) - CLI argument parsing and settings

## Development Commands

### Setup and Installation
```bash
# Install in development mode
pip install -e .

# Install dependencies only
pip install mcp>=1.2.0 pygls>=1.3.0 aiosqlite>=0.19.0
```

### Running the Server
```bash
# Basic usage (new command name)
clangaroo --project /path/to/cpp/project

# With custom clangd path
clangaroo --project /path/to/cpp/project --clangd-path /usr/local/bin/clangd

# Enable verbose logging
clangaroo --project /path/to/cpp/project --log-level debug

# Legacy command still works
mcp-clangd --project /path/to/cpp/project
```

### Testing
```bash
# Run unit tests
pytest tests/

# Run integration tests with real C++ project
pytest tests/integration/ --project /path/to/test/project

# Run performance benchmarks
pytest tests/benchmarks/ -v
```

### Code Quality
```bash
# Format code
black mcp_clangd/ tests/

# Type checking
mypy mcp_clangd/

# Linting
ruff check mcp_clangd/
```

## Critical Implementation Details

### MCP Tool Interfaces
Clangaroo exposes 7 core tools via MCP:

**Discovery Tools:**
- `cpp_list_files` - List C++ source files in the project
- `cpp_search_symbols` - Search for symbols by name using LSP + text fallback

**Analysis Tools:**
- `cpp_definition` - Find symbol definitions
- `cpp_references` - Find all references to a symbol
- `cpp_hover` - Get type info and documentation
- `cpp_incoming_calls` - Find callers of a function
- `cpp_outgoing_calls` - Find what a function calls

Discovery tools enable exploration, while analysis tools require precise `file`, `line`, and `column` parameters.

### Performance Requirements
- Definition/Hover: Lightning-fast response times
- References/Call hierarchy: Fast response times for complex queries
- Cache all responses with 24-hour TTL
- Invalidate cache on file changes

### Clangd Configuration
The service starts clangd with these arguments:
```python
[
    "--background-index",
    "--header-insertion=never",
    "--clang-tidy=false",
    "--completion-style=detailed",
    "--pch-storage=memory",
    "--malloc-trim"
]
```

### Error Handling Strategy
1. Auto-restart clangd on crash (max 3 attempts)
2. Return partial results after 500ms timeout
3. Log cache errors but continue without cache
4. Return empty results for missing symbols (no fallbacks)

### LSP Protocol Details
- Uses stdio transport (not HTTP/SSE)
- Handles async notifications (diagnostics, progress)
- Manages document synchronization
- Requires proper initialization handshake

## Project Requirements

For the MCP service to work with a C++ project:
1. `compile_commands.json` must exist in project root
2. clangd 16+ must be available in PATH
3. Python 3.10+ required
4. ~2GB RAM for large projects

## Future Considerations

The PRD mentions potential AI-powered summarization as a v2 feature. When implementing:
- Add optional `summarize` parameter to `cpp_hover` tool
- Cache AI summaries separately with 7-day TTL
- Keep provider interface simple initially (not LangChain)
- Ensure graceful fallback to raw docs if AI fails