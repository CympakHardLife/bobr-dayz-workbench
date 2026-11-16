"""Portable Workbench entrypoint, using this Python interpreter."""
from pathlib import Path
import runpy
import sys
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools/workbench"))
runpy.run_path(str(ROOT / "tools/workbench/cli.py"), run_name="__main__")
