"""Where the app keeps its files. One place, so moving a module never breaks a path (sol-hud counted parent
folders from each file).  SOL_CONTROL_DATA overrides the data folder (tests, a second copy side by side)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]                     # the repo folder
DATA_DIR = Path(os.environ.get("SOL_CONTROL_DATA", ROOT / "data"))   # settings, logs, run history (gitignored)
WORKFLOWS = ROOT / "workflows"
