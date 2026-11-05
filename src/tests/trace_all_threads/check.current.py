import runpy
import sys
from pathlib import Path

# Only the main thread is recorded, the workers run outside the trace
sys.argv += ["--current"]
runpy.run_path(str(Path(__file__).with_name("check.py")), run_name="__main__")
