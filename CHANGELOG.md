# Changelog

## [0.2.0] - 2024-12-28

### Fixed
- Fixed critical daemon startup errors that prevented the MCP server from initializing
- Fixed `TypeError: LLMProvider() takes no arguments` by removing incorrect instantiation of abstract class
- Fixed `TypeError: EnhancedAISummaryCache.__init__() missing 1 required positional argument` by proper initialization
- Fixed async/sync issues in daemon fork process
- Fixed file descriptor handling for proper daemon detachment
- Fixed proxy handling of parent process exit code 0

### Improved
- Enhanced error logging with file-based startup log at `/tmp/clangaroo-daemon-startup.log`
- Better daemon process management and error recovery
- Increased daemon startup timeout from 5 to 10 seconds for reliability

### Internal
- Refactored daemon and proxy implementation for better separation of concerns
- Improved event loop handling after fork operations
- Added comprehensive logging throughout the startup process

## [0.1.0] - 2024-01-XX

### Initial Release
- Core MCP server implementation for C++ code intelligence
- Integration with clangd LSP server
- Seven powerful tools for C++ code navigation and analysis
- Smart caching for sub-50ms response times
- AI-powered documentation summaries (optional)
- Tree-sitter integration for ultra-fast syntax analysis
- Support for Claude Desktop and Claude Code

### Features
- 🔍 Discovery tools: list files, search symbols
- 📊 Analysis tools: definitions, references, hover info, call hierarchies
- ⚡ Performance: SQLite caching, Tree-sitter parsing
- 🤖 AI enhancement: Optional Gemini-powered summaries
- 🚀 Easy setup: Single command installation
