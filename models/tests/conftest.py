"""Make the top-level sidecar modules importable from every pytest working directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
