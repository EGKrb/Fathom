import runpy
import sys
from pathlib import Path

# Same checks as check.py, for the blocked worker
sys.argv += ["--mode", "wait"]
runpy.run_path(str(Path(__file__).with_name("check.py")), run_name="__main__")
