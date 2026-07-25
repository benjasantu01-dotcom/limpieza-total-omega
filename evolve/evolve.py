"""
evolve.py — el "portero + mensajero" del bucle autónomo.

Cada corrida (disparada por GitHub Actions cada N minutos, ver
.github/workflows/evolve.yml):
  1. Chequea el presupuesto diario (no pasarse del límite gratis de Gemini).
  2. Lee la misión actual desde MISSION.md (la "guía" que vos escribís).
  3. Le pide a Gemini UNA mejora concreta y acotada a un archivo puntual.
  4. Aplica el cambio en un archivo temporal, corre los tests (pytest).
  5. Si pasan: reemplaza el archivo real y lo deja listo para commit.
     Si no pasan: descarta el cambio y lo deja logueado en evolve_log.md.
  6. Registra la petición usada en el presupuesto.

IMPORTANTE: este script SOLO edita archivos de texto dentro del repo.
Nunca ejecuta comandos de limpieza reales ni toca tu PC. Eso lo hacés
vos manualmente corriendo app/main.py cuando querés.
"""

from __future__ import annotations
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from budget import load_state, save_state, can_make_request, register_request, remaining_today

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
MISSION_FILE = ROOT / "MISSION.md"
LOG_FILE = ROOT / "evolve_log.md"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

ITERATIONS_PER_RUN = int(os.environ.get("ITERATIONS_PER_RUN", "5"))

# El nivel gratuito de Gemini limita peticiones POR MINUTO, no solo por
# día. Espaciamos cada llamada para no pasarnos (ajustable por si Google
# cambia el límite; 15s es conservador para la mayoría de los free tiers).
SECONDS_BETWEEN_REQUESTS = int(os.environ.get("SECONDS_BETWEEN_REQUESTS", "15"))
MAX_RETRIES_ON_RATE_LIMIT = 3

FILE_BLOCK_RE = re.compile(r"```(?:python)?\s*#\s*FILE:\s*(.+?)\n(.*?)```", re.DOTALL)


def log(message: str) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    line = f"- `{timestamp}` {message}\n"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    print(line.strip())


def read_mission() -> str:
    if not MISSION_FILE.exists():
        return "Mejorá la robustez y legibilidad del código existente sin cambiar su comportamiento."
    return MISSION_FILE.read_text(encoding="utf-8")


def list_editable_files() -> list[Path]:
    return sorted(p for p in APP_DIR.glob("*.py"))


def build_prompt(mission: str, target_file: Path) -> str:
    current_code = target_file.read_text(encoding="utf-8")
    return f"""Sos un colaborador de código cuidadoso trabajando en un proyecto Python real.

MISIÓN ACTUAL (definida por el dueño del proyecto):
{mission}

Tu tarea: proponer UNA SOLA mejora pequeña, segura y concreta al archivo
"{target_file.name}" de abajo. Reglas estrictas:
- NO cambies el comportamiento observable del código (misma funcionalidad).
- NO agregues borrado masivo de archivos ni nada destructivo.
- NO agregues dependencias nuevas.
- El código debe seguir siendo válido y ejecutable.
- Devolvé el archivo COMPLETO y modificado, en un único bloque así:

```python
# FILE: {target_file.name}
<contenido completo del archivo aca>
```

No agregues explicación fuera del bloque.

--- CONTENIDO ACTUAL DE {target_file.name} ---
{current_code}
"""


def call_gemini(prompt: str) -> str | None:
    if not GEMINI_API_KEY:
        log("ERROR: falta GEMINI_API_KEY en el entorno.")
        return None

    for attempt in range(1, MAX_RETRIES_ON_RATE_LIMIT + 1):
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=60,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 20 * attempt))
                log(f"Rate limit de Gemini (intento {attempt}/{MAX_RETRIES_ON_RATE_LIMIT}). Esperando {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except requests.RequestException as e:
            log(f"ERROR llamando a Gemini: {e}")
            return None
        except (KeyError, IndexError):
            log("ERROR: respuesta de Gemini con formato inesperado.")
            return None

    log("Se agotaron los reintentos por rate limit. Se salta esta iteración.")
    return None


def extract_file_change(response_text: str) -> tuple[str, str] | None:
    match = FILE_BLOCK_RE.search(response_text)
    if not match:
        return None
    filename, content = match.group(1).strip(), match.group(2)
    return filename, content


def run_tests() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "evolve" / "tests"), "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"Tests FALLARON:\n```\n{result.stdout[-1500:]}\n```")
    return result.returncode == 0


def try_one_improvement(state) -> None:
    files = list_editable_files()
    if not files:
        log("No hay archivos editables en app/.")
        return

    # elegir el archivo modificado hace más tiempo (round-robin simple)
    target = min(files, key=lambda p: p.stat().st_mtime)
    original_content = target.read_text(encoding="utf-8")

    prompt = build_prompt(read_mission(), target)
    response = call_gemini(prompt)
    register_request(state)  # cuenta el intento, haya salido bien o mal
    time.sleep(SECONDS_BETWEEN_REQUESTS)  # respetar el límite por minuto antes de la próxima

    if response is None:
        return

    change = extract_file_change(response)
    if change is None:
        log(f"Gemini no devolvió un bloque de archivo válido para {target.name}.")
        return

    _, new_content = change
    target.write_text(new_content, encoding="utf-8")

    if run_tests():
        log(f"✅ Mejora aceptada en {target.name}.")
        target.touch()  # actualiza mtime para el round-robin
    else:
        target.write_text(original_content, encoding="utf-8")
        log(f"❌ Mejora descartada en {target.name} (no pasó los tests), se revirtió.")


def main() -> None:
    state = load_state()
    if not can_make_request(state):
        log(f"Presupuesto diario agotado ({state.requests_used} usados). Corte hasta mañana.")
        return

    log(f"Arrancando corrida. Quedan hoy ~{remaining_today(state)} peticiones objetivo.")

    for i in range(ITERATIONS_PER_RUN):
        if not can_make_request(state):
            log("Tope duro de presupuesto alcanzado en medio de la corrida. Freno.")
            break
        try_one_improvement(state)

    log(f"Corrida terminada. Total usado hoy: {state.requests_used}.")


if __name__ == "__main__":
    main()
