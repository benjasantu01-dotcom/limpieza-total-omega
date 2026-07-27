"""
evolve.py — el "portero + mensajero" del bucle autónomo.

Cada corrida (disparada por GitHub Actions cada N minutos, ver
.github/workflows/evolve.yml):
  1. Chequea el presupuesto diario (no pasarse del límite gratis de Gemini).
  2. Lee la misión actual desde MISSION.md (la "guía" que vos escribís).
  3. Le pide a Gemini UNA mejora concreta y acotada a un archivo puntual,
     rotando entre 6 categorías de enfoque (errores, legibilidad,
     rendimiento, casos límite, seguridad, funcionalidad incremental).
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
from guards import validate_change
from logrotate import rotate_all, summarize as summarize_rotation
from tracking import (
    next_iteration,
    pick_assignment,
    record_metric,
    regenerate_progress,
    RESULT_ACCEPTED,
    RESULT_REJECTED_TESTS,
    RESULT_REJECTED_GUARD,
    RESULT_NO_CHANGE,
    RESULT_NO_RESPONSE,
)

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
MISSION_FILE = ROOT / "MISSION.md"
LOG_FILE = ROOT / "evolve_log.md"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

ITERATIONS_PER_RUN = int(os.environ.get("ITERATIONS_PER_RUN", "1"))

# El nivel gratuito de Gemini limita peticiones POR MINUTO, no solo por
# día. Espaciamos cada llamada para no pasarnos (ajustable por si Google
# cambia el límite; 15s es conservador para la mayoría de los free tiers).
SECONDS_BETWEEN_REQUESTS = int(os.environ.get("SECONDS_BETWEEN_REQUESTS", "15"))
MAX_RETRIES_ON_RATE_LIMIT = 2
# Reintentos ante fallas del servidor de Google (503, 500) y cortes de red.
# Son transitorias y no consumen cuota útil, así que conviene insistir.
MAX_RETRIES_ON_SERVER_ERROR = 3
MAX_WAIT_SECONDS = 30  # nunca esperar más que esto por un solo reintento
# Si Google pide esperar más que esto, asumimos que es la cuota DIARIA
# agotada (no por minuto) y no tiene sentido insistir en esta corrida.
QUOTA_EXHAUSTED_WAIT_THRESHOLD = 60
# Corte de seguridad propio: el job de GitHub Actions tiene un límite
# duro. Nos cortamos solos antes para siempre dejar tiempo de terminar
# prolijo y commitear lo que se haya logrado hasta ahí.
RUN_DEADLINE_SECONDS = int(os.environ.get("RUN_DEADLINE_SECONDS", "480"))

FILE_BLOCK_RE = re.compile(r"```(?:python)?\s*#\s*FILE:\s*(.+?)\n(.*?)```", re.DOTALL)
RATIONALE_RE = re.compile(r"RATIONALE:\s*(.+)")


def extract_rationale(response_text: str) -> str:
    match = RATIONALE_RE.search(response_text)
    return match.group(1).strip() if match else "(sin justificación provista)"


def log(message: str) -> None:
    """Escribe en evolve_log.md y en la consola.

    El archivo siempre va en UTF-8, pero la consola puede no soportar
    emojis (por ejemplo cp1252 en Windows): en ese caso se imprime una
    versión degradada en vez de cortar la corrida por un error de encoding.
    """
    timestamp = datetime.now().isoformat(timespec="seconds")
    line = f"- `{timestamp}` {message}\n"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    try:
        print(line.strip())
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(line.strip().encode(encoding, errors="replace").decode(encoding, errors="replace"))


def read_mission() -> str:
    if not MISSION_FILE.exists():
        return "Mejorá la robustez y legibilidad del código existente sin cambiar su comportamiento."
    return MISSION_FILE.read_text(encoding="utf-8")


def list_editable_files() -> list[Path]:
    return sorted(p for p in APP_DIR.glob("*.py"))


# Cada categoría trae: (técnicas de ejemplo, permite_comportamiento_nuevo,
# usar_busqueda_web). Solo "funcionalidad incremental" puede agregar
# comportamiento nuevo (siempre aditivo, nunca reemplazando lo existente)
# y es la única que investiga en la web antes de proponer algo.
IMPROVEMENT_CATEGORIES = {
    "manejo de errores y validación de entradas": (
        "capturar excepciones específicas en vez de genéricas, validar "
        "parámetros antes de usarlos, agregar mensajes de error más "
        "informativos, chequear valores None/vacíos antes de operar sobre ellos",
        False, False,
    ),
    "legibilidad y documentación": (
        "docstrings que expliquen el PORQUÉ de una decisión no obvia, "
        "type hints donde falten, nombres de variables más descriptivos "
        "cuando el actual sea ambiguo, extraer un bloque complejo a una función con nombre claro",
        False, False,
    ),
    "rendimiento": (
        "evitar recalcular o releer algo que ya se tiene, evitar loops "
        "anidados innecesarios, usar estructuras de datos más eficientes "
        "para el caso de uso (set en vez de list para búsquedas, etc.)",
        False, False,
    ),
    "robustez ante casos límite": (
        "rutas que no existen, permisos denegados, archivos vacíos o "
        "corruptos, listas vacías, valores inesperados de configuración, "
        "concurrencia entre hilos",
        False, False,
    ),
    "seguridad defensiva": (
        "validar que una ruta esté dentro de la carpeta esperada antes de "
        "tocarla, evitar construir rutas o comandos a partir de datos sin "
        "validar, evitar efectos secundarios si una operación falla a mitad de camino",
        False, False,
    ),
    "diseño visual y experiencia de uso": (
        "mejorar cómo se VE y cómo se usa la app. Ideas: más color con "
        "sentido (que el estado se entienda sin leer), mejor jerarquía "
        "visual y espaciado, agrupar en tarjetas, barras de progreso o "
        "medidores en vez de números sueltos, íconos, insignias de estado, "
        "textos más claros y cortos, mejor orden de los controles, "
        "resaltar la acción principal y atenuar las secundarias, "
        "accesibilidad (contraste suficiente, orden de tabulación). "
        "REGLAS: todo color, tamaño e ícono va en branding.py, nunca "
        "escrito a mano en main.py. Nada de dependencias nuevas: el dibujo "
        "se hace con Canvas de Tkinter y widgets de customtkinter. No "
        "cambies la lógica ni saques confirmaciones de las acciones "
        "destructivas. Antes de proponer, buscá en la web cómo se ven las "
        "apps de mantenimiento modernas para inspirarte",
        True, True,
    ),
    "funcionalidad incremental": (
        "una función nueva y chica que sume valor real a la misión del "
        "proyecto (ej: selección de disco/carpeta a escanear, una opción "
        "de filtrado extra, un resumen de estadísticas). Antes de proponer, "
        "buscá en la web cómo lo resuelven limpiadores/antivirus reales "
        "(CCleaner, Windows Defender, BleachBit) para inspirarte. SIEMPRE "
        "aditiva: no debe romper ni cambiar nada de lo que ya existe, solo sumar",
        True, True,
    ),
}


def build_prompt(mission: str, target_file: Path, category: str, techniques: str, allow_new_behavior: bool) -> str:
    current_code = target_file.read_text(encoding="utf-8")
    behavior_rule = (
        "- PODÉS agregar funcionalidad nueva (una función, un parámetro opcional, etc.), "
        "siempre que sea puramente ADITIVA: todo lo que ya existe debe seguir funcionando "
        "exactamente igual que antes. No reemplaces ni cambies comportamiento existente."
        if allow_new_behavior else
        "- NO cambies el comportamiento observable del código (misma funcionalidad)."
    )
    return f"""Sos un colaborador de código senior, cuidadoso y exigente, trabajando en un
proyecto Python real que se va a mostrar como demo técnica. Tu trabajo se
evalúa TODOS LOS DÍAS por el dueño del proyecto: se espera progreso real
y visible en cada corrida, no solo en las primeras.

MISIÓN ACTUAL (definida por el dueño del proyecto):
{mission}

ENFOQUE DE ESTA MEJORA (obligatorio, no te desvíes a otra cosa): {category}
Técnicas típicas de este enfoque (usalas como inspiración, no es una lista cerrada): {techniques}

Tu tarea: proponer UNA SOLA mejora concreta y sustancial al archivo
"{target_file.name}" de abajo, enfocada estrictamente en el enfoque de
arriba. ESFORZATE EN SERIO antes de rendirte: releé el archivo completo
buscando específicamente huecos relacionados a este enfoque — es muy raro
que un archivo real no tenga NINGÚN margen de mejora en ninguna de las
técnicas listadas arriba. Considerá al menos 2 o 3 opciones distintas
internamente y elegí la más valiosa.

Reglas estrictas (estas NO se negocian bajo ningún enfoque):
{behavior_rule}
- NO agregues borrado masivo de archivos ni nada destructivo.
- NO agregues dependencias nuevas.
- PROHIBIDO hacer cambios cosméticos: no renombres variables sin un motivo
  funcional real, no reformatees espacios, no agregues comentarios que no
  aporten información nueva.
- Devolver el archivo SIN CAMBIOS solo está permitido como último recurso,
  después de haber considerado en serio varias opciones y confirmar que
  el archivo ya está sólido en este enfoque específico. Esto debería ser
  la excepción, no la respuesta por defecto.
- El código debe seguir siendo válido y ejecutable.

Formato de respuesta OBLIGATORIO, en este orden exacto:

RATIONALE: <una sola oración explicando qué mejoraste y por qué, o "Sin cambios: <motivo específico y concreto>" si de verdad no hay nada que mejorar>

```python
# FILE: {target_file.name}
<contenido completo del archivo aca>
```

No agregues nada más fuera de esas dos partes.

--- CONTENIDO ACTUAL DE {target_file.name} ---
{current_code}
"""


def call_gemini(prompt: str, use_search: bool = False) -> str | None:
    if not GEMINI_API_KEY:
        log("ERROR: falta GEMINI_API_KEY en el entorno.")
        return None

    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if use_search:
        payload["tools"] = [{"google_search": {}}]

    # Se cuentan por separado los reintentos por rate limit y por fallas del
    # servidor: son problemas distintos y se resuelven con esperas distintas.
    intentos_servidor = 0
    intento_limite = 0
    total_maximo = MAX_RETRIES_ON_RATE_LIMIT + MAX_RETRIES_ON_SERVER_ERROR + 2

    for _ in range(total_maximo):
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=90 if use_search else 60,
            )

            # 5xx: el problema es de Google, no del pedido. Vale reintentar.
            # Antes esto caía en el `except RequestException` y descartaba la
            # iteración entera, que es la causa principal de las iteraciones
            # perdidas: en un día se fueron así ~26% de las peticiones.
            if resp.status_code >= 500:
                intentos_servidor += 1
                if intentos_servidor > MAX_RETRIES_ON_SERVER_ERROR:
                    log(f"Gemini sigue devolviendo {resp.status_code} tras "
                        f"{MAX_RETRIES_ON_SERVER_ERROR} reintentos. Se salta esta iteración.")
                    return None
                espera = min(MAX_WAIT_SECONDS, 3 * 2 ** (intentos_servidor - 1))
                log(f"Gemini devolvió {resp.status_code} (falla temporal del servidor, "
                    f"intento {intentos_servidor}/{MAX_RETRIES_ON_SERVER_ERROR}). "
                    f"Esperando {espera}s...")
                time.sleep(espera)
                continue

            if resp.status_code == 429:
                intento_limite += 1
                attempt = intento_limite
                wait = int(resp.headers.get("Retry-After", 20 * attempt))
                detail = resp.text[:300].replace("\n", " ")
                log(f"Detalle del 429 de Gemini: {detail}")
                if wait > QUOTA_EXHAUSTED_WAIT_THRESHOLD:
                    log(f"Gemini pide esperar {wait}s: parece cuota DIARIA agotada, no vale la pena reintentar hoy.")
                    return "QUOTA_EXHAUSTED"
                if intento_limite > MAX_RETRIES_ON_RATE_LIMIT:
                    log("Se agotaron los reintentos por rate limit. Se salta esta iteración.")
                    return None
                wait = min(wait, MAX_WAIT_SECONDS)
                log(f"Rate limit de Gemini (intento {attempt}/{MAX_RETRIES_ON_RATE_LIMIT}). Esperando {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            if not text.strip():
                raise KeyError("respuesta sin texto")
            return text
        except (requests.Timeout, requests.ConnectionError) as e:
            # Cortes de red y timeouts también son temporales: reintentar.
            intentos_servidor += 1
            if intentos_servidor > MAX_RETRIES_ON_SERVER_ERROR:
                log(f"Red inestable tras {MAX_RETRIES_ON_SERVER_ERROR} reintentos ({e}). "
                    "Se salta esta iteración.")
                return None
            espera = min(MAX_WAIT_SECONDS, 3 * 2 ** (intentos_servidor - 1))
            log(f"Problema de red hablando con Gemini (intento "
                f"{intentos_servidor}/{MAX_RETRIES_ON_SERVER_ERROR}). Esperando {espera}s...")
            time.sleep(espera)
            continue
        except requests.RequestException as e:
            # El resto (4xx que no sea 429, URL mal armada) no se arregla
            # reintentando: el pedido es el problema.
            log(f"ERROR llamando a Gemini: {e}")
            return None
        except (KeyError, IndexError):
            log("ERROR: respuesta de Gemini con formato inesperado.")
            return None

    log("Se agotaron todos los reintentos. Se salta esta iteración.")
    return None


def extract_file_change(response_text: str) -> tuple[str, str] | None:
    match = FILE_BLOCK_RE.search(response_text)
    if not match:
        return None
    filename, content = match.group(1).strip(), match.group(2)
    return filename, content


def run_tests(report_failure: bool = True) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "evolve" / "tests"), "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0 and report_failure:
        log(f"Tests FALLARON:\n```\n{result.stdout[-1500:]}\n```")
    return result.returncode == 0


def tests_pass_before_any_change() -> bool:
    """Verifica que la suite esté verde ANTES de pedirle algo a la IA.

    Sin este chequeo, un test defectuoso (por ejemplo uno que solo funciona
    en Windows cuando el runner es Linux) haría que TODA propuesta se
    rechace "por no pasar los tests", durante días, sin que nadie note que
    el problema está en el portero y no en las mejoras.
    """
    if run_tests(report_failure=False):
        return True
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "evolve" / "tests"), "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    log(
        "⛔ La suite de tests YA falla antes de tocar nada: el problema está en "
        "los tests, no en las mejoras. Se corta la corrida para no descartar "
        "propuestas buenas ni gastar cuota de Gemini. Arreglá evolve/tests/ "
        f"y volvé a correr.\n```\n{result.stdout[-1500:]}\n```"
    )
    return False


def try_one_improvement(state, is_last_iteration: bool = False) -> str:
    """Devuelve 'ok', 'quota_exhausted', o 'no_response' según cómo salió."""
    files = list_editable_files()
    if not files:
        log("No hay archivos editables en app/.")
        return "no_response"

    # Rotación persistente: recorre todas las combinaciones archivo × enfoque.
    # Antes esto se hacía por fecha de modificación, pero el checkout de
    # GitHub Actions reescribe los mtimes en cada corrida, así que la
    # rotación se rompía entre corridas (ver evolve/tracking.py).
    categories_list = list(IMPROVEMENT_CATEGORIES.items())
    iteration = next_iteration()
    target, (category, (techniques, allow_new_behavior, use_search)) = pick_assignment(
        iteration, files, categories_list
    )
    original_content = target.read_text(encoding="utf-8")

    def finish(result: str, rationale: str) -> str:
        record_metric(
            iteration=iteration,
            file_name=target.name,
            category=category,
            result=result,
            rationale=rationale,
        )
        return "ok" if result != RESULT_NO_RESPONSE else "no_response"

    prompt = build_prompt(read_mission(), target, category, techniques, allow_new_behavior)
    response = call_gemini(prompt, use_search=use_search)
    register_request(state)  # cuenta el intento, haya salido bien o mal

    if response == "QUOTA_EXHAUSTED":
        return "quota_exhausted"

    # Espaciar solo si viene otra petición después: en la última iteración
    # esperar es tiempo de runner pagado a cambio de nada.
    if not is_last_iteration:
        time.sleep(SECONDS_BETWEEN_REQUESTS)

    if response is None:
        return finish(RESULT_NO_RESPONSE, "sin respuesta utilizable de Gemini")

    rationale = extract_rationale(response)
    change = extract_file_change(response)
    if change is None:
        log(f"Gemini no devolvió un bloque de archivo válido para {target.name} (enfoque: {category}).")
        return finish(RESULT_NO_RESPONSE, "respuesta sin bloque de archivo válido")

    returned_name, new_content = change

    # El bloque debe corresponder al archivo que pedimos. Si la IA se
    # confunde de archivo, escribir ese contenido acá mezclaría dos módulos.
    if Path(returned_name).name != target.name:
        log(f"🛑 Descartado: se pidió {target.name} pero la respuesta trae '{returned_name}'.")
        return finish(RESULT_REJECTED_GUARD, f"archivo equivocado en la respuesta: '{returned_name}'")

    if new_content.strip() == original_content.strip():
        log(f"➖ Sin cambios en {target.name} (enfoque: {category}). Motivo: {rationale}")
        return finish(RESULT_NO_CHANGE, rationale)

    # Guardias antes de tocar el disco: sintaxis, encogimiento sospechoso y
    # pérdida de símbolos. Esto protege también a main.py, que ningún test
    # puede importar en CI.
    is_valid, reason = validate_change(original_content, new_content, target.name)
    if not is_valid:
        log(f"🛑 Propuesta bloqueada por la guardia en {target.name} (enfoque: {category}): {reason}")
        return finish(RESULT_REJECTED_GUARD, f"{reason} | intento: {rationale}")

    target.write_text(new_content, encoding="utf-8")

    if run_tests():
        log(f"✅ Mejora aceptada en {target.name} (enfoque: {category}). {rationale}")
        return finish(RESULT_ACCEPTED, rationale)

    target.write_text(original_content, encoding="utf-8")
    log(f"❌ Mejora descartada en {target.name} (no pasó los tests), se revirtió. Intento: {rationale}")
    return finish(RESULT_REJECTED_TESTS, rationale)


def main() -> None:
    start_time = time.monotonic()
    state = load_state()
    if not can_make_request(state):
        log(f"Presupuesto diario agotado ({state.requests_used} usados). Corte hasta mañana.")
        return

    log(f"Arrancando corrida. Quedan hoy ~{remaining_today(state)} peticiones objetivo.")

    # El portero tiene que estar sano antes de juzgar a nadie.
    if not tests_pass_before_any_change():
        return

    for i in range(ITERATIONS_PER_RUN):
        if time.monotonic() - start_time > RUN_DEADLINE_SECONDS:
            log(f"Corte de seguridad: se alcanzó el límite de {RUN_DEADLINE_SECONDS}s para esta corrida. Termino prolijo.")
            break
        if not can_make_request(state):
            log("Tope duro de presupuesto alcanzado en medio de la corrida. Freno.")
            break
        result = try_one_improvement(state, is_last_iteration=(i == ITERATIONS_PER_RUN - 1))
        if result == "quota_exhausted":
            log("Cortando la corrida: cuota diaria de Gemini agotada. Reintentamos en la próxima corrida programada.")
            break

    # Reporte de avance regenerado en cada corrida, para poder puntuar el
    # progreso diario sin leer todo el log a mano.
    try:
        regenerate_progress()
    except OSError as e:
        log(f"No se pudo regenerar PROGRESS.md: {e}")

    # Rotación de logs: sin esto, una semana de corridas deja un evolve_log.md
    # de megas que se vuelve a subir entero en cada commit del bot.
    try:
        log(summarize_rotation(rotate_all(ROOT)))
    except OSError as e:
        log(f"No se pudo rotar los logs: {e}")

    log(f"Corrida terminada. Total usado hoy: {state.requests_used}.")


if __name__ == "__main__":
    main()
