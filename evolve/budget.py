"""
budget.py
Controla cuántas peticiones a Gemini se hicieron hoy, para no pasarse
del límite gratuito de Google AI Studio. Guarda el estado en un
archivo JSON versionado en el repo, así persiste entre corridas de
GitHub Actions.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

STATE_FILE = Path(__file__).parent / "budget_state.json"

DAILY_TARGET = 300    # objetivo de mensajes por día (cuota real: 500 RPD, dejamos margen amplio)
DAILY_HARD_CAP = 350  # tope duro (margen de seguridad antes del límite real de Google)


@dataclass
class BudgetState:
    day: str
    requests_used: int = 0

    def to_dict(self):
        return asdict(self)


def _today() -> str:
    return date.today().isoformat()


def load_state() -> BudgetState:
    if not STATE_FILE.exists():
        return BudgetState(day=_today())
    data = json.loads(STATE_FILE.read_text())
    state = BudgetState(**data)
    if state.day != _today():
        # nuevo día, resetear contador
        state = BudgetState(day=_today())
    return state


def save_state(state: BudgetState) -> None:
    STATE_FILE.write_text(json.dumps(state.to_dict(), indent=2))


def can_make_request(state: BudgetState) -> bool:
    return state.requests_used < DAILY_HARD_CAP


def register_request(state: BudgetState) -> BudgetState:
    state.requests_used += 1
    save_state(state)
    return state


def remaining_today(state: BudgetState) -> int:
    return max(0, DAILY_TARGET - state.requests_used)
