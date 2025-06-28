# clangaroo/mcp_clangd/daemon.py
import asyncio
import signal
import os
from pathlib import Path
import logging
from typing import Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .session import ClientSession

from .backend import Backend
from .utils import project_socket_path, cleanup_stale_socket

logger = logging.getLogger(__name__)

class ClangarooDaemon:
    """
    Hosts the shared Backend and accepts client connections over a Unix socket.
    """
    def __init__(self, project_root: Path, config: dict):
        self.project_root = project_root
        self.config = config
        self.socket_path = project_socket_path(project_root)
        self.backend: Backend = Backend(project_root, config)
        self._server: Optional[asyncio.Server] = None
        self._sessions: Set["ClientSession"] = set()
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Starts the daemon, including the backend and the socket server."""
        # Log progress to file for debugging
        with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
            f.write("daemon.start() called\n")
            f.flush()
            
        # Ensure no stale socket from a previous unclean shutdown exists.
        cleanup_stale_socket(self.socket_path)

        with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
            f.write("Starting backend...\n")
            f.flush()
            
        await self.backend.start()

        with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
            f.write("Backend started, setting up signal handlers...\n")
            f.flush()

        # Set up signal handlers for graceful shutdown.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.shutdown(s)))

        with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
            f.write(f"Starting unix server on {self.socket_path}...\n")
            f.flush()

        self._server = await asyncio.start_unix_server(self._handle_client, path=self.socket_path)
        logger.info(f"Daemon listening on {self.socket_path}")
        
        with open('/tmp/clangaroo-daemon-startup.log', 'a') as f:
            f.write(f"Daemon listening successfully!\n")
            f.flush()

        # Wait until the shutdown event is set.
        await self._shutdown_event.wait()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Callback for handling a new client connection."""
        logger.info("New client connected.")
        from .session import ClientSession
        session = ClientSession(self.backend, reader, writer)
        self._sessions.add(session)
        try:
            await session.run()
        except Exception as e:
            logger.error(f"Error in client session: {e}", exc_info=True)
        finally:
            self._sessions.discard(session)
            logger.info("Client session ended.")

    async def shutdown(self, sig: Optional[signal.Signals] = None):
        """Performs a graceful shutdown of the daemon and its resources."""
        if self._shutdown_event.is_set():
            return
            
        logger.info(f"Shutdown initiated by signal {sig.name if sig else 'request'}...")

        # Stop accepting new connections.
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        
        # Close all active client sessions.
        for session in self._sessions:
            session.close()

        # Shut down the backend resources.
        if self.backend:
            await self.backend.shutdown()

        # Clean up the socket file.
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        logger.info("Daemon has shut down gracefully.")
        self._shutdown_event.set()