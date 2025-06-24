"""
Clangd process management
"""

import asyncio
import subprocess
import logging
import signal
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from .config import Config
from .utils import log_error_with_context


logger = logging.getLogger(__name__)


@dataclass
class ClangdProcess:
    """Container for clangd process information"""
    
    process: asyncio.subprocess.Process
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    restart_count: int = 0
    start_time: float = 0.0
    
    @property
    def is_alive(self) -> bool:
        """Check if process is still running"""
        return self.process.returncode is None
        
    @property
    def uptime(self) -> float:
        """Get process uptime in seconds"""
        return time.time() - self.start_time


class ClangdManager:
    """Manages clangd process lifecycle"""
    
    def __init__(self, config: Config):
        self.config = config
        self.process: Optional[ClangdProcess] = None
        self.lock = asyncio.Lock()
        self.max_restarts = 3
        self.restart_delay = 1.0  # seconds
        self._stderr_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
    async def start(self) -> ClangdProcess:
        """Start clangd process
        
        Returns:
            ClangdProcess instance
            
        Raises:
            RuntimeError: If clangd fails to start
        """
        
        async with self.lock:
            if self.process and self.process.is_alive:
                logger.debug("Clangd already running")
                return self.process
                
            logger.info("Starting clangd process...")
            
            # Build command
            cmd = [self.config.clangd_path] + self.config.clangd_args
            logger.debug(f"Clangd command: {' '.join(cmd)}")
            
            try:
                # Start process
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.config.project_root,
                    # Ensure process dies with parent
                    preexec_fn=None if hasattr(signal, 'SIGHUP') else None
                )
                
                self.process = ClangdProcess(
                    process=process,
                    stdin=process.stdin,
                    stdout=process.stdout,
                    stderr=process.stderr,
                    start_time=time.time()
                )
                
                # Start monitoring tasks
                self._stderr_task = asyncio.create_task(self._read_stderr())
                self._health_task = asyncio.create_task(self._health_monitor())
                
                logger.info(f"Clangd started with PID {process.pid}")
                return self.process
                
            except Exception as e:
                log_error_with_context(
                    logger, e, 
                    {"command": cmd, "cwd": str(self.config.project_root)}
                )
                raise RuntimeError(f"Failed to start clangd: {e}")
                
    async def restart(self) -> ClangdProcess:
        """Restart clangd process
        
        Returns:
            New ClangdProcess instance
            
        Raises:
            RuntimeError: If max restart attempts exceeded
        """
        
        async with self.lock:
            if self.process and self.process.restart_count >= self.max_restarts:
                logger.error(f"Max restart attempts ({self.max_restarts}) reached")
                raise RuntimeError("clangd keeps crashing - max restart attempts exceeded")
                
            restart_count = 0
            if self.process:
                restart_count = self.process.restart_count + 1
                logger.warning(f"Restarting clangd (attempt {restart_count}/{self.max_restarts})")
                await self._stop_internal()
            else:
                logger.info("Starting clangd after crash")
                
            # Wait before restart
            await asyncio.sleep(self.restart_delay)
            
            # Start new process
            await self.start()
            if self.process:
                self.process.restart_count = restart_count
                
            return self.process
            
    async def stop(self):
        """Stop clangd process gracefully"""
        
        logger.info("Stopping clangd process...")
        self._shutdown_event.set()
        
        async with self.lock:
            await self._stop_internal()
            
        logger.info("Clangd process stopped")
        
    async def _stop_internal(self):
        """Internal stop implementation"""
        
        if not self.process:
            return
            
        # Cancel monitoring tasks
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
            
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
            
        # Stop process
        if self.process.is_alive:
            try:
                # Send terminate signal
                self.process.process.terminate()
                
                # Wait for graceful shutdown
                try:
                    await asyncio.wait_for(self.process.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Clangd didn't terminate gracefully, killing...")
                    self.process.process.kill()
                    await self.process.process.wait()
                    
            except ProcessLookupError:
                # Process already dead
                pass
                
        self.process = None
        
    async def _read_stderr(self):
        """Read and log stderr from clangd"""
        
        if not self.process:
            return
            
        try:
            while not self._shutdown_event.is_set():
                line = await self.process.stderr.readline()
                if not line:
                    break
                    
                stderr_text = line.decode('utf-8', errors='replace').strip()
                if stderr_text:
                    # Filter out noise, log important messages
                    if any(keyword in stderr_text.lower() for keyword in ['error', 'fatal', 'crash']):
                        logger.error(f"clangd stderr: {stderr_text}")
                    elif 'warning' in stderr_text.lower():
                        logger.warning(f"clangd stderr: {stderr_text}")
                    else:
                        logger.debug(f"clangd stderr: {stderr_text}")
                        
        except Exception as e:
            if not self._shutdown_event.is_set():
                logger.error(f"Error reading clangd stderr: {e}")
                
    async def _health_monitor(self):
        """Monitor clangd health and restart if needed"""
        
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(5.0)  # Check every 5 seconds
                
                if not self.process:
                    continue
                    
                if not self.process.is_alive:
                    logger.error(f"Clangd process died (uptime: {self.process.uptime:.1f}s)")
                    try:
                        await self.restart()
                    except RuntimeError as e:
                        logger.error(f"Failed to restart clangd: {e}")
                        break
                        
        except Exception as e:
            if not self._shutdown_event.is_set():
                logger.error(f"Health monitor error: {e}")
                
    async def health_check(self) -> dict:
        """Get health status
        
        Returns:
            Dictionary with health information
        """
        
        if not self.process:
            return {
                "status": "stopped",
                "pid": None,
                "uptime": 0,
                "restart_count": 0
            }
            
        return {
            "status": "running" if self.process.is_alive else "dead",
            "pid": self.process.process.pid,
            "uptime": self.process.uptime,
            "restart_count": self.process.restart_count,
            "max_restarts": self.max_restarts
        }