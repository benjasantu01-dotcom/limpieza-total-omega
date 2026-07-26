"""
chain.py
Decide si la cadena auto-disparada debe continuar.

Por qué existe: el cron de GitHub Actions no es confiable (en la práctica
descartó casi todas las corridas programadas), así que el bucle se
re-dispara a sí mismo para tener un latido propio. Eso es potente pero
peligroso: una cadena que se auto-alimenta puede quedar girando sola.
Este módulo es el freno.

Frenos que aplica:
  1. Tope duro de eslabones por día (MAX_LINKS_PER_DAY). Si se alcanza,
     la cadena se detiene y el cron queda como reinicio de respaldo.
  2. Interruptor manual: si existe el archivo STOP_FILE en la raíz del
     repo, la cadena no sigue. Sirve para frenarla haciendo un commit,
     sin tocar la configuración de Actions.

Uso: `python evolve/chain.py` imprime "go" o "stop" y escribe el motivo
en stderr. El workflow usa esa salida para decidir si re-dispara.
"""

from __future__ import annotations
import json
import sys
from datetime import date
from pathlib import Path

EVOLVE_DIR = Path(__file__).resolve().parent
ROOT = EVOLVE_DIR.parent

CHAIN_FILE = EVOLVE_DIR / "chain_state.json"
STOP_FILE = ROOT / "STOP_CHAIN"

# Con un ciclo objetivo de ~10 minutos, un día son ~144 eslabones.
# 200 deja margen sin permitir que se descontrole.
MAX_LINKS_PER_DAY = 200


def _today() -> str:
    return date.today().isoformat()


def load_state() -> dict:
    if not CHAIN_FILE.exists():
        return {"day": _today(), "links": 0}
    try:
        state = json.loads(CHAIN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"day": _today(), "links": 0}
    if state.get("day") != _today():
        return {"day": _today(), "links": 0}
    return {"day": state.get("day", _today()), "links": int(state.get("links", 0))}


def save_state(state: dict) -> None:
    CHAIN_FILE.write_text(json.dumps(state, indent=2))


def decide() -> tuple[bool, str]:
    if STOP_FILE.exists():
        return False, f"freno manual activo: existe el archivo {STOP_FILE.name}"

    state = load_state()
    if state["links"] >= MAX_LINKS_PER_DAY:
        return False, (
            f"tope diario de la cadena alcanzado ({state['links']}/{MAX_LINKS_PER_DAY}). "
            "El cron queda como reinicio de respaldo."
        )

    state["links"] += 1
    save_state(state)
    return True, f"eslabon {state['links']}/{MAX_LINKS_PER_DAY} del dia {state['day']}"


def main() -> None:
    should_continue, reason = decide()
    print("go" if should_continue else "stop")
    print(reason, file=sys.stderr)


if __name__ == "__main__":
    main()
