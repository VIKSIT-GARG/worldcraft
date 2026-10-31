"""Run FastAPI and Vite together, including inside a managed preview namespace."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
children=[]
def stop(*_):
    for p in children:
        if p.poll() is None:p.terminate()
    for p in children:
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:p.kill()
    sys.exit(0)
signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
try:
    children.append(subprocess.Popen([sys.executable,'-m','uvicorn','backend.main:app','--host','127.0.0.1','--port','8000'],cwd=root))
    children.append(subprocess.Popen(['node','node_modules/vite/bin/vite.js','--host','0.0.0.0',*sys.argv[1:]],cwd=root/'frontend'))
    while all(p.poll() is None for p in children):time.sleep(.25)
finally:stop()
