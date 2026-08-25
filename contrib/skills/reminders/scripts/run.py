"""Cron entry point: session "none" jobs invoke the skill's run action.
Delegates to remind.py check."""
import os
import subprocess
import sys

result = subprocess.run(
    [sys.executable, os.path.join(os.path.dirname(__file__), "remind.py"), "check"],
    capture_output=True, text=True, timeout=60,
)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
