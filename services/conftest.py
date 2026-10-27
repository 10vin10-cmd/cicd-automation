import os
import sys
from pathlib import Path

# Services import their shared code as `shared`, with services/ on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("INVENTORY_DB_URL", "sqlite:///:memory:")
