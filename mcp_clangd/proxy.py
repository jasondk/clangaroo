# clangaroo/mcp_clangd/proxy.py
import asyncio
import sys
import subprocess
import os
from pathlib import Path
import logging

from .utils import project_socket_path, is_socket_active

logger = logging.getLogger(__name__)

class StdioProxy:
    """
    A client-side proxy that forwards stdio to the project's daemon via a
    Unix socket. It ensures the daemon is running and attempts to reconnect
    if the connection is lost.
    """
    def __init__(self, project_root: Path, config: dict):
        self.project_root = project_root
        self.config = config
        self.socket_path = project_socket_path(project_root)

    async def run(self) -> None:
        """Main loop to connect to the daemon and forward I/O."""
        while True:
            try:
                await self._ensure_daemon()
                logger.info(f"Connecting to daemon at {self.socket_path}")
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                logger.info("Connection established. Forwarding stdio.")

                # Create async stream reader for stdin
                loop = asyncio.get_event_loop()
                stdin_reader = asyncio.StreamReader()
                stdin_protocol = asyncio.StreamReaderProtocol(stdin_reader)
                await loop.connect_read_pipe(lambda: stdin_protocol, sys.stdin)

                # Create two tasks for bidirectional forwarding.
                stdin_task = asyncio.create_task(self._forward_stdin(stdin_reader, writer))
                stdout_task = asyncio.create_task(self._forward_stdout(reader))
                
                done, pending = await asyncio.wait(
                    {stdin_task, stdout_task},
                    return_when=asyncio.FIRST_COMPLETED
                )

                for task in pending:
                    task.cancel() # Clean up the other task.

                writer.close()
                await writer.wait_closed()
                
                # If stdin closed, it's a clean exit.
                if stdin_task in done:
                    logger.info("Stdin closed. Proxy shutting down.")
                    break # Exit the while loop
                else: # Otherwise, the socket broke.
                    raise ConnectionResetError("Daemon connection lost.")

            except (ConnectionRefusedError, ConnectionResetError, FileNotFoundError):
                logger.warning("Daemon connection failed. Retrying in 2 seconds...")
                await asyncio.sleep(2) # Wait before attempting to reconnect.
            except Exception as e:
                logger.critical(f"Proxy encountered an unrecoverable error: {e}", exc_info=True)
                sys.exit(1)

    async def _forward_stdin(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Forward data from stdin to the daemon."""
        try:
            while True:
                data = await reader.read(8192)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _forward_stdout(self, reader: asyncio.StreamReader) -> None:
        """Forward data from the daemon to stdout."""
        try:
            while True:
                data = await reader.read(8192)
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        except Exception as e:
            logger.error(f"Error forwarding stdout: {e}")

    async def _ensure_daemon(self) -> None:
        """Starts the daemon process if it's not already active."""
        if is_socket_active(self.socket_path):
            return

        logger.info("Daemon not found. Attempting to start...")
        cmd = [sys.executable, '-m', 'mcp_clangd', '--daemon', '--project', str(self.project_root)]
        
        # Pass through all relevant config options from proxy to daemon
        if self.config.get('clangd_path'):
            cmd.extend(['--clangd-path', self.config['clangd_path']])
        if self.config.get('log_level'):
            cmd.extend(['--log-level', self.config['log_level']])
        if self.config.get('cache_dir'):
            cmd.extend(['--cache-dir', str(self.config['cache_dir'])])
        if not self.config.get('cache_enabled', True):
            cmd.append('--no-cache')
        if self.config.get('lsp_timeout'):
            cmd.extend(['--timeout', str(self.config['lsp_timeout'])])
        if self.config.get('index_path'):
            cmd.extend(['--index-path', str(self.config['index_path'])])
        if self.config.get('warmup'):
            cmd.append('--warmup')
        if self.config.get('wait_for_index'):
            cmd.append('--wait-for-index')
        if self.config.get('index_timeout'):
            cmd.extend(['--index-timeout', str(self.config['index_timeout'])])
        if self.config.get('warmup_file_limit'):
            cmd.extend(['--warmup-limit', str(self.config['warmup_file_limit'])])
            
        # AI options
        if self.config.get('ai_enabled'):
            cmd.append('--ai-enabled')
        if self.config.get('ai_provider'):
            cmd.extend(['--ai-provider', self.config['ai_provider']])
        if self.config.get('ai_api_key'):
            cmd.extend(['--ai-api-key', self.config['ai_api_key']])
        if self.config.get('ai_cache_days'):
            cmd.extend(['--ai-cache-days', str(self.config['ai_cache_days'])])
        if self.config.get('ai_cost_limit_monthly'):
            cmd.extend(['--ai-cost-limit', str(self.config['ai_cost_limit_monthly'])])
        if self.config.get('ai_analysis_level'):
            cmd.extend(['--ai-analysis-level', self.config['ai_analysis_level']])
        if self.config.get('ai_context_level'):
            cmd.extend(['--ai-context-level', self.config['ai_context_level']])
            
        # Call hierarchy options
        if self.config.get('call_hierarchy_max_depth'):
            cmd.extend(['--call-hierarchy-depth', str(self.config['call_hierarchy_max_depth'])])
        if self.config.get('call_hierarchy_max_calls'):
            cmd.extend(['--call-hierarchy-max-calls', str(self.config['call_hierarchy_max_calls'])])
        if self.config.get('call_hierarchy_max_per_level'):
            cmd.extend(['--call-hierarchy-per-level', str(self.config['call_hierarchy_max_per_level'])])
            
        if self.config.get('debug'):
            cmd.append('--debug')
        
        # Start daemon process with error capture
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            
            # Wait for the daemon to start and the socket to become active.
            parent_exited = False
            for i in range(100): # 10-second timeout (increased)
                if is_socket_active(self.socket_path):
                    logger.info("Daemon started successfully.")
                    return
                
                # Check if process exited
                if not parent_exited and proc.poll() is not None:
                    stdout, stderr = proc.communicate()
                    
                    # Exit code 0 is expected when daemon forks successfully
                    if proc.returncode == 0:
                        # Parent process exited after fork, keep checking for socket
                        logger.info("Parent process exited after fork, continuing to wait for daemon socket...")
                        parent_exited = True
                        # Log any stderr from parent before fork
                        if stderr.strip():
                            logger.debug(f"Parent stderr before fork: {stderr}")
                    else:
                        # Actual error
                        logger.error(f"Daemon process exited with code {proc.returncode}")
                        if stdout:
                            logger.error(f"Daemon stdout: {stdout}")
                        if stderr:
                            logger.error(f"Daemon stderr: {stderr}")
                        raise RuntimeError(f"Daemon failed to start: exit code {proc.returncode}")
                
                # Log progress every second
                if i % 10 == 0 and i > 0:
                    logger.debug(f"Still waiting for daemon socket... ({i/10}s)")
                    
                await asyncio.sleep(0.1)
            
            # Timeout - check if process is still running
            if proc.poll() is None:
                proc.terminate()
                stdout, stderr = proc.communicate()
                if stderr:
                    logger.error(f"Daemon stderr after timeout: {stderr}")
            
            # Check if daemon log exists to get more info
            daemon_log = '/tmp/clangaroo-daemon-startup.log'
            if os.path.exists(daemon_log):
                with open(daemon_log, 'r') as f:
                    log_content = f.read()
                    logger.error(f"Daemon startup log:\n{log_content}")
            
            raise ConnectionRefusedError("Timed out waiting for daemon to start.")
        except Exception as e:
            logger.error(f"Failed to start daemon: {e}")
            raise