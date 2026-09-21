"""
assistant.py — asistente que explica el estado del sistema y qué conviene hacer.

Tiene DOS motores, y el orden importa:

1. **Motor local (siempre disponible, sin conexión).** Reglas sobre las
   métricas ya calculadas. Responde qué está mal, por qué, y qué botón de la
   app resuelve cada cosa. No manda nada a ninguna parte.

2. **Motor Gemini (opcional, apagado por defecto).** Agrega la parte
   conversacional: preguntas escritas con palabras propias. Requiere que el
   usuario lo active y que haya una clave.

QUÉ SE ENVÍA Y QUÉ NO
---------------------
Esto es lo más importante del módulo. Cuando el motor remoto está activo se
manda **solo un puñado de números agregados**: MB de basura, cantidad de
sospechosos, porcentaje de RAM y disco libres, cantidad de programas de
inicio, puntaje de salud.

Nunca se envían:
  - rutas de archivos ni de carpetas
  - nombres de archivos
  - contenido de archivos
  - nombres de procesos
  - nombre de usuario, de la máquina ni números de serie

`build_context()` es la única función que arma lo que sale del equipo, y
`SENSITIVE_KEYS_NEVER_SENT` documenta lo que queda afuera. Un test verifica
 que el texto enviado no contenga separadores de ruta, así el día que alguien
agregue una métrica con una ruta adentro, el test falla antes de que se filtre.

EL ASISTENTE NO EJecuta NADA
----------------------------
Solo devuelve texto. No borra, no mueve, no aísla. Si sugiere una acción, la
describe para que el usuario la haga desde su pestaña. Un asistente que puede
apretar botones es un asistente que puede equivocarse sobre archivos reales.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import re
import math
import logging
import operator
from itertools import islice
from functools import lru_cache, wraps
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeAlias, Callable, Optional, Union, NamedTuple, Iterator

import settings
from safety import is_protected_path

__all__ = [
    "SystemContext",
    "Answer",
    "SENSITIVE_KEYS_NEVER_SENT",
    "SUGGESTED_QUESTIONS",
    "PRIVACY_NOTICE",
    "OFFLINE_NOTICE",
    "SYSTEM_PROMPT",
    "build_context",
    "context_as_text",
    "local_answer",
    "ask",
    "explain_area",
]

# Configuración de límites predeterminados
DEFAULT_METRIC_VAL: Final[float] = -1.0
DEFAULT_RAM_PCT: Final[float] = 50.0

def _safe_handler_wrapper(func: Callable[[SystemContext, str], Answer]) -> Callable[[SystemContext, str], Answer]:
    """
    Decorador para handlers de preguntas: garantiza una estructura de retorno consistente
    y aísla excepciones internas para evitar el cierre inesperado del hilo de la UI.
    """
    @wraps(func)
    def wrapper(ctx: SystemContext, q: str) -> Answer:
        if ctx.is_empty: return Answer("Primero analizá el sistema.")
        try:
            result = func(ctx, q)
            if isinstance(result, Answer):
                return result
            logging.error(f"Handler {func.__name__} devolvió tipo inesperado: {type(result)}")
        except Exception as e:
            logging.error(f"Error en {func.__name__}: {e}")
        return Answer("No pude procesar la información solicitada.")
    return wrapper

class AssistantConfig(NamedTuple):
    """Configuración persistida del asistente cargada desde settings."""
    api_key: str
    model: str
    allow_metrics: bool

@dataclass(frozen=True)
class MetricSpec:
    """
    Define el contrato de validación y conversión para métricas numéricas.
    Garantiza que solo se procesen datos que representen magnitudes reales.
    """
    cast_func: Callable[[Any], Any]
    min_val: float
    max_val: float

    def is_valid_type(self, val: Any) -> bool:
        """Valida que el valor sea un número real; excluye explícitamente booleanos."""
        return isinstance(val, (int, float)) and not isinstance(val, bool)

class ProblemCriterion(NamedTuple):
    """
    Regla heurística para identificar problemas críticos según métricas.
    Encapsula la lógica de comparación para evitar ramas 'if' complejas.
    """
    metric_key: str
    threshold: float
    operator: str
    message_format: str

    def _evaluate_metric(self, val: float) -> bool:
        """Ejecuta la comparación lógica entre la métrica actual y el umbral usando el operador definido."""
        ops = {"<": operator.lt, ">": operator.gt}
        op_func = ops.get(self.operator)
        return op_func(val, self.threshold) if op_func else False

    def is_triggered_by(self, ctx: SystemContext) -> bool:
        """Determina si el contexto del sistema viola el umbral establecido."""
        val = ctx.get_metric(self.metric_key, DEFAULT_METRIC_VAL)
        return val >= 0 and self._evaluate_metric(val)

    def format_if_triggered(self, ctx: SystemContext) -> Optional[str]:
        """Genera un mensaje de advertencia formateado si el criterio de problema es superado."""
        val: float = ctx.get_metric(self.metric_key, DEFAULT_METRIC_VAL)
        
        if val < 0 or not self._evaluate_metric(val):
            return None
            
        try:
            msg: str = self.message_format.format(val)[:_MAX_MSG_CHUNK]
            return msg if _ensure_safe_text(msg) else None
        except (ValueError, TypeError, AttributeError, KeyError):
            return None

class AreaExplanation(NamedTuple):
    """Mapeo para descripciones pedagógicas de cada área de la aplicación."""
    key: str
    description: str

# Límites de seguridad y tamaño
_MAX_TEXT_LENGTH: Final[int] = 1000
_MAX_RESPONSE_BYTES: Final[int] = 32768
_MAX_MSG_CHUNK: Final[int] = 200
_MAX_PROMPT_LIMIT: Final[int] = 4000
_MAX_NESTING_DEPTH: Final[int] = 2

SENSITIVE_KEYS_NEVER_SENT: Final[tuple[str, ...]] = (
    "rutas de archivos", "nombres de archivos", "contenido de archivos",
    "nombres de procesos", "nombre de usuario", "nombre del equipo", "números de serie",
)

PRIVACY_NOTICE: Final[str] = (
    "El asistente en línea envía a Google solo números agregados. "
    "Nunca envía rutas, nombres ni contenido de archivos."
)

OFFLINE_NOTICE: Final[str] = (
    "Respondido por el motor local, sin conexión ni envío de datos."
)

SUGGESTED_QUESTIONS: Final[tuple[str, ...]] = (
    "¿Qué es lo más urgente que debería arreglar?",
    "¿Por qué mi PC está lenta?",
    "¿Es seguro borrar lo que encontró la limpieza?",
    "¿Cuánto espacio puedo recuperar?",
    "¿Qué significa mi puntaje de salud?",
    "¿Conviene desactivar programas de inicio?",
)

SUGGESTED_QUESTIONS_SHORT: Final[list[str]] = list(SUGGESTED_QUESTIONS[:3])

SYSTEM_PROMPT: Final[str] = (
    "Sos el asistente de Limpieza Total Omega, una app de mantenimiento para "
    "Windows 11. Respondés en castellano rioplatense, de forma breve y "
    "concreta, sin tecnicismos innecesarios.\n\n"
    "Reglas:\n"
    "- Basate solo en las métricas que te paso. No inventes datos que no están.\n"
    "- No prometas resultados mágicos.\n"
    "- Los 'limpiadores de RAM' empeoran el rendimiento: explicalo si preguntan.\n"
    "- Nunca digas que borraste o cambiaste algo: vos solo aconsejás.\n"
    "- Si te preguntan algo que no se puede saber con estas métricas, decí "
    "que hace falta correr el análisis correspondiente.\n"
    "- Máximo 6 líneas."
)

# Regex de validación
_ENDPOINT: Final[str] = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TIMEOUT_SECONDS: Final[int] = 30
_PATH_INJECTION_REGEX: Final[re.Pattern] = re.compile(r"([a-zA-Z]:[\\/]|/|\\|\.\.|\0|[\u202e\u202d\u200e\u200f])")
_CONTROL_CHARS_REGEX: Final[re.Pattern] = re.compile(r"[\x00-\x1f\x7f\u0080-\u009f\u202b-\u202f\u200b-\u200d\uFEFF]")
_ANSI_ESCAPE_REGEX: Final[re.Pattern] = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_PS_COMMAND_REGEX: Final[re.Pattern] = re.compile(r"(Get-|Remove-|Set-|Stop-|Start-)[a-zA-Z]+", re.IGNORECASE)
_RESTRICTED_CONTENT_REGEX: Final[re.Pattern] = re.compile(r"(exec|eval|subprocess|system\s*\(|rm\s+|del\s+|cmd\.exe|powershell|reg\.exe)", re.IGNORECASE)
_SENSITIVE_STRUCTURE_REGEX: Final[re.Pattern] = re.compile(r"(\\\\|[a-z]:\\|/etc/|\\\\UNC|C:\\Windows|System32|/proc/|/dev/)", re.IGNORECASE)
_TOKEN_REGEX: Final[re.Pattern] = re.compile(r"\w+")
_MODEL_NAME_REGEX: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9\.\-_]{1,64}$")
_API_KEY_REGEX: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9_\-\.]{1,128}$")

_CRITERIOS_SALUD: Final[tuple[ProblemCriterion, ...]] = (
    ProblemCriterion("disk_free_percent", 10.0, "<", "{:.0f}% de disco libre"),
    ProblemCriterion("suspicious_warnings", 0, ">", "{:d} archivo(s) sospechosos"),
    ProblemCriterion("memory_available_percent", 15.0, "<", "{:.0f}% de RAM"),
    ProblemCriterion("junk_mb", 1000.0, ">", "{:.0f} MB de basura"),
    ProblemCriterion("duplicate_mb", 500.0, ">", "{:.0f} MB en duplicados"),
    ProblemCriterion("startup_count", 15, ">", "{:d} programas de inicio")
)

_EXPLANATION_MAP: Final[dict[str, str]] = {
    "basura": "Archivos temporales y restos de instaladores: ocupan espacio innecesario sin aportar valor operativo.",
    "seguridad": "Archivos con señales de riesgo: extensiones inusuales o ejecutables sin firma, requieren revisión manual.",
    "memoria": "Recursos de acceso rápido: si la memoria disponible es baja, Windows utiliza el disco duro, ralentizando todo.",
    "disco": "Almacenamiento disponible: niveles inferiores al 10% afectan la estabilidad y velocidad de escritura de Windows.",
    "duplicados": "Copias idénticas del mismo archivo: se pueden eliminar de forma segura ya que el archivo original permanece.",
    "inicio": "Programas que arrancan con Windows: cada entrada incrementa el tiempo de inicio y el consumo base de memoria.",
}

_VALIDATORS: Final[dict[str, MetricSpec]] = {
    "junk_mb": MetricSpec(float, 0.0, 1e9),
    "suspicious_count": MetricSpec(int, 0, 10000),
    "suspicious_warnings": MetricSpec(int, 0, 10000),
    "memory_available_percent": MetricSpec(float, 0.0, 100.0),
    "memory_total_gb": MetricSpec(float, 0.0, 2048.0),
    "disk_free_percent": MetricSpec(float, 0.0, 100.0),
    "duplicate_mb": MetricSpec(float, 0.0, 1e9),
    "startup_count": MetricSpec(int, 0, 1000),
    "quarantined_count": MetricSpec(int, 0, 10000),
    "browser_cache_mb": MetricSpec(float, 0.0, 1e6),
    "score": MetricSpec(int, 0, 100),
}

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Convierte cualquier valor a float asegurando que el resultado sea finito y no negativo."""
    try:
        if val is None or isinstance(val, bool) or not isinstance(val, (int, float, str)):
            return default
        f = float(val)
        return f if (math.isfinite(f) and f >= 0) else default
    except (TypeError, ValueError):
        return default

def _validate_response_length(text: Any) -> str:
    """Trunca el texto asegurando que no exceda el límite definido."""
    if not isinstance(text, str): return ""
    return text[:_MAX_TEXT_LENGTH]

def _is_input_too_deep_or_complex(val: Any, depth: int = 0) -> bool:
    """Detecta si una estructura de datos es peligrosamente profunda para evitar stack overflow."""
    if depth > _MAX_NESTING_DEPTH: return True
    if isinstance(val, (list, tuple, set)):
        if len(val) > 20: return True
        return any(_is_input_too_deep_or_complex(item, depth + 1) for item in val)
    return False

def _is_metric_within_bounds(val: float, spec: MetricSpec) -> bool:
    """Verifica si un valor numérico está dentro del rango lógico definido por su especificación."""
    return math.isfinite(val) and spec.min_val <= val <= spec.max_val

@dataclass
class SystemContext:
    """
    Agregador de estado del sistema utilizado para diagnósticos.
    Implementa validación de integridad para evitar métricas malformadas o peligrosas.
    """
    score: Optional[int] = None
    grade: str = ""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 0.0
    memory_total_gb: float = 0.0
    disk_free_percent: float = 0.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0
    browser_cache_mb: float = 0.0
    analyzed: bool = False

    def get_metric(self, key: str, default: float) -> float:
        """Obtiene una métrica del contexto, aplicando un valor por defecto si no existe o es inválida."""
        try:
            val = getattr(self, key, default)
            return float(val) if isinstance(val, (int, float)) and math.isfinite(val) else default
        except (TypeError, ValueError):
            return default

    @property
    def is_empty(self) -> bool:
        """Verifica si el contexto contiene datos útiles tras el análisis."""
        if not self.analyzed: return True
        return self.score is None or not isinstance(self.score, int) or self.score < 0

    def __hash__(self) -> int:
        return hash((self.score, self.grade, self.junk_mb, self.suspicious_count, 
                     self.memory_available_percent, self.disk_free_percent,
                     self.duplicate_mb, self.startup_count))

    @property
    def is_valid_structure(self) -> bool:
        """Valida que el grado de salud (string) no contenga inyecciones o caracteres prohibidos."""
        return _ensure_safe_text(self.grade) if self.grade else True

    def _apply_field(self, source: Any, key: str, spec: MetricSpec) -> bool:
        """Valida y asigna un valor individual al campo correspondiente si cumple el contrato MetricSpec."""
        val = _get_source_value(source, key)
        if val is None or not spec.is_valid_type(val):
            return False
            
        try:
            float_val = float(val)
            if math.isfinite(float_val) and _is_metric_within_bounds(float_val, spec):
                setattr(self, key, spec.cast_func(float_val))
                return True
        except (ValueError, TypeError, OverflowError):
            return False
        return False

    def _clean_grade(self, val: Any) -> str:
        """Limpia el string del grado de salud eliminando caracteres no alfanuméricos."""
        if not isinstance(val, str): return ""
        clean = _CONTROL_CHARS_REGEX.sub(" ", val)[:10].strip()
        return clean if _ensure_safe_text(clean) else ""

    def ingest(self, source: Any) -> bool:
        """
        Carga datos externos hacia el contexto tras validar la integridad de cada campo.
        Retorna True solo si al menos una métrica fue importada exitosamente y el contexto es íntegro.
        """
        if source is None or _is_input_too_deep_or_complex(source):
            return False
        if not isinstance(source, (dict, object)) or isinstance(source, (str, int, float, bool)):
            return False
            
        found_data = False
        for key, spec in _VALIDATORS.items():
            if self._apply_field(source, key, spec):
                found_data = True
        
        grade_val = _get_source_value(source, "grade")
        if isinstance(grade_val, str):
            clean_grade = self._clean_grade(grade_val)
            if clean_grade:
                self.grade = clean_grade
                found_data = True
        
        return found_data and _validate_context_integrity(self)

@dataclass
class Answer:
    """Respuesta generada, encapsulando el texto y la fuente del mensaje."""
    text: str
    source: str = "local"
    notice: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        return self.source == "gemini"

def _validate_context_integrity(ctx: SystemContext) -> bool:
    """Verifica que las métricas del contexto se encuentren dentro de rangos físicamente posibles y no sean valores NaN/Inf."""
    return (
        ctx.junk_mb >= 0 and math.isfinite(ctx.junk_mb) and
        ctx.duplicate_mb >= 0 and math.isfinite(ctx.duplicate_mb) and
        0 <= ctx.get_metric("disk_free_percent", 0.0) <= 100 and
        0 <= ctx.get_metric("memory_available_percent", 0.0) <= 100
    )

def _is_safe_text_structure(text: str) -> bool:
    """
    Chequeo profundo de seguridad: busca patrones de inyección de rutas, 
    comandos de PowerShell o caracteres de control en textos recibidos.
    """
    if not text: return True
    if any(ord(c) < 32 and c not in '\n\r\t' for c in text): return False
    
    if not is_protected_path(text): 
        try:
            p = Path(text)
            if p.is_absolute() or text.startswith(("./", "../", "..\\")): return False
        except Exception: pass
    
    return not (
        _PATH_INJECTION_REGEX.search(text) or 
        _RESTRICTED_CONTENT_REGEX.search(text) or 
        _SENSITIVE_STRUCTURE_REGEX.search(text) or
        _ANSI_ESCAPE_REGEX.search(text) or
        _PS_COMMAND_REGEX.search(text)
    )

def _ensure_safe_text(text: Any) -> bool:
    """Wrapper final para validar que cualquier texto sea seguro, corto y libre de caracteres de control."""
    if not isinstance(text, str) or not text or len(text) > _MAX_TEXT_LENGTH:
        return False
    if _CONTROL_CHARS_REGEX.search(text):
        return False
    return _is_safe_text_structure(text)

def _get_source_value(source: Any, key: str) -> Any:
    """Acceso seguro a atributos: previene la ejecución de métodos o acceso a dunders."""
    if not isinstance(key, str) or key.startswith("_"): return None
    if isinstance(source, dict):
        return source.get(key)
    try:
        if isinstance(source, type): return None
        val = getattr(source, key, None)
        return None if callable(val) or key.startswith("__") else val
    except Exception:
        return None

def build_context(metrics: Any = None, health: Any = None, **extra: Any) -> SystemContext:
    """Inicializa un SystemContext completo integrando datos de distintas fuentes."""
    ctx = SystemContext()
    for s in (metrics, health, extra):
        if s is not None and ctx.ingest(s):
            ctx.analyzed = True
    return ctx

def _fmt_metric_sanitized(val: Any, unit: str = "", decimal: int = 0) -> str:
    """Formatea una métrica, limpiando caracteres prohibidos para su visualización."""
    if not isinstance(val, (int, float, str)): return "N/A"
    raw = _fmt_metric(val, unit, decimal)
    return _PATH_INJECTION_REGEX.sub(" ", _CONTROL_CHARS_REGEX.sub(" ", raw))[:32]

@lru_cache(maxsize=16)
def _generate_context_cached(ctx: SystemContext) -> str:
    """Genera bloque de resumen del sistema para prompts del asistente."""
    s_score = f"{ctx.score}" if ctx.score is not None else 'N/A'
    s_grade = f" nota {ctx.grade[:5]}" if ctx.grade else ''
    return (
        f"Puntaje de salud: {s_score}{s_grade}\n"
        f"Basura: {_fmt_metric(ctx.junk_mb, ' MB', 0)}\n"
        f"Sospechosos: {int(ctx.suspicious_count)}\n"
        f"RAM disponible: {_fmt_metric(ctx.memory_available_percent, '%', 0)}\n"
        f"Disco libre: {_fmt_metric(ctx.disk_free_percent, '%', 0)}\n"
        f"Duplicados: {_fmt_metric(ctx.duplicate_mb, ' MB', 0)}\n"
        f"Inicio: {int(ctx.startup_count)} items"
    )

def context_as_text(context: SystemContext) -> str:
    """Serializa las métricas de SystemContext en texto optimizado para prompts."""
    if context.is_empty: return ""
    return _generate_context_cached(context)

def _fmt_metric(val: Any, unit: str = "", decimal: int = 0) -> str:
    """Convierte valores a cadena con precisión definida."""
    f = _safe_float(val, -1.0)
    if f < 0: return "N/A"
    return f"{f:.{decimal}f}{unit}"

def explain_area(area: Any) -> str:
    """Devuelve la definición pedagógica de un área específica de la aplicación."""
    if not isinstance(area, str):
        return "No tengo una explicación para esa área."
    return _validate_response_length(_EXPLANATION_MAP.get(area.strip().lower(), "No tengo una explicación para esa área."))

@lru_cache(maxsize=16)
def _get_active_problems(ctx: SystemContext) -> tuple[str, ...]:
    """Identifica problemas activos comparando contra criterios de salud."""
    return tuple(msg for crit in _CRITERIOS_SALUD if (msg := crit.format_if_triggered(ctx)))

def _format_problem_message(problems: tuple[str, ...], score: Union[int, str]) -> str:
    """Crea una oración descriptiva con los problemas encontrados."""
    try:
        clean_score = str(score)
        if not problems:
            return f"Tu sistema está en buen estado ({clean_score}/100). No hay nada urgente."
        return f"Con un puntaje de {clean_score}/100, por orden de prioridad: {', '.join(problems)}."
    except Exception:
        return "Tu sistema tiene problemas detectados."

def _identify_active_problems(ctx: SystemContext) -> tuple[str, ...]:
    """Valida la integridad del escaneo y retorna los problemas identificados."""
    return _get_active_problems(ctx) if ctx.analyzed else ()

@_safe_handler_wrapper
def handle_ram(ctx: SystemContext, user_query: str) -> Answer:
    """Procesa consultas sobre el uso y estado de memoria RAM."""
    mem_pct = ctx.get_metric("memory_available_percent", DEFAULT_RAM_PCT)
    total_gb = ctx.get_metric("memory_total_gb", 0.0)
    
    parts = [f"Tenés {mem_pct:.0f}% de RAM disponible{f' de {total_gb:.0f} GB' if total_gb > 0 else ''}."]
    if mem_pct < 15:
        parts.append("Eso es poco: Windows está usando el disco como memoria y ahí se siente la lentitud. Cerrá lo que no uses.")
    else:
        parts.append("Eso está bien. Si la PC va lenta, el problema seguramente no es la RAM.")
    
    parts.append("No busques un 'liberador de RAM': la PC queda más lenta.")
    startup_count = int(ctx.get_metric("startup_count", 0))
    if startup_count > 12:
        parts.append(f"Sí te conviene mirar los {startup_count} programas de inicio.")
    return Answer(_validate_response_length(" ".join(parts)), notice=OFFLINE_NOTICE, suggestions=["¿Conviene desactivar programas de inicio?"])

@_safe_handler_wrapper
def handle_disk(ctx: SystemContext, user_query: str) -> Answer:
    """Procesa consultas sobre el espacio en disco."""
    junk = ctx.get_metric("junk_mb", 0.0)
    dup = ctx.get_metric("duplicate_mb", 0.0)
    cache = ctx.get_metric("browser_cache_mb", 0.0)
    free = ctx.get_metric("disk_free_percent", 100.0)
    
    recuperable = junk + dup + cache
    msg = f"Tenés {free:.0f}% libre en disco. Podés recuperar cerca de {recuperable:.0f} MB."
    msg += f" (incluye {junk:.0f} MB de basura, {dup:.0f} MB de duplicados{f', {cache:.0f} MB caché' if cache > 0 else ''})."
    if free < 10:
        msg += " ¡Alerta! Estás por debajo del 10%, afecta la estabilidad."
    msg += " Empezá por Limpieza: mueve los candidatos a revisión."
    return Answer(_validate_response_length(msg), notice=OFFLINE_NOTICE)

@_safe_handler_wrapper
def handle_security(ctx: SystemContext, user_query: str) -> Answer:
    """Procesa consultas sobre riesgos de seguridad hallados."""
    count = int(ctx.get_metric("suspicious_count", 0.0))
    warn = int(ctx.get_metric("suspicious_warnings", 0.0))
    if count == 0:
        texto = "No hay archivos sospechosos. La app nunca borra sola, todo va a revisión."
    else:
        info = f"Hay {count} archivo(s) marcados, {warn} con advertencia."
        sugerencia = "Son señales, no una condena: si no reconocés alguno, usá 'Aislar hallazgos'."
        texto = f"{info} {sugerencia} La limpieza solo mueve a cuarentena."
    return Answer(_validate_response_length(texto), notice=OFFLINE_NOTICE)

@_safe_handler_wrapper
def handle_score(ctx: SystemContext, user_query: str) -> Answer:
    """Responde explicando cómo se compone el puntaje de salud."""
    score_val = ctx.score if ctx.score is not None else "N/A"
    grade_str = ctx.grade if ctx.grade else ""
    score_display = f"Tu puntaje es {score_val}/100{f' (nota {grade_str})' if grade_str else ''}."
    
    problemas = _identify_active_problems(ctx)
    resumen = ("Lo que más te está restando: " + ", ".join(problemas[:3]) + ".") if problemas else "No hay nada urgente."
    explicacion = " El puntaje combina basura, seguridad, memoria, disco, duplicados y programas de inicio."
    return Answer(_validate_response_length(f"{score_display} {resumen}{explicacion}"), notice=OFFLINE_NOTICE)

@_safe_handler_wrapper
def handle_startup(ctx: SystemContext, user_query: str) -> Answer:
    """Procesa consultas sobre programas de arranque."""
    count = int(ctx.get_metric("startup_count", 0.0))
    estado = f"Tenés {count} programas que arrancan con Windows."
    valoracion = "Son bastantes, y cada uno suma tiempo de encendido." if count > 15 else ("Es normal." if count > 8 else "Está bien.")
    cierre = " La app los lista, pero desactivalos desde el Administrador de tareas de Windows."
    return Answer(_validate_response_length(f"{estado} {valoracion}{cierre}"), notice=OFFLINE_NOTICE)

TOKENS_BY_CATEGORY: Final[dict[str, Callable[[SystemContext, str], Answer]]] = {
    token: handler 
    for key_set, handler in {
        frozenset(["ram", "memoria", "lenta", "lento", "acelerar"]): handle_ram,
        frozenset(["espacio", "disco", "lleno", "recuperar", "liberar"]): handle_disk,
        frozenset(["seguro", "virus", "sospechos", "borrar", "peligro"]): handle_security,
        frozenset(["puntaje", "salud", "nota", "score"]): handle_score,
        frozenset(["inicio", "arranque", "arranca", "encender"]): handle_startup
    }.items() for token in key_set
}

def _sanitize_query(question: str) -> str:
    """Limpia el input del usuario eliminando caracteres prohibidos."""
    if not isinstance(question, str): return ""
    clean = _CONTROL_CHARS_REGEX.sub(' ', question).strip()[:100]
    return clean if _ensure_safe_text(clean) else ""

def local_answer(question: str, context: SystemContext) -> Answer:
    """Motor de inferencia local: procesa preguntas basadas en las métricas."""
    q_sanitized = _sanitize_query(question)
    if not q_sanitized:
        return Answer("Entrada no válida.")
    if context.is_empty:
        return Answer(
            text="Todavía no corriste ningún análisis. Andá a la pestaña Salud "
                 "y apretá 'Analizar el sistema': es de solo lectura.",
            notice=OFFLINE_NOTICE,
            suggestions=SUGGESTED_QUESTIONS_SHORT,
        )
    
    for token in _TOKEN_REGEX.findall(q_sanitized.lower()):
        if handler := TOKENS_BY_CATEGORY.get(token):
            return handler(context, question)
            
    cuerpo = _format_problem_message(
        _identify_active_problems(context), 
        context.score if context.score is not None else "N/A"
    )
    return Answer(_validate_response_length(cuerpo), notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_SHORT)

def available(base: Union[str, Path, None] = None) -> bool:
    """Verifica si el asistente remoto (Gemini) está habilitado."""
    try:
        return settings.assistant_enabled(base)
    except (TypeError, ValueError, AttributeError, OSError):
        return False

def _parse_config(raw_cfg: Any) -> AssistantConfig:
    """Parsea el diccionario de configuración externa de manera segura."""
    default = AssistantConfig("", "gemini-3.1-flash-lite", True)
    if not isinstance(raw_cfg, dict):
        return default
    try:
        api_key = str(raw_cfg.get("asistente_api_key", ""))
        model = str(raw_cfg.get("asistente_modelo", "gemini-3.1-flash-lite"))
        metrics_val = raw_cfg.get("asistente_enviar_metricas")
        allow_metrics = True if metrics_val is None else bool(metrics_val)
        return AssistantConfig(api_key, model, allow_metrics)
    except (ValueError, TypeError, AttributeError):
        return default

def _build_payload(question: str, context_text: str) -> Optional[bytes]:
    """Serializa la pregunta y el contexto en un JSON listo para ser enviado a la API."""
    if not context_text or not _ensure_safe_text(context_text): return None
    q = _sanitize_query(question)
    if not q or not _ensure_safe_text(q): return None
    
    try:
        full_prompt = f"{SYSTEM_PROMPT}\n\nMétricas:\n{context_text}\n\nPregunta: {q}"
        if len(full_prompt) > _MAX_PROMPT_LIMIT: return None
        payload_data = {"contents": [{"parts": [{"text": full_prompt}]}]}
        payload = json.dumps(payload_data).encode("utf-8")
        return payload if len(payload) < (_MAX_RESPONSE_BYTES // 2) else None
    except (TypeError, ValueError):
        return None

def _extract_text_from_gemini_json(data: Any) -> Optional[str]:
    """Extrae de forma segura el texto de la estructura JSON devuelta por la API."""
    if not isinstance(data, dict): return None
    try:
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates: return None
        content = candidates[0].get("content")
        if not isinstance(content, dict): return None
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts: return None
        text_val = parts[0].get("text")
        return str(text_val) if isinstance(text_val, str) else None
    except (AttributeError, TypeError, IndexError): 
        return None

def _call_gemini(question: str, context_text: str, api_key: str, model: str) -> Optional[str]:
    """Realiza la comunicación HTTP con Gemini mediante librería estándar."""
    if not _API_KEY_REGEX.match(api_key) or not _MODEL_NAME_REGEX.match(model): 
        return None
    payload = _build_payload(question, context_text)
    if not payload: return None
    
    try:
        url = _ENDPOINT.format(model=model) + f"?key={api_key}"
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as res:
            if res.status != 200: return None
            raw_res = res.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw_res) > _MAX_RESPONSE_BYTES: return None
            
            data = json.loads(raw_res.decode("utf-8"))
            raw_text = _extract_text_from_gemini_json(data)
            if raw_text and _ensure_safe_text(raw_text):
                return _validate_response_length(raw_text.strip())
            return None
    except urllib.error.HTTPError as e:
        logging.error(f"Error HTTP en Gemini: {e.code}")
        return None
    except (urllib.error.URLError, OSError, ValueError, KeyError, json.JSONDecodeError):
        return None

def ask(question: str, context: Optional[SystemContext] = None,
        base: Union[str, Path, None] = None) -> Answer:
    """Punto de entrada unificado para consultas de usuario, alternando entre motores."""
    if not _ensure_safe_text(question):
        return Answer("Entrada no válida.")
    ctx: SystemContext = context if isinstance(context, SystemContext) else SystemContext()
    respaldo: Answer = local_answer(question, ctx)
    if not available(base):
        return respaldo
    try:
        settings_data = settings.load(base)
        cfg = _parse_config(settings_data)
        texto_contexto = context_as_text(ctx) if cfg.allow_metrics else "El usuario no autorizó enviar métricas."
        remoto = _call_gemini(question, texto_contexto, cfg.api_key, cfg.model)
        if not remoto:
            respaldo.notice = "No se pudo consultar al asistente en línea, respondí con el motor local."
            return respaldo
        return Answer(remoto, source="gemini", notice=PRIVACY_NOTICE)
    except Exception:
        return respaldo
