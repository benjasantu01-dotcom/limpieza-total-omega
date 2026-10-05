"""
assistant.py — asistente que explica el estado del sistema y qué conviene hace.

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
from functools import lru_cache, wraps, cached_property
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

# Límites de seguridad y tamaño para evitar inyecciones o ataques de denegación de servicio (DoS)
_MAX_TEXT_LENGTH: Final[int] = 1000
_MAX_RESPONSE_BYTES: Final[int] = 32768
_MAX_MSG_CHUNK: Final[int] = 200
_MAX_PROMPT_LIMIT: Final[int] = 4000
_MAX_NESTING_DEPTH: Final[int] = 2

# Estructura fija para resumen de contexto: (clave_métrica, unidad_legible, precisión_decimal)
_CONTEXT_SCHEMA: Final = (
    ("score", "", 0), ("junk_mb", " MB", 0), ("suspicious_count", "", 0), 
    ("memory_available_percent", "%", 0), ("disk_free_percent", "%", 0),
    ("duplicate_mb", " MB", 0), ("startup_count", "", 0)
)

def _is_safe_key(key: str) -> bool:
    """Valida que una clave de diccionario o atributo no sea privada o interna."""
    return isinstance(key, str) and not (key.startswith("__") or key.startswith("_") or key == "ingest")

def _check_metric_integrity(val: float) -> bool:
    """Verifica que un valor numérico sea seguro, finito y coherente para el asistente."""
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val) and not math.isnan(val)

def _safe_handler_wrapper(func: Callable[[SystemContext, str], Answer]) -> Callable[[SystemContext, str], Answer]:
    """
    Decorador para proteger los manejadores de consulta contra excepciones.
    
    Asegura la disponibilidad del contexto y captura errores de ejecución
    para evitar que el hilo principal de la UI falle, retornando un fallback.
    """
    @wraps(func)
    def wrapper(ctx: SystemContext, q: str) -> Answer:
        if not isinstance(ctx, SystemContext) or ctx.is_empty: 
            return Answer("Primero analizá el sistema.")
        try:
            result = func(ctx, q)
            if isinstance(result, Answer) and result.text:
                return result
            logging.error(f"Handler {func.__name__} devolvió respuesta vacía o inválida")
        except Exception as e:
            logging.error(f"Falla inesperada en {func.__name__}: {str(e)[:50]}")
        return Answer("Error al procesar la respuesta.")
    return wrapper

class AssistantConfig(NamedTuple):
    """
    Configuración estructurada para el cliente de IA (Gemini).

    Attributes:
        api_key (str): Credencial para la API, idealmente desde variables de entorno.
        model (str): Identificador del modelo remoto (ej. gemini-3.1-flash-lite).
        allow_metrics (bool): Flag de privacidad para autorizar el envío de métricas.
    """
    api_key: str
    model: str
    allow_metrics: bool

@dataclass(frozen=True)
class MetricSpec:
    """
    Especificación de validación para métricas numéricas del sistema.

    Attributes:
        cast_func (Callable): Función para normalizar el tipo de dato.
        min_val (float): Límite inferior permitido para el valor.
        max_val (float): Límite superior permitido para el valor.
    """
    cast_func: Callable[[Any], Any]
    min_val: float
    max_val: float

    def is_valid_type(self, val: Any) -> bool:
        """Verifica que el valor sea un número (int/float) real y no booleano."""
        return isinstance(val, (int, float)) and not isinstance(val, bool)

class ProblemCriterion(NamedTuple):
    """
    Regla declarativa de detección de problemas basada en umbrales.

    Attributes:
        metric_key (str): Nombre de la métrica a evaluar en SystemContext.
        threshold (float): Valor límite para disparar el criterio.
        operator (str): Operador lógico ('<' o '>') de comparación.
        message_format (str): Template de mensaje educativo para el usuario.
    """
    metric_key: str
    threshold: float
    operator: str
    message_format: str

    def _evaluate_metric(self, val: float) -> bool:
        """Ejecuta la comparación lógica según el operador del criterio."""
        if not _check_metric_integrity(val):
            return False
        if self.operator == "<":
            return val < self.threshold
        if self.operator == ">":
            return val > self.threshold
        return False

    def is_triggered_by(self, ctx: SystemContext) -> bool:
        """Verifica si el criterio de riesgo actual se cumple."""
        val = ctx.get_metric(self.metric_key, DEFAULT_METRIC_VAL)
        return val >= 0 and self._evaluate_metric(val)

    def format_if_triggered(self, ctx: SystemContext) -> Optional[str]:
        """Formatea un mensaje de advertencia si el criterio se dispara."""
        val: float = ctx.get_metric(self.metric_key, DEFAULT_METRIC_VAL)
        
        if not _check_metric_integrity(val) or val < 0 or not self._evaluate_metric(val):
            return None
            
        try:
            msg: str = str(self.message_format.format(val))[:_MAX_MSG_CHUNK]
            return msg if _ensure_safe_text(msg) else None
        except (ValueError, TypeError, KeyError):
            return None

class AreaExplanation(NamedTuple):
    """Mapeo para descripciones pedagógicas de cada área de la aplicación."""
    key: str
    description: str

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

# Constantes de seguridad y regex
_ENDPOINT_BASE: Final[str] = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TIMEOUT_SECONDS: Final[int] = 30
_API_HOST_ROOT: Final[str] = "https://generativelanguage.googleapis.com/"

# Regex de seguridad para prevenir inyecciones de código o rutas del sistema
_REGEX_STRUCTURE_INJECTION: Final[re.Pattern] = re.compile(r"([a-zA-Z]:[\\/]|/|\\|\.\.|\0|[\u202e\u202d\u200e\u200f])")
_REGEX_CONTROL_CHARS: Final[re.Pattern] = re.compile(r"[\x00-\x1f\x7f\u0080-\u009f\u202b-\u202f\u200b-\u200d\uFEFF]")
_REGEX_PATH_TRAVERSAL: Final[re.Pattern] = re.compile(r"(\.\.[\\/])|([\\/]\.\.)", re.IGNORECASE)
_REGEX_ANSI_ESCAPE: Final[re.Pattern] = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_REGEX_POWERSHELL_CMDS: Final[re.Pattern] = re.compile(r"(Get-|Remove-|Set-|Stop-|Start-)[a-zA-Z]+", re.IGNORECASE)
_REGEX_EXEC_FUNCTIONS: Final[re.Pattern] = re.compile(r"(exec|eval|subprocess|system\s*\(|rm\s+|del\s+|cmd\.exe|powershell|reg\.exe)", re.IGNORECASE)
_REGEX_SYSTEM_PATHS: Final[re.Pattern] = re.compile(r"(\\\\|[a-z]:\\|/etc/|\\\\UNC|C:\\Windows|System32|/proc/|/dev/)", re.IGNORECASE)

SECURITY_PATTERNS: Final[list[re.Pattern]] = [
    _REGEX_STRUCTURE_INJECTION,
    _REGEX_CONTROL_CHARS,
    _REGEX_PATH_TRAVERSAL,
    _REGEX_ANSI_ESCAPE,
    _REGEX_POWERSHELL_CMDS,
    _REGEX_EXEC_FUNCTIONS,
    _REGEX_SYSTEM_PATHS
]

_TOKEN_REGEX: Final[re.Pattern] = re.compile(r"\w+")
_MODEL_NAME_REGEX: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9\.\-_]{1,64}$")
_API_KEY_REGEX: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9_\-\.]{1,128}$")

# Definición de umbrales para salud del sistema
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
    """Convierte cualquier valor a float asegurando que el resultado sea finito, real y no negativo."""
    try:
        if val is None or isinstance(val, bool) or not isinstance(val, (int, float, str)):
            return default
        f = float(val)
        return f if _check_metric_integrity(f) and f >= 0 else default
    except (TypeError, ValueError):
        return default

def _validate_response_length(text: Any) -> str:
    """Trunca el texto asegurando que no exceda el límite definido."""
    if not isinstance(text, str): return ""
    return text[:_MAX_TEXT_LENGTH]

def _is_safe_payload_structure(val: Any, depth: int = 0) -> bool:
    """Valida que los datos del payload no contengan estructuras recursivas profundas o tipos inválidos."""
    if depth > _MAX_NESTING_DEPTH: return False
    if isinstance(val, (list, tuple)):
        if len(val) > 10: return False
        return all(_is_safe_payload_structure(i, depth + 1) for i in val)
    if isinstance(val, dict):
        if len(val) > 20: return False
        return all(isinstance(k, str) and _is_safe_payload_structure(v, depth + 1) for k, v in val.items())
    return isinstance(val, (str, int, float, bool, type(None)))

def _is_input_too_deep_or_complex(val: Any, depth: int = 0) -> bool:
    """Recursivamente detecta estructuras de datos excesivamente anidadas o grandes."""
    if depth > _MAX_NESTING_DEPTH: return True
    try:
        if isinstance(val, (list, tuple, set)):
            if len(val) > 20: return True
            return any(_is_input_too_deep_or_complex(item, depth + 1) for item in val)
        elif isinstance(val, dict):
            if len(val) > 50: return True
            return any(_is_input_too_deep_or_complex(k, depth + 1) or _is_input_too_deep_or_complex(v, depth + 1) for k, v in val.items())
    except Exception:
        return True
    return False

def _is_metric_within_bounds(val: float, spec: MetricSpec) -> bool:
    """Verifica si un valor numérico está dentro del rango lógico definido por su especificación."""
    return _check_metric_integrity(val) and spec.min_val <= val <= spec.max_val

@dataclass
class SystemContext:
    """
    Contenedor de estado consolidado que representa la salud del sistema.
    
    Ingesta datos de diagnóstico, aplicando validaciones de tipo y seguridad
    mediante `MetricSpec`. Actúa como la única fuente de verdad para el motor
    del asistente, anonimizando la información antes de cualquier uso.
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
        """Retorna una métrica numérica validada o el valor por defecto si falla."""
        val = getattr(self, key, None)
        if not _check_metric_integrity(val if isinstance(val, (int, float)) else -1.0):
            return default
        return float(val)

    @property
    def active_problems(self) -> tuple[str, ...]:
        """Evalúa los criterios de salud contra los datos actuales y retorna los problemas activos."""
        if not self.analyzed: return ()
        return tuple(p for p in (c.format_if_triggered(self) for c in _CRITERIOS_SALUD) if p is not None)

    @property
    def is_empty(self) -> bool:
        """Verifica si el contexto contiene información válida tras el análisis."""
        return not self.analyzed or self.score is None or not isinstance(self.score, int) or self.score < 0

    def __hash__(self) -> int:
        return hash((self.score, self.grade, self.junk_mb, self.suspicious_count, 
                     self.memory_available_percent, self.disk_free_percent,
                     self.duplicate_mb, self.startup_count))

    @property
    def is_valid_structure(self) -> bool:
        """Validación de seguridad para impedir inyecciones en el campo de grado."""
        return _ensure_safe_text(self.grade) if self.grade else True

    def _apply_field(self, source: Any, key: str, spec: MetricSpec) -> Any:
        """Valida y normaliza un campo individual de la fuente de datos."""
        val = _get_source_value(source, key)
        if val is None or not spec.is_valid_type(val): return None
        try:
            float_val = float(val)
            if not _is_metric_within_bounds(float_val, spec): return None
            return spec.cast_func(float_val)
        except (TypeError, ValueError):
            return None

    def _clean_grade(self, val: Any) -> str:
        """Limpia el string de calificación eliminando caracteres de control no seguros."""
        if not isinstance(val, str): return ""
        clean = _REGEX_CONTROL_CHARS.sub(" ", val)[:10].strip()
        return clean if _ensure_safe_text(clean) and not is_protected_path(clean) else ""

    def _validate_ingestion_source(self, source: Any) -> bool:
        """Realiza comprobaciones de seguridad sobre el objeto fuente antes de ingestarlo."""
        if source is None: return False
        if not isinstance(source, (dict, object)): return False
        return not _is_input_too_deep_or_complex(source)

    def ingest(self, source: Any) -> bool:
        """Normaliza e importa datos externos al contexto local de manera transaccional."""
        if not self._validate_ingestion_source(source): return False
        
        has_updates = False
        try:
            for key, spec in _VALIDATORS.items():
                res = self._apply_field(source, key, spec)
                if res is not None and math.isfinite(float(res)):
                    object.__setattr__(self, key, res)
                    has_updates = True
            
            if (grade_val := self._clean_grade(_get_source_value(source, "grade"))):
                object.__setattr__(self, 'grade', grade_val)
                has_updates = True
            
            if has_updates:
                object.__setattr__(self, 'analyzed', True)
                return True
        except (AttributeError, TypeError, ValueError) as e:
            logging.error(f"Falla durante la ingestión de contexto: {str(e)[:50]}")
        return False

@dataclass
class Answer:
    """Encapsula la respuesta final del asistente, incluyendo metadatos de fuente."""
    text: str
    source: str = "local"
    notice: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        return self.source == "gemini"

def _is_safe_path_input(text: str) -> bool:
    """Verifica si el texto parece contener una ruta o referencia de sistema."""
    if is_protected_path(text): return True
    return bool(_REGEX_STRUCTURE_INJECTION.search(text) or _REGEX_PATH_TRAVERSAL.search(text) or _REGEX_SYSTEM_PATHS.search(text))

def _ensure_safe_text(text: Any) -> bool:
    """Realiza una desinfección estricta y validación de seguridad sobre cadenas."""
    if not isinstance(text, str) or not text or len(text) > _MAX_TEXT_LENGTH:
        return False
    if _REGEX_CONTROL_CHARS.search(text) or any(c in text for c in "<>|&^"):
        return False
    
    if _is_safe_path_input(text):
        return False
    
    return not any(pattern.search(text) for pattern in SECURITY_PATTERNS)

def _get_source_value(source: Any, key: str) -> Any:
    """Acceso seguro a atributos evitando recursión, inyecciones de clase y acceso a métodos."""
    if not _is_safe_key(key): return None
    try:
        val = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
        return None if callable(val) or isinstance(val, type) else val
    except Exception:
        return None

def build_context(metrics: Any = None, health: Any = None, **extra: Any) -> SystemContext:
    """Crea un objeto SystemContext a partir de múltiples fuentes de datos de análisis."""
    ctx = SystemContext()
    for s in (metrics, health, extra):
        if s is not None:
            ctx.ingest(s)
    return ctx

def _generate_safe_context(ctx: SystemContext) -> str:
    """Genera un resumen textual del contexto validando cada métrica estrictamente."""
    res = []
    for key, unit, precision in _CONTEXT_SCHEMA:
        val = ctx.get_metric(key, -1.0)
        if val >= 0:
            res.append(f"{key}: {val:.{precision}f}{unit}")
    return "\n".join(res)

@lru_cache(maxsize=16)
def _generate_context_cached(ctx: SystemContext) -> str:
    """Resumen del sistema optimizado mediante caché para prompts de IA."""
    return _generate_safe_context(ctx)

def context_as_text(context: SystemContext) -> str:
    """Serializa el contexto a un formato textual seguro para el prompt del asistente."""
    return _generate_safe_context(context) if not context.is_empty else ""

def _fmt_metric(val: Any, unit: str = "", decimal: int = 0) -> str:
    """Formatea métricas numéricas convirtiéndolas a strings legibles."""
    f = _safe_float(val, -1.0)
    if f < 0: return "N/A"
    return f"{f:.{decimal}f}{unit}"

def explain_area(area: Any) -> str:
    """Retorna una explicación amigable para el usuario sobre un área del sistema."""
    if not isinstance(area, str):
        return "No tengo una explicación para esa área."
    return _validate_response_length(_EXPLANATION_MAP.get(area.strip().lower(), "No tengo una explicación para esa área."))

@lru_cache(maxsize=128)
def _format_problem_message(problems: tuple[str, ...], score: int | str) -> str:
    """Crea una oración descriptiva basada en los problemas de salud detectados."""
    clean_score = str(score)
    if not problems:
        return f"Tu sistema está en buen estado ({clean_score}/100). No hay nada urgente."
    return f"Con un puntaje de {clean_score}/100, por orden de prioridad: {', '.join(problems)}."

@_safe_handler_wrapper
def handle_ram(ctx: SystemContext, user_query: str) -> Answer:
    """Gestiona las consultas relacionadas con el estado de la memoria RAM."""
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
    """Gestiona las consultas relacionadas con el almacenamiento y limpieza."""
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
    """Gestiona las consultas sobre archivos sospechosos y seguridad."""
    count = int(ctx.get_metric("suspicious_count", 0.0))
    warn = int(ctx.get_metric("suspicious_warnings", 0.0))
    if count == 0:
        texto = "No hay archivos sospechosos. La app nunca borra sola, todo va a revisión."
    else:
        info = f"Hay {count} archivo(s) marcados, {warn} con advertencia."
        sugerencia = "Son señales, no una condena: si no reconocés alguno, usá 'Aislar hallazgos'."
        texto = f"{info} {sugerencia} La limpieza solo mueve a cuarentena."
    return Answer(_validate_response_length(texto), notice=OFFLINE_NOTICE)

@lru_cache(maxsize=32)
def _get_score_response(score: int | None, grade: str, problems: tuple[str, ...]) -> str:
    """Genera y cachea la explicación del puntaje de salud."""
    score_val = score if score is not None else "N/A"
    grade_str = grade if grade else ""
    score_display = f"Tu puntaje es {score_val}/100{f' (nota {grade_str})' if grade_str else ''}."
    
    resumen = ("Lo que más te está restando: " + ", ".join(problems[:3]) + ".") if problems else "No hay nada urgente."
    explicacion = " El puntaje combina basura, seguridad, memoria, disco, duplicados y programas de inicio."
    return f"{score_display} {resumen}{explicacion}"

@_safe_handler_wrapper
def handle_score(ctx: SystemContext, user_query: str) -> Answer:
    """Gestiona las consultas sobre el puntaje de salud global."""
    return Answer(
        _validate_response_length(_get_score_response(ctx.score, ctx.grade, ctx.active_problems)), 
        notice=OFFLINE_NOTICE
    )

@_safe_handler_wrapper
def handle_startup(ctx: SystemContext, user_query: str) -> Answer:
    """Gestiona las consultas sobre aplicaciones de arranque."""
    count = int(ctx.get_metric("startup_count", 0.0))
    estado = f"Tenés {count} programas que arrancan con Windows."
    valoracion = "Son bastantes, y cada uno suma tiempo de encendido." if count > 15 else ("Es normal." if count > 8 else "Está bien.")
    cierre = " La app los lista, pero desactivalos desde el Administrador de tareas de Windows."
    return Answer(_validate_response_length(f"{estado} {valoracion}{cierre}"), notice=OFFLINE_NOTICE)

_TOKENS_MAP: Final[dict[str, Callable[[SystemContext, str], Answer]]] = {
    "ram": handle_ram, "memoria": handle_ram, "lenta": handle_ram, "lento": handle_ram, "acelerar": handle_ram,
    "espacio": handle_disk, "disco": handle_disk, "lleno": handle_disk, "recuperar": handle_disk, "liberar": handle_disk,
    "seguro": handle_security, "virus": handle_security, "sospechos": handle_security, "borrar": handle_security, "peligro": handle_security,
    "puntaje": handle_score, "salud": handle_score, "nota": handle_score, "score": handle_score,
    "inicio": handle_startup, "arranque": handle_startup, "arranca": handle_startup, "encender": handle_startup
}

def _sanitize_query(question: str) -> str:
    """Limpia y trunca la consulta del usuario para prevenir abusos de longitud o inyección."""
    if not isinstance(question, str): return ""
    clean = _REGEX_CONTROL_CHARS.sub(' ', question).strip()[:100]
    return clean if _ensure_safe_text(clean) else ""

def local_answer(question: str, context: SystemContext) -> Answer:
    """Motor de inferencia local: resuelve consultas basándose en heurísticas de datos."""
    if context.is_empty:
        return Answer(
            text="Todavía no corriste ningún análisis. Andá a la pestaña Salud "
                 "y apretá 'Analizar el sistema': es de solo lectura.",
            notice=OFFLINE_NOTICE,
            suggestions=SUGGESTED_QUESTIONS_SHORT,
        )

    q_sanitized = _sanitize_query(question)
    if not q_sanitized:
        return Answer("Entrada no válida.")
    
    for token in _TOKEN_REGEX.findall(q_sanitized.lower()):
        if handler := _TOKENS_MAP.get(token):
            return handler(context, question)
            
    cuerpo = _format_problem_message(context.active_problems, context.score or "N/A")
    ans = Answer(_validate_response_length(cuerpo), notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_SHORT)
    return ans if ans.text else Answer("No pude procesar tu consulta correctamente.")

def available(base: str | Path | None = None) -> bool:
    """Determina si la consulta a IA está habilitada globalmente mediante settings."""
    try:
        return settings.assistant_enabled(base)
    except (TypeError, ValueError, AttributeError, OSError):
        return False

def _parse_config(raw_cfg: Any) -> AssistantConfig:
    """Parsea la configuración de usuario para el asistente, con valores seguros por defecto."""
    default = AssistantConfig("", "gemini-3.1-flash-lite", True)
    if not isinstance(raw_cfg, dict):
        return default
    
    try:
        api_key = raw_cfg.get("asistente_api_key")
        model = raw_cfg.get("asistente_modelo")
        metrics_val = raw_cfg.get("asistente_enviar_metricas")
        
        return AssistantConfig(
            api_key=str(api_key) if isinstance(api_key, str) else "",
            model=str(model) if isinstance(model, str) and len(str(model)) < 64 else "gemini-3.1-flash-lite",
            allow_metrics=bool(metrics_val) if isinstance(metrics_val, bool) else True
        )
    except Exception:
        return default

def _build_payload(question: str, context_text: str) -> Optional[bytes]:
    """Serializa la pregunta y el contexto en el formato JSON esperado por Gemini."""
    if not all([_ensure_safe_text(context_text), _ensure_safe_text(SYSTEM_PROMPT)]): 
        return None
    
    q = _sanitize_query(question)
    if not q or not _ensure_safe_text(q): return None
    
    full_prompt = f"{SYSTEM_PROMPT}\n\nMétricas:\n{context_text}\n\nPregunta: {q}"
    
    payload_data = {"contents": [{"parts": [{"text": full_prompt}]}]}
    if not _is_safe_payload_structure(payload_data): return None
    
    if len(full_prompt) > _MAX_PROMPT_LIMIT or _is_input_too_deep_or_complex(full_prompt): 
        return None
        
    try:
        payload = json.dumps(payload_data).encode("utf-8")
        return payload if len(payload) <= (_MAX_RESPONSE_BYTES // 2) else None
    except (TypeError, ValueError, AttributeError):
        return None

def _extract_text_from_gemini_json(data: Any) -> Optional[str]:
    """Extrae de manera segura el contenido textual de la respuesta JSON del motor remoto."""
    if not isinstance(data, dict): return None
        
    try:
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list) or not candidates: return None
        
        first_candidate = candidates[0]
        if not isinstance(first_candidate, dict) or first_candidate.get("finishReason") != "STOP": 
            return None
        
        parts = first_candidate.get("content", {}).get("parts", [])
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict): 
            return None
        
        text_val = parts[0].get("text")
        if isinstance(text_val, str):
            sanitized = _validate_response_length(text_val)
            return sanitized if _ensure_safe_text(sanitized) else None
    except (AttributeError, TypeError, IndexError, KeyError): 
        pass
    return None

def _call_gemini(question: str, context_text: str, api_key: str, model: str) -> Optional[str]:
    """Realiza una petición POST segura a la API de Google."""
    if not isinstance(api_key, str) or not _API_KEY_REGEX.match(api_key) or not _MODEL_NAME_REGEX.match(model) or not context_text:
        return None
    
    payload = _build_payload(question, context_text)
    if not payload: return None
    
    target_url = _ENDPOINT_BASE.format(model=model)
    if not target_url.startswith(_API_HOST_ROOT) or re.search(r"[<>\s]", target_url):
        return None
        
    url = f"{target_url}?key={api_key}"
    
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as res:
            if res.status != 200: 
                return None
            raw_res = res.read(_MAX_RESPONSE_BYTES + 1)
            if not isinstance(raw_res, bytes) or len(raw_res) > _MAX_RESPONSE_BYTES: return None
            
            data = json.loads(raw_res.decode("utf-8"))
            if not _is_safe_payload_structure(data): return None
            
            raw_text = _extract_text_from_gemini_json(data)
            
            if isinstance(raw_text, str) and _ensure_safe_text(raw_text):
                return raw_text.strip()
            return None
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, KeyError, json.JSONDecodeError):
        return None

def ask(question: str, context: SystemContext | None = None,
        base: str | Path | None = None) -> Answer:
    """Punto de acceso único que orquesta los motores local y remoto."""
    if not _ensure_safe_text(question):
        return Answer("Entrada no válida.")
        
    ctx: SystemContext = context if isinstance(context, SystemContext) else SystemContext()
    respaldo: Answer = local_answer(question, ctx)
    if not available(base):
        return respaldo
    try:
        settings_data = settings.load(base)
        cfg = _parse_config(settings_data)
        
        if ctx.is_empty and cfg.allow_metrics:
            return respaldo
            
        texto_contexto = context_as_text(ctx) if cfg.allow_metrics else "El usuario no autorizó enviar métricas."
        remoto = _call_gemini(question, texto_contexto, cfg.api_key, cfg.model)
        if not remoto:
            respaldo.notice = "No se pudo consultar al asistente en línea, respondí con el motor local."
            return respaldo
        return Answer(remoto, source="gemini", notice=PRIVACY_NOTICE)
    except Exception:
        return respaldo
