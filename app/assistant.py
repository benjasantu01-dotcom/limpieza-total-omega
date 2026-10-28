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
import os
from itertools import islice
from functools import lru_cache, wraps, cached_property
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeAlias, Callable, Optional, Union, NamedTuple, Iterator, Dict

import settings
from safety import is_protected_path

# Type definitions for clarity
MetricsDict: TypeAlias = Dict[str, float]
ValidationSpec: TypeAlias = Dict[str, "MetricSpec"]

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
_MAX_NESTING_DEPTH: Final[int] = 1
_MAX_COLLECTION_SIZE: Final[int] = 20
_MAX_GRADE_LEN: Final[int] = 10

# Esquema que define qué métricas se serializan al motor remoto (clave, sufijo, precisión)
_CONTEXT_SCHEMA: Final = (
    ("score", "", 0), ("junk_mb", " MB", 0), ("suspicious_count", "", 0), 
    ("memory_available_percent", "%", 0), ("disk_free_percent", "%", 0),
    ("duplicate_mb", " MB", 0), ("startup_count", "", 0)
)

# Conjunto optimizado para validación rápida de caracteres prohibidos
_FORBIDDEN_CHARS: Final[set[str]] = set("<>|&^$():;\n\r\t\"'\\`")

def _is_nan_or_inf(val: Any) -> bool:
    """Verifica si un valor es NaN o Infinito, evitando errores de cálculo."""
    if not isinstance(val, (int, float)): return True
    return math.isnan(val) or math.isinf(val)

def _is_safe_key(key: str) -> bool:
    """Valida que la clave pertenezca a la lista blanca de atributos métricos permitidos."""
    if not isinstance(key, str): return False
    return key in _VALIDATORS or key in ("grade", "analyzed")

def _check_metric_integrity(val: Any) -> bool:
    """Verifica que un valor numérico sea seguro, finito y coherente para el asistente."""
    return isinstance(val, (int, float)) and not isinstance(val, bool) and not _is_nan_or_inf(val)

def _is_safe_numeric_context(val: Any) -> bool:
    """Valida que un valor numérico no exceda rangos críticos para prevenir desbordamientos."""
    if not _check_metric_integrity(val): return False
    return -1e12 < float(val) < 1e12

def _is_valid_numeric_update(val: Any, spec: MetricSpec) -> Optional[float]:
    """
    Valida si un valor es convertible a métrica numérica. 
    Verifica tipos, límites de la especificación y seguridad ante overflow.
    """
    if val is None or isinstance(val, bool):
        return None
    try:
        f: float = float(val)
        if _is_safe_numeric_context(f) and spec.is_valid_type(f) and _is_metric_within_bounds(f, spec):
            return f
    except (ValueError, TypeError, OverflowError):
        pass
    return None

def _get_safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    """Accede defensivamente a atributos del objeto evitando llamadas a métodos ejecutables."""
    if not _is_safe_key(name): return default
    try:
        val: Any = getattr(obj, name, default)
        return val if not callable(val) else default
    except Exception:
        return default

def _safe_handler_wrapper(func: Callable[[SystemContext, str], Answer]) -> Callable[[SystemContext, str], Answer]:
    """Decorador para proteger manejadores de consulta ante excepciones y asegurar contexto válido."""
    @wraps(func)
    def wrapper(ctx: SystemContext, q: str) -> Answer:
        if not isinstance(ctx, SystemContext): 
            return Answer("Error de contexto.")
        try:
            if ctx.is_empty:
                return Answer("Primero analizá el sistema.")
            result: Answer = func(ctx, q)
            if isinstance(result, Answer) and result.text and len(result.text) > 0:
                return result
            logging.error(f"Handler {func.__name__} devolvió respuesta vacía")
        except (AttributeError, TypeError, Exception) as e:
            logging.error(f"Falla inesperada en handler {func.__name__}: {e}")
        return Answer("Error al procesar la respuesta.")
    return wrapper

@dataclass(frozen=True)
class AssistantConfig:
    """Configuración inmutable para el cliente de IA (Gemini)."""
    api_key: str
    model: str
    allow_metrics: bool

@dataclass(frozen=True)
class MetricSpec:
    """Define las restricciones de tipo, rango mínimo y máximo para una métrica numérica."""
    type_hint: type
    min_val: float
    max_val: float

    def is_valid_type(self, val: Any) -> bool:
        """Verifica que el valor coincida con el tipo permitido y no sea booleano."""
        return isinstance(val, (int, float)) and not isinstance(val, bool) and _check_metric_integrity(val)

class ProblemCriterion(NamedTuple):
    """
    Define una regla lógica para evaluar el estado del sistema.
    
    Attributes:
        metric_key: Nombre del atributo en SystemContext a evaluar.
        threshold: Valor límite para comparar.
        operator: Comparador lógico ('<' o '>').
        message_format: Plantilla de mensaje con un marcador '{:.0f}' o '{:d}'.
    """
    metric_key: str
    threshold: float
    operator: str
    message_format: str

    def _evaluate_metric(self, val: float) -> bool:
        """Determina si la métrica excede el umbral basándose en el operador lógico configurado."""
        if not _check_metric_integrity(val):
            return False
        return (val < self.threshold) if self.operator == "<" else (val > self.threshold)

    def is_triggered_by(self, val: float) -> bool:
        """Verifica si el valor es positivo y cumple la condición de umbral de riesgo."""
        return val >= 0 and self._evaluate_metric(val)

    def format_if_triggered(self, val: float) -> Optional[str]:
        """Formatea el mensaje de advertencia solo si la condición es verdadera y es texto seguro."""
        if not self.is_triggered_by(val):
            return None
        try:
            msg: str = str(self.message_format.format(val))[:_MAX_MSG_CHUNK]
            return msg if _ensure_safe_text(msg) else None
        except (ValueError, TypeError, KeyError):
            return None

# Definición centralizada de criterios de salud
_CRITERIOS_SALUD: Final[tuple[ProblemCriterion, ...]] = (
    ProblemCriterion("disk_free_percent", 10.0, "<", "{:.0f}% de disco libre"),
    ProblemCriterion("suspicious_warnings", 0, ">", "{:d} archivo(s) sospechosos"),
    ProblemCriterion("memory_available_percent", 15.0, "<", "{:.0f}% de RAM"),
    ProblemCriterion("junk_mb", 1000.0, ">", "{:.0f} MB de basura"),
    ProblemCriterion("duplicate_mb", 500.0, ">", "{:.0f} MB en duplicados"),
    ProblemCriterion("startup_count", 15, ">", "{:d} programas de inicio")
)

class AreaExplanation(NamedTuple):
    """Mapeo para descripciones pedagógicas de áreas de la aplicación."""
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
_REGEX_SHELL_INJECTION: Final[re.Pattern] = re.compile(r"([&|<>!])")
_REGEX_UNICODE_FORMAT: Final[re.Pattern] = re.compile(r"[\u2000-\u200f\u202a-\u202e\u2060-\u206f]")

SECURITY_PATTERNS: Final[list[re.Pattern]] = [
    _REGEX_STRUCTURE_INJECTION,
    _REGEX_CONTROL_CHARS,
    _REGEX_PATH_TRAVERSAL,
    _REGEX_ANSI_ESCAPE,
    _REGEX_POWERSHELL_CMDS,
    _REGEX_EXEC_FUNCTIONS,
    _REGEX_SYSTEM_PATHS,
    _REGEX_SHELL_INJECTION,
    _REGEX_UNICODE_FORMAT
]

_TOKEN_REGEX: Final[re.Pattern] = re.compile(r"\w+")
_MODEL_NAME_REGEX: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9\.\-_]{1,64}$")
_API_KEY_REGEX: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9_\-\.]{1,128}$")

_EXPLANATION_MAP: Final[Dict[str, str]] = {
    "basura": "Archivos temporales y restos de instaladores: ocupan espacio innecesario sin aportar valor operativo.",
    "seguridad": "Archivos con señales de riesgo: extensiones inusuales o ejecutables sin firma, requieren revisión manual.",
    "memoria": "Recursos de acceso rápido: si la memoria disponible es baja, Windows utiliza el disco duro, ralentizando todo.",
    "disco": "Almacenamiento disponible: niveles inferiores al 10% afectan la estabilidad y velocidad de escritura de Windows.",
    "duplicados": "Copias idénticas del mismo archivo: se pueden eliminar de forma segura ya que el archivo original permanece.",
    "inicio": "Programas que arrancan con Windows: cada entrada incrementa el tiempo de inicio y el consumo base de memoria.",
}

# Diccionario central de especificaciones: define rangos lógicos y tipos para validación de datos
_VALIDATORS: Final[ValidationSpec] = {
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

# Claves validadas pre-calculadas para rendimiento
_VALID_METRIC_KEYS: Final[set[str]] = set(_VALIDATORS.keys())

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Convierte cualquier valor a float asegurando que el resultado sea finito, real y no negativo."""
    try:
        if not _is_safe_numeric_context(val):
            return default
        f: float = float(val)
        return f if f >= 0.0 else default
    except (TypeError, ValueError):
        return default

def _validate_response_length(text: Any) -> str:
    """Trunca el texto asegurando que no exceda el límite definido y no sea vacío."""
    if not isinstance(text, str) or not text: return "Respuesta no disponible."
    return text[:_MAX_TEXT_LENGTH]

def _is_safe_payload_structure(val: Any, depth: int = 0) -> bool:
    """Verifica de forma recursiva que la estructura de datos sea plana y contenga tipos permitidos."""
    if depth > _MAX_NESTING_DEPTH: return False
    if isinstance(val, (int, float, bool, str)) or val is None: return True
    if isinstance(val, (list, tuple, set)):
        if len(val) > _MAX_COLLECTION_SIZE: return False
        return all(isinstance(item, (int, float, bool, str)) for item in val)
    if isinstance(val, dict):
        if len(val) > _MAX_COLLECTION_SIZE: return False
        return all(isinstance(k, str) and isinstance(v, (int, float, bool, str)) for k, v in val.items())
    return False

def _is_input_too_deep_or_complex(val: Any, depth: int = 0) -> bool:
    """Realiza un escaneo de seguridad para detectar estructuras anidadas o tipos inválidos."""
    if depth > _MAX_NESTING_DEPTH: return True
    if val is None or isinstance(val, (int, float, bool, str)): return False
    if isinstance(val, (list, tuple, set)):
        return len(val) > _MAX_COLLECTION_SIZE or any(_is_input_too_deep_or_complex(item, depth + 1) for item in val)
    if isinstance(val, dict):
        return len(val) > _MAX_COLLECTION_SIZE or any(_is_input_too_deep_or_complex(v, depth + 1) for v in val.values())
    return True

def _is_metric_within_bounds(val: float, spec: MetricSpec) -> bool:
    """Verifica si un valor numérico está dentro del rango lógico definido por su especificación."""
    return spec.min_val <= val <= spec.max_val

@lru_cache(maxsize=16)
def _evaluate_active_problems(metrics_tuple: tuple) -> tuple[str, ...]:
    """Calcula los problemas activos a partir de los valores crudos cacheables."""
    metrics_dict = dict(metrics_tuple)
    return tuple(
        msg for c in _CRITERIOS_SALUD 
        if (msg := c.format_if_triggered(metrics_dict.get(c.metric_key, -1.0))) is not None
    )

@dataclass
class SystemContext:
    """
    Contenedor de estado consolidado que representa la salud del sistema. 
    Almacena métricas numéricas, califica la salud y gestiona su validación/exposición.
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

    @cached_property
    def metrics_snapshot(self) -> MetricsDict:
        """Snapshot cacheado de las métricas numéricas para acceso eficiente."""
        return {k: float(v) for k in _VALID_METRIC_KEYS if (v := getattr(self, k, -1.0)) != -1.0}

    @cached_property
    def _cached_active_problems(self) -> tuple[str, ...]:
        """Versión cacheada de active_problems, usando valores ordenados según los criterios."""
        if self.analyzed is not True: return ()
        snap = self.metrics_snapshot
        key_vals = tuple((c.metric_key, snap.get(c.metric_key, -1.0)) for c in _CRITERIOS_SALUD)
        return _evaluate_active_problems(key_vals)

    @property
    def active_problems(self) -> tuple[str, ...]:
        """Evalúa los criterios de salud contra los datos actuales y retorna los problemas activos."""
        return self._cached_active_problems

    @cached_property
    def _hash_tuple(self) -> tuple:
        """Tupla cacheada para representar el estado actual para hashing."""
        return (self.score, self.grade, self.junk_mb, self.suspicious_count, 
                self.memory_available_percent, self.disk_free_percent,
                self.duplicate_mb, self.startup_count)

    def get_metric(self, key: str, default: float) -> float:
        """Retorna una métrica numérica validada o el valor por defecto si falla."""
        return self.metrics_snapshot.get(key, default)

    @property
    def is_empty(self) -> bool:
        """Verifica si el contexto contiene información válida tras el análisis."""
        if self.analyzed is not True: return True
        if not isinstance(self.score, int): return True
        return self.score < 0

    def __hash__(self) -> int:
        return hash(self._hash_tuple)

    @property
    def is_valid_structure(self) -> bool:
        """Validación de seguridad para impedir inyecciones en el campo de grado."""
        return _ensure_safe_text(self.grade) if (self.grade is not None and len(self.grade) > 0) else True

    def _apply_field(self, source: Any, key: str, spec: MetricSpec) -> Any:
        """Valida, convierte y verifica límites de un campo individual según su especificación."""
        try:
            val = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
            if val is None or isinstance(val, bool) or not isinstance(val, (int, float, str)) or callable(val):
                return None
            valid_val = _is_valid_numeric_update(val, spec)
            return spec.type_hint(valid_val) if valid_val is not None else None
        except (ValueError, TypeError, AttributeError, Exception):
            return None

    def _clean_grade(self, val: Any) -> str:
        """Limpia el string de calificación eliminando caracteres de control no seguros."""
        if not isinstance(val, str) or callable(val): return ""
        try:
            clean: str = _REGEX_CONTROL_CHARS.sub(" ", val)[:_MAX_GRADE_LEN].strip()
            if not clean or is_protected_path(clean) or not _ensure_safe_text(clean): 
                return ""
            return clean
        except (TypeError, Exception):
            return ""

    def _validate_and_sanitize_data(self, source: Any) -> Optional[dict[str, Any]]:
        """Extrae y valida métricas numéricas y texto de calificación de una fuente de forma transaccional."""
        if not isinstance(source, (dict, object)) or callable(source): return None
        updates: dict[str, Any] = {}
        try:
            for key, spec in _VALIDATORS.items():
                val = self._apply_field(source, key, spec)
                if val is not None: 
                    updates[key] = val
            
            g_val = source.get("grade") if isinstance(source, dict) else getattr(source, "grade", None)
            if g_val is not None and not callable(g_val):
                grade_val = self._clean_grade(g_val)
                if grade_val: updates["grade"] = grade_val
            
            return updates if updates else None
        except (AttributeError, Exception):
            return None

    def _validate_ingestion_source(self, source: Any) -> bool:
        """Realiza comprobaciones de seguridad sobre el objeto fuente."""
        if source is None or callable(source) or not isinstance(source, (dict, object)): 
            return False
        return not _is_input_too_deep_or_complex(source) if isinstance(source, (dict, list)) else True

    def _apply_updates(self, updates: dict[str, Any]) -> None:
        """Aplica cambios validados al contexto y limpia cachés internas."""
        try:
            for k, v in updates.items():
                setattr(self, k, v)
            self.analyzed = True
            for attr in ("metrics_snapshot", "_cached_active_problems", "_hash_tuple"):
                self.__dict__.pop(attr, None)
        except Exception:
            self.analyzed = False

    def ingest(self, source: Any) -> bool:
        """Normaliza e importa datos externos al contexto de manera transaccional."""
        if not self._validate_ingestion_source(source): return False
        try:
            updates = self._validate_and_sanitize_data(source)
            if updates is None: return False
            self._apply_updates(updates)
            return True
        except Exception:
            return False

@dataclass
class Answer:
    """Representa la respuesta textual del asistente, incluyendo metadatos de fuente y sugerencias."""
    text: str
    source: str = "local"
    notice: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        return self.source == "gemini"

def _is_safe_path_input(text: str) -> bool:
    """Verifica si el texto parece contener rutas o caracteres de control maliciosos."""
    if not isinstance(text, str): return False
    return not (_REGEX_STRUCTURE_INJECTION.search(text) or _REGEX_PATH_TRAVERSAL.search(text) or _REGEX_SYSTEM_PATHS.search(text) or is_protected_path(text))

def _contains_forbidden_patterns(text: str) -> bool:
    """Verifica si el texto contiene patrones de seguridad prohibidos."""
    return any(pattern.search(text) for pattern in SECURITY_PATTERNS)

def _has_invalid_meta_characters(text: str) -> bool:
    """Verifica si la cadena contiene caracteres de control, formato o escape ANSI."""
    return bool(_REGEX_ANSI_ESCAPE.search(text) or _REGEX_UNICODE_FORMAT.search(text) or any(c in _FORBIDDEN_CHARS for c in text))

def _is_safe_text_length(text: Any) -> bool:
    """Valida la longitud del texto."""
    return isinstance(text, str) and 0 < len(text) <= _MAX_TEXT_LENGTH

def _ensure_safe_text(text: Any) -> bool:
    """Desinfecta estricta sobre cadenas evitando inyecciones de comandos."""
    if not _is_safe_text_length(text): return False
    if is_protected_path(text) or any(is_protected_path(part) for part in text.split()): return False
    return not (_has_invalid_meta_characters(text) or _contains_forbidden_patterns(text) or not _is_safe_path_input(text))

def build_context(metrics: Any = None, health: Any = None, **extra: Any) -> SystemContext:
    """Crea y popula un objeto SystemContext a partir de fuentes de datos múltiples."""
    ctx: SystemContext = SystemContext()
    for s in (metrics, health, extra):
        if s is not None: ctx.ingest(s)
    return ctx

def context_as_text(context: SystemContext) -> str:
    """Serializa el contexto a un formato textual seguro para el prompt del asistente."""
    if context.is_empty: return ""
    snap: MetricsDict = context.metrics_snapshot
    lines = []
    for k, u, p in _CONTEXT_SCHEMA:
        if k in snap and snap[k] >= 0.0:
            val_str = f"{k}: {snap[k]:.{p}f}{u}"
            # Filtro adicional de seguridad defensiva para asegurar que la línea serializada sea segura
            if _ensure_safe_text(val_str):
                lines.append(val_str)
    return "\n".join(lines)

def explain_area(area: Any) -> str:
    """Retorna una explicación amigable para el usuario sobre un área del sistema."""
    if not isinstance(area, str): return "No tengo una explicación para esa área."
    return _validate_response_length(_EXPLANATION_MAP.get(area.strip().lower(), "No tengo una explicación para esa área."))

@lru_cache(maxsize=128)
def _format_problem_message(problems: tuple[str, ...], score: int | str) -> str:
    """Crea una oración descriptiva basada en los problemas de salud detectados."""
    clean_score: str = str(score)
    if not problems:
        return f"Tu sistema está en buen estado ({clean_score}/100). No hay nada urgente."
    return f"Con un puntaje de {clean_score}/100, por orden de prioridad: {', '.join(problems)}."

@_safe_handler_wrapper
def handle_ram(ctx: SystemContext, user_query: str) -> Answer:
    """Gestiona consultas sobre memoria RAM usando contexto validado."""
    snap: MetricsDict = ctx.metrics_snapshot
    mem_pct: float = snap.get("memory_available_percent", DEFAULT_RAM_PCT)
    total_gb: float = snap.get("memory_total_gb", 0.0)
    msg: str = f"Tenés {mem_pct:.0f}% de RAM disponible{f' de {total_gb:.0f} GB' if total_gb > 0.0 else ''}."
    if mem_pct < 15.0:
        msg += " Eso es poco: Windows está usando el disco como memoria y ahí se siente la lentitud. Cerrá lo que no uses."
    else:
        msg += " Eso está bien. Si la PC va lenta, el problema seguramente no es la RAM."
    msg += " No busques un 'liberador de RAM': la PC queda más lenta."
    startup_count: int = int(snap.get("startup_count", 0))
    if startup_count > 12:
        msg += f" Sí te conviene mirar los {startup_count} programas de inicio."
    return Answer(msg, notice=OFFLINE_NOTICE, suggestions=["¿Conviene desactivar programas de inicio?"])

@_safe_handler_wrapper
def handle_disk(ctx: SystemContext, user_query: str) -> Answer:
    """Gestiona consultas sobre almacenamiento y limpieza."""
    snap: MetricsDict = ctx.metrics_snapshot
    junk: float = snap.get("junk_mb", 0.0)
    dup: float = snap.get("duplicate_mb", 0.0)
    cache: float = snap.get("browser_cache_mb", 0.0)
    free: float = snap.get("disk_free_percent", 100.0)
    recuperable: float = junk + dup + cache
    msg: str = f"Tenés {free:.0f}% libre en disco. Podés recuperar cerca de {recuperable:.0f} MB."
    msg += f" (incluye {junk:.0f} MB de basura, {dup:.0f} MB de duplicados{f', {cache:.0f} MB caché' if cache > 0.0 else ''})."
    if free < 10.0:
        msg += " ¡Alerta! Estás por debajo del 10%, afecta la estabilidad."
    msg += " Empezá por Limpieza: mueve los candidatos a revisión."
    return Answer(msg, notice=OFFLINE_NOTICE)

@_safe_handler_wrapper
def handle_security(ctx: SystemContext, user_query: str) -> Answer:
    """Gestiona consultas sobre archivos sospechosos y seguridad."""
    snap: MetricsDict = ctx.metrics_snapshot
    count: int = int(snap.get("suspicious_count", 0.0))
    warn: int = int(snap.get("suspicious_warnings", 0.0))
    if count == 0:
        texto: str = "No hay archivos sospechosos. La app nunca borra sola, todo va a revisión."
    else:
        texto = f"Hay {count} archivo(s) marcados, {warn} con advertencia. Son señales, no una condena: si no reconocés alguno, usá 'Aislar hallazgos'."
    return Answer(texto, notice=OFFLINE_NOTICE)

@_safe_handler_wrapper
def handle_score(ctx: SystemContext, user_query: str) -> Answer:
    """Gestiona consultas sobre el puntaje de salud global."""
    resumen: str = _format_problem_message(ctx.active_problems, ctx.score if ctx.score is not None else "N/A")
    grade_str: str = f" (nota {ctx.grade})" if (ctx.grade is not None and len(ctx.grade) > 0) else ""
    explicacion: str = " El puntaje combina basura, seguridad, memoria, disco, duplicados y programas de inicio."
    return Answer(f"{resumen}{grade_str}{explicacion}", notice=OFFLINE_NOTICE)

@_safe_handler_wrapper
def handle_startup(ctx: SystemContext, user_query: str) -> Answer:
    """Gestiona consultas sobre aplicaciones de arranque."""
    count: int = int(ctx.metrics_snapshot.get("startup_count", 0.0))
    estado: str = f"Tenés {count} programas que arrancan con Windows."
    valoracion: str = "Son bastantes, y cada uno suma tiempo de encendido." if count > 15 else ("Es normal." if count > 8 else "Está bien.")
    cierre: str = " La app los lista, pero desactivalos desde el Administrador de tareas de Windows."
    return Answer(f"{estado} {valoracion}{cierre}", notice=OFFLINE_NOTICE)

_TOKENS_MAP: Final[dict[str, Callable[[SystemContext, str], Answer]]] = {
    "ram": handle_ram, "memoria": handle_ram, "lenta": handle_ram, "lento": handle_ram, "acelerar": handle_ram,
    "espacio": handle_disk, "disco": handle_disk, "lleno": handle_disk, "recuperar": handle_disk, "liberar": handle_disk,
    "seguro": handle_security, "virus": handle_security, "sospechos": handle_security, "borrar": handle_security, "peligro": handle_security,
    "puntaje": handle_score, "salud": handle_score, "nota": handle_score, "score": handle_score,
    "inicio": handle_startup, "arranque": handle_startup, "arranca": handle_startup, "encender": handle_startup
}

def _sanitize_query(question: str) -> str:
    """Limpia, trunca y desinfecta la consulta del usuario antes del procesamiento."""
    if not isinstance(question, str): return ""
    clean: str = " ".join(_REGEX_CONTROL_CHARS.sub(' ', question).split())[:100]
    return clean if _ensure_safe_text(clean) else ""

def local_answer(question: str, context: SystemContext) -> Answer:
    """Motor de inferencia local: resuelve consultas basándose en heurísticas de datos."""
    if context.is_empty:
        return Answer("Todavía no corriste ningún análisis. Andá a la pestaña Salud y apretá 'Analizar el sistema': es de solo lectura.", notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_SHORT)
    q_sanitized: str = _sanitize_query(question)
    if not q_sanitized: return Answer("Entrada no válida.")
    for token in _TOKEN_REGEX.findall(q_sanitized.lower()):
        # Validación defensiva extra: procesar tokens solo si pasan el filtro de texto seguro
        if _ensure_safe_text(token) and token in _TOKENS_MAP: 
            return _TOKENS_MAP[token](context, question)
    return Answer(_format_problem_message(context.active_problems, context.score if context.score is not None else "N/A"), notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_SHORT)

def available(base: str | Path | None = None) -> bool:
    """Determina si la consulta a IA está habilitada globalmente mediante settings."""
    try: return bool(settings.assistant_enabled(base))
    except Exception: return False

def _parse_config(raw_cfg: Any) -> AssistantConfig:
    """Parsea la configuración de usuario para el asistente manejando errores de estructura."""
    if not isinstance(raw_cfg, dict): return AssistantConfig("", "gemini-3.1-flash-lite", True)
    
    # La API key siempre se prioriza desde la variable de entorno para evitar persistencia insegura en disco
    api_key = os.environ.get("LTO_API_KEY", str(raw_cfg.get("asistente_api_key", "")))
    
    try:
        return AssistantConfig(
            api_key=api_key,
            model=str(m) if isinstance(m := raw_cfg.get("asistente_modelo"), str) and _MODEL_NAME_REGEX.match(m) else "gemini-3.1-flash-lite",
            allow_metrics=bool(raw_cfg.get("asistente_enviar_metricas", True))
        )
    except (TypeError, ValueError):
        return AssistantConfig("", "gemini-3.1-flash-lite", True)

def _build_payload(question: str, context_text: str) -> Optional[bytes]:
    """Serializa la pregunta y el contexto en el formato JSON esperado por Gemini."""
    if not context_text or not _ensure_safe_text(context_text): return None
    q: str = _sanitize_query(question)
    if not q or not _ensure_safe_text(q): return None
    full_prompt: str = f"{SYSTEM_PROMPT}\n\nMétricas:\n{context_text}\n\nPregunta: {q}"
    if len(full_prompt) > _MAX_PROMPT_LIMIT or not _ensure_safe_text(full_prompt) or any(c in full_prompt for c in _FORBIDDEN_CHARS): return None
    payload_data: dict[str, Any] = {"contents": [{"parts": [{"text": full_prompt}]}]}
    if not _is_safe_payload_structure(payload_data): return None
    try:
        encoded: bytes = json.dumps(payload_data).encode("utf-8")
        return encoded if len(encoded) <= _MAX_RESPONSE_BYTES else None
    except (TypeError, ValueError): return None

def _extract_text_from_gemini_json(data: Any) -> Optional[str]:
    """Extracte de manera segura el contenido textual de la respuesta JSON del motor remoto."""
    if not isinstance(data, dict) or not _is_safe_payload_structure(data): return None
    try:
        candidates = data.get("candidates")
        if isinstance(candidates, list) and candidates and candidates[0].get("finishReason") == "STOP":
            text_val = candidates[0].get("content", {}).get("parts", [{}])[0].get("text")
            if isinstance(text_val, str):
                sanitized: str = _validate_response_length(text_val)
                return sanitized if _ensure_safe_text(sanitized) else None
    except (KeyError, AttributeError, TypeError): return None
    return None

def _call_gemini(question: str, context_text: str, api_key: str, model: str) -> Optional[str]:
    """Realiza una petición POST segura a la API de Google."""
    if not (_API_KEY_REGEX.match(api_key) and _MODEL_NAME_REGEX.match(model) and context_text): return None
    payload: Optional[bytes] = _build_payload(question, context_text)
    if not payload: return None
    target_url: str = _ENDPOINT_BASE.format(model=model)
    if not (target_url.startswith(_API_HOST_ROOT) and not any(c in target_url for c in "<>\"' \t\n\r&?=")): return None
    try:
        req: urllib.request.Request = urllib.request.Request(f"{target_url}?key={api_key}", data=payload, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as res:
            if res.status != 200: return None
            raw_res: bytes = res.read(_MAX_RESPONSE_BYTES + 1)
            return _extract_text_from_gemini_json(json.loads(raw_res.decode("utf-8"))) if len(raw_res) <= _MAX_RESPONSE_BYTES else None
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError, ValueError): return None

def ask(question: str, context: Optional[SystemContext] = None, base: Optional[Union[str, Path]] = None) -> Answer:
    """Punto de acceso único que orquesta los motores local y remoto para responder consultas."""
    q_sanitized: str = _sanitize_query(question)
    if not q_sanitized: return Answer("Entrada no válida.")
    ctx: SystemContext = context if isinstance(context, SystemContext) else SystemContext()
    respaldo: Answer = local_answer(q_sanitized, ctx)
    if not available(base): return respaldo
    try:
        cfg: AssistantConfig = _parse_config(settings.load(base))
        if (ctx.is_empty and cfg.allow_metrics) or not ctx.is_valid_structure: return respaldo
        texto_contexto: str = context_as_text(ctx) if cfg.allow_metrics else "El usuario no autorizó enviar métricas."
        remoto: Optional[str] = _call_gemini(q_sanitized, texto_contexto, cfg.api_key, cfg.model)
        if remoto is None:
            respaldo.notice = "No se pudo consultar al asistente en línea, respondí con el motor local."
            return respaldo
        return Answer(remoto, source="gemini", notice=PRIVACY_NOTICE)
    except (Exception, TypeError, ValueError): return respaldo
