"""Tri-model orchestrator: bus + account pool + spawner + merge queue, exposed to the Planner over MCP."""
import os
from pathlib import Path

ROOT = Path(os.environ.get("ORCH_ROOT") or Path(__file__).resolve().parents[1])
STATE = ROOT / ".orchestrator"
