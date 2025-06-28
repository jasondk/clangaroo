#!/usr/bin/env python3
"""
Wrapper to capture daemon startup errors and log them properly
"""
import sys
import subprocess
import os
from pathlib import Path

def main():
    # Get the command line arguments
    args = sys.argv[1:]
    
    # Find project path
    project_idx = args.index('--project') if '--project' in args else -1
    if project_idx == -1 or project_idx + 1 >= len(args):
        print("Error: --project argument required", file=sys.stderr)
        sys.exit(1)
    
    project_path = args[project_idx + 1]
    
    # Log to stderr that we're starting
    print(f"Starting daemon for project: {project_path}", file=sys.stderr)
    
    # Run the actual daemon
    cmd = [sys.executable, '-m', 'mcp_clangd'] + args
    
    try:
        # Run with captured output
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"Daemon failed with exit code {result.returncode}", file=sys.stderr)
            if result.stdout:
                print("STDOUT:", file=sys.stderr)
                print(result.stdout, file=sys.stderr)
            if result.stderr:
                print("STDERR:", file=sys.stderr) 
                print(result.stderr, file=sys.stderr)
            sys.exit(result.returncode)
            
    except Exception as e:
        print(f"Failed to start daemon: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()