"""Put ``dashboard/`` on sys.path so tests import plugin modules directly."""
import sys
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))
