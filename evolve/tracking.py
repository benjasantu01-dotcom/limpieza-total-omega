"""
tracking.py
Dos responsabilidades que antes faltaban en el bucle autónomo:

1. ROTACIÓN PERSISTENTE. Antes el bucle elegía qué archivo mejorar según
   la fecha de modificación (mtime). Eso funcionaba dentro de una misma
   corrida, pero se rompía entre corridas: `actions/checkout` reescribe
   todos los archivos con el mismo mtime, así que la "rotación" quedaba
   arbitraria y podía insistir siempre con el mismo archivo. Ahora
   guardamos un contador que nunca se resetea y recorremos
   sistemáticamente todas las combinaciones archivo × enfoque.

2. MÉTRICAS PARA EVALUACIÓN DIARIA. Cada iteración deja un registro
   estructurado en metrics.jsonl, y de ahí se regenera PROGRESS.md con
   los totales por día, por enfoque y por archivo. Sirve para puntuar el
   avance sin tener que leer cientos de líneas de log a mano.
"""

from __future__ import annotations
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

EVOLVE_DIR = Path(__file__).resolve().parent
ROOT = EVOLVE_DIR.parent

CYCLE_FILE = EVOLVE_DIR / "cycle_state.json"
METRICS_FILE = EVOLVE_DIR / "metrics.jsonl"
PROGRESS_FILE = ROOT / "PROGRESS.md"

# Etiquetas de resultado que se registran en las métricas.
RESULT_ACCEPTED = "aceptada"
RESULT_REJECTED_TESTS = "rechazada_tests"
RESULT_REJECTED_GUARD = "rechazada_guardia"
RESULT_NO_CHANGE = "sin_cambios"
RESULT_NO_RESPONSE = "sin_respuesta"


def next_iteration() -> int:
    """Devuelve el número de iteración global y lo incrementa en disco.

    A diferencia del presupuesto diario, este contador NO se resetea: es
    lo que garantiza que la rotación de archivo/enfoque siga avanzando
    aunque cambie el día o se reinicie el runner.
    """
    iteration = 0
    if CYCLE_FILE.exists():
        try:
            iteration = int(json.loads(CYCLE_FILE.read_text()).get("iteration", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            iteration = 0
    CYCLE_FILE.write_text(json.dumps({"iteration": iteration + 1}, indent=2))
    return iteration


def pick_assignment(iteration: int, files: list, categories: list) -> tuple:
    """Elige (archivo, enfoque) recorriendo la matriz completa sin repetir.

    Con 3 archivos y 6 enfoques hay 18 combinaciones: se cubren todas
    antes de volver a empezar, así ningún archivo se queda sin recibir
    todos los enfoques.
    """
    if not files or not categories:
        raise ValueError("Hacen falta al menos un archivo y un enfoque.")
    combo_index = iteration % (len(files) * len(categories))
    target = files[combo_index % len(files)]
    category = categories[combo_index // len(files)]
    return target, category


def record_metric(*, iteration: int, file_name: str, category: str, result: str, rationale: str) -> None:
    """Agrega una línea JSON con el resultado de la iteración."""
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "day": datetime.now().date().isoformat(),
        "iteration": iteration,
        "file": file_name,
        "category": category,
        "result": result,
        "rationale": rationale,
    }
    with METRICS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _load_metrics() -> list[dict]:
    if not METRICS_FILE.exists():
        return []
    entries = []
    for line in METRICS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # una línea corrupta no debe romper el reporte
    return entries


def regenerate_progress() -> None:
    """Reescribe PROGRESS.md con los totales acumulados.

    Es un resumen legible de un vistazo: cuántas mejoras se aceptaron y
    rechazaron cada día, cómo se reparte por enfoque y por archivo, y las
    últimas mejoras aceptadas con su justificación.
    """
    entries = _load_metrics()
    if not entries:
        return

    by_day: dict[str, Counter] = defaultdict(Counter)
    by_category: Counter = Counter()
    by_file: Counter = Counter()
    for e in entries:
        by_day[e.get("day", "?")][e.get("result", "?")] += 1
        if e.get("result") == RESULT_ACCEPTED:
            by_category[e.get("category", "?")] += 1
            by_file[e.get("file", "?")] += 1

    totals = Counter(e.get("result", "?") for e in entries)
    accepted = totals[RESULT_ACCEPTED]
    total = sum(totals.values())
    rate = f"{(accepted / total * 100):.1f}%" if total else "n/a"

    lines: list[str] = [
        "# Progreso del bucle autónomo",
        "",
        "Este archivo se regenera solo en cada corrida a partir de",
        "`evolve/metrics.jsonl`. No lo edites a mano.",
        "",
        "## Resumen general",
        "",
        f"- Iteraciones totales: **{total}**",
        f"- Mejoras aceptadas: **{accepted}** ({rate} de aceptación)",
        f"- Rechazadas por tests: {totals[RESULT_REJECTED_TESTS]}",
        f"- Rechazadas por guardia de seguridad: {totals[RESULT_REJECTED_GUARD]}",
        f"- Sin cambios (nada sustancial que mejorar): {totals[RESULT_NO_CHANGE]}",
        f"- Sin respuesta de la IA (error o límite): {totals[RESULT_NO_RESPONSE]}",
        "",
        "## Por día",
        "",
        "| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |",
        "|---|---|---|---|---|---|",
    ]
    for day in sorted(by_day):
        c = by_day[day]
        lines.append(
            f"| {day} | {c[RESULT_ACCEPTED]} | {c[RESULT_REJECTED_TESTS]} | "
            f"{c[RESULT_REJECTED_GUARD]} | {c[RESULT_NO_CHANGE]} | {c[RESULT_NO_RESPONSE]} |"
        )

    lines += ["", "## Mejoras aceptadas por enfoque", ""]
    if by_category:
        for cat, n in by_category.most_common():
            lines.append(f"- {cat}: **{n}**")
    else:
        lines.append("- (todavía sin mejoras aceptadas)")

    lines += ["", "## Mejoras aceptadas por archivo", ""]
    if by_file:
        for name, n in by_file.most_common():
            lines.append(f"- `{name}`: **{n}**")
    else:
        lines.append("- (todavía sin mejoras aceptadas)")

    recent = [e for e in entries if e.get("result") == RESULT_ACCEPTED][-15:]
    lines += ["", "## Últimas 15 mejoras aceptadas", ""]
    if recent:
        for e in reversed(recent):
            lines.append(
                f"- `{e.get('timestamp', '?')}` **{e.get('file', '?')}** "
                f"({e.get('category', '?')}): {e.get('rationale', '')}"
            )
    else:
        lines.append("- (todavía sin mejoras aceptadas)")

    lines.append("")
    PROGRESS_FILE.write_text("\n".join(lines), encoding="utf-8")
