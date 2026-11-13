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
import urllib.parse
import re
import math
import logging
import operator
import os
import copy
from functools import lru_cache, wraps, cached_property
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Final, TypeAlias, Callable, Optional, Union, NamedTuple, 
    Dict, List, Set, Tuple, Mapping, Sequence, Iterable
)

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

class ContextMetric(NamedTuple):
    """Configuración para exportar una métrica al motor remoto."""
    key: str
    suffix: str
    precision: int

# Esquema que define qué métricas se serializan al motor remoto
_CONTEXT_SCHEMA: Final[tuple[ContextMetric, ...]] = (
    ContextMetric("score", "", 0),
    ContextMetric("junk_mb", " MB", 0),
    ContextMetric("suspicious_count", "", 0), 
    ContextMetric("memory_available_percent", "%", 0),
    ContextMetric("disk_free_percent", "%", 0),
    ContextMetric("duplicate_mb", " MB", 0),
    ContextMetric("startup_count", "", 0)
)

# Conjunto optimizado para validación rápida de caracteres prohibidos
_FORBIDDEN_CHARS: Final[set[str]] = set("<>|&^$():;\n\r\t\"'\\`")

def _is_nan_or_inf(val: Any) -> bool:
    """Verifica si el valor es un flotante no válido (NaN o Infinito)."""
    if not isinstance(val, (int, float)) or isinstance(val, bool): return True
    return math.isnan(val) or math.isinf(val)

def _is_safe_key(key: str) -> bool:
    """Valida que la clave de métrica no contenga caracteres de inyección."""
    if not isinstance(key, str) or any(c in _FORBIDDEN_CHARS for c in key): return False
    return key in _VALIDATORS or key in ("grade", "analyzed")

def _check_metric_integrity(val: Any) -> bool:
    """Verifica si el valor es un número real, finito y no un booleano."""
    return isinstance(val, (int, float)) and not isinstance(val, bool) and not _is_nan_or_inf(val)

def _is_safe_numeric_context(val: Any) -> bool:
    """Valida rango de seguridad para prevenir desbordamientos numéricos y tipos incorrectos."""
    if not _check_metric_integrity(val): return False
    # Evita pasar objetos que no sean números simples (ej: clases, funciones o tipos complejos)
    if isinstance(val, (Path, type, Callable)): return False
    return -1e12 < float(val) < 1e12

def _is_valid_numeric_update(val: Any, spec: MetricSpec) -> Optional[float]:
    """
    Intenta validar un valor crudo contra la especificación de tipo y rango.
    
    Returns: Valor validado o None si la entrada no cumple las reglas de seguridad.
    """
    if val is None or isinstance(val, (bool, dict, list, tuple, Path)):
        return None
    try:
        metric_val: float = float(val)
        if _is_safe_numeric_context(metric_val) and spec.is_valid_type(metric_val) and _is_metric_within_bounds(metric_val, spec):
            return metric_val
    except (ValueError, TypeError, OverflowError):
        pass
    return None

def _get_safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    """Accede a un atributo de objeto solo si el nombre de clave es seguro y el atributo es un escalar."""
    if obj is None or not _is_safe_key(name): return default
    try:
        if not hasattr(obj, name): return default
        val: Any = getattr(obj, name, default)
        # Solo permitimos escalares básicos como valores de atributo y validamos que no sean rutas protegidas
        if isinstance(val, (int, float, str, bool)) and not isinstance(val, bool):
            if isinstance(val, str) and is_protected_path(val):
                return default
            return val
        return default
    except (AttributeError, Exception):
        return default

def _ensure_query_is_safe(func: Callable[[SystemContext, str], Answer]) -> Callable[[SystemContext, str], Answer]:
    """Decorador: Bloquea consultas si detecta caracteres maliciosos antes de procesarlas."""
    @wraps(func)
    def wrapper(ctx: SystemContext, q: str) -> Answer:
        if not _ensure_safe_text(q):
            return Answer("Consulta bloqueada por motivos de seguridad.")
        return func(ctx, q)
    return wrapper

def _ensure_handler_is_safe(func: Callable[[SystemContext, str], Answer]) -> Callable[[SystemContext, str], Answer]:
    """Decorador: Asegura la integridad del contexto y captura errores para prevenir caídas."""
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
    """Configuración inmutable para la conexión con el motor de IA."""
    api_key: str
    model: str
    allow_metrics: bool

@dataclass(frozen=True)
class MetricSpec:
    """Define los límites, tipo esperado y validez para una métrica del sistema."""
    type_hint: type
    min_val: float
    max_val: float

    def is_valid_type(self, val: Any) -> bool:
        """Verifica que el valor sea numérico, no booleano y finito."""
        # Se asegura que la validación sea exacta contra el tipo (int vs float)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return False
        return _check_metric_integrity(val)

class ProblemCriterion(NamedTuple):
    """
    Representa una regla de salud del sistema.
    
    - metric_key: Nombre de la métrica a evaluar.
    - threshold: Punto de corte para determinar el problema.
    - op_type: Operador lógico ('<' o '>') para comparar contra el umbral.
    - message_format: Plantilla de texto para reportar el problema detectado.
    """
    metric_key: str
    threshold: float
    op_type: str 
    message_format: str

    @property
    def _op_func(self) -> Callable[[float, float], bool]:
        """Obtiene la función operadora (lt/gt) según la configuración del criterio."""
        return operator.lt if self.op_type == "<" else operator.gt

    def _evaluate_metric(self, val: float) -> bool:
        """Aplica el operador al valor y el umbral configurado."""
        if not _check_metric_integrity(val):
            return False
        return self._op_func(val, self.threshold)

    def is_triggered_by(self, val: float) -> bool:
        """Retorna True si el valor de la métrica excede el umbral crítico."""
        return val >= 0 and self._evaluate_metric(val)

    def format_if_triggered(self, val: float) -> Optional[str]:
        """Formatea el mensaje de alerta si la condición es cumplida."""
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

def _get_criterios_map() -> Dict[str, Tuple[ProblemCriterion, ...]]:
    """Devuelve mapa de acceso eficiente a criterios, encapsulado para integridad."""
    return {k: tuple(c for c in _CRITERIOS_SALUD if c.metric_key == k)
            for k in {c.metric_key for c in _CRITERIOS_SALUD}}

_CRITERIOS_MAP_IMMUTABLE: Final[Dict[str, Tuple[ProblemCriterion, ...]]] = _get_criterios_map()
_KEYS_WITH_CRITERIA: Final[frozenset[str]] = frozenset(_CRITERIOS_MAP_IMMUTABLE.keys())

class AreaExplanation(NamedTuple):
    """Mapeo simple para descripciones pedagógicas de áreas de la aplicación."""
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

# Regex para prevenir inyecciones o recorridos de sistema
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

# Diccionario central de validadores de métricas
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

_VALID_METRIC_KEYS: Final[set[str]] = set(_VALIDATORS.keys())

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Convierte a float asegurando que el resultado sea finito, real y no negativo."""
    try:
        if not _is_safe_numeric_context(val):
            return default
        metric_val: float = float(val)
        return metric_val if metric_val >= 0.0 else default
    except (TypeError, ValueError):
        return default

def _validate_response_length(text: Any) -> str:
    """Verifica la longitud del texto de respuesta y lo trunca si es necesario."""
    if not isinstance(text, str) or not text: return "Respuesta no disponible."
    return text[:_MAX_TEXT_LENGTH]

def _is_safe_payload_structure(val: Any, depth: int = 0) -> bool:
    """
    Valida recursivamente que una estructura JSON (diccionario/lista) sea plana.
    Aplica límites de profundidad y tamaño de colección para mitigar ataques DoS.
    """
    if depth > _MAX_NESTING_DEPTH: return False
    if val is None or isinstance(val, (int, float, bool)): return True
    if isinstance(val, str): return _ensure_safe_text(val)
    if isinstance(val, (Path, type, Callable)): return False
    if isinstance(val, (list, tuple)):
        if len(val) > _MAX_COLLECTION_SIZE: return False
        return all(not isinstance(i, (Path, type, Callable)) and _is_safe_payload_structure(i, depth + 1) for i in val)
    if isinstance(val, dict):
        if len(val) > _MAX_COLLECTION_SIZE: return False
        return all(isinstance(k, str) and _ensure_safe_text(k) and not isinstance(v, (Path, type, Callable)) and _is_safe_payload_structure(v, depth + 1) for k, v in val.items())
    return False

def _is_input_too_deep_or_complex(val: Any, depth: int = 0) -> bool:
    """
    Detecta si una estructura de datos es excesivamente compleja o profunda.
    Retorna True si supera la profundidad permitida o el tamaño máximo de colección.
    """
    if depth > _MAX_NESTING_DEPTH: return True
    if val is None or isinstance(val, (int, float, bool, str)): return False
    if isinstance(val, Path): return True
    if isinstance(val, (list, tuple)):
        if len(val) > _MAX_COLLECTION_SIZE: return True
        return any(isinstance(i, (list, tuple, dict, Path)) or _is_input_too_deep_or_complex(i, depth + 1) for i in val)
    if isinstance(val, dict):
        if len(val) > _MAX_COLLECTION_SIZE: return True
        return any(isinstance(v, (list, tuple, dict, Path)) or _is_input_too_deep_or_complex(v, depth + 1) for v in val.values())
    return True

def _is_metric_within_bounds(val: float, spec: MetricSpec) -> bool:
    """Valida si un valor numérico está dentro de los límites definidos en su especificación."""
    return spec.min_val <= val <= spec.max_val

@lru_cache(maxsize=128)
def _evaluate_active_problems(metrics_items: frozenset[tuple[str, float]]) -> tuple[str, ...]:
    """
    Evalúa el estado de salud basado en métricas utilizando los criterios configurados.
    """
    results = []
    for key, val in metrics_items:
        for criterion in _CRITERIOS_MAP_IMMUTABLE.get(key, ()):
            if (msg := criterion.format_if_triggered(val)) is not None:
                results.append(msg)
    return tuple(results)

@dataclass
class SystemContext:
    """
    Representa el estado consolidado de salud del sistema.
    
    Gestiona la ingestión y validación de métricas recolectadas por otros módulos.
    Utiliza un diseño defensivo: cualquier entrada externa es sanitizada antes 
    de ser almacenada para evitar que rutas o datos maliciosos contaminen el 
    estado del asistente.
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
        """Retorna solo las métricas numéricas que fueron correctamente inicializadas."""
        return {k: float(v) for k in _VALID_METRIC_KEYS if (v := getattr(self, k, -1.0)) != -1.0}

    @cached_property
    def _cached_active_problems(self) -> tuple[str, ...]:
        """Recopila problemas detectados basándose en los criterios de salud."""
        if not self.analyzed: return ()
        snap = self.metrics_snapshot
        # Búsqueda optimizada: solo evaluamos llaves que tienen criterios activos
        relevant = frozenset((k, snap[k]) for k in snap if k in _KEYS_WITH_CRITERIA)
        return _evaluate_active_problems(relevant)

    @property
    def active_problems(self) -> tuple[str, ...]:
        """Devuelve la lista actual de problemas de salud del sistema."""
        return self._cached_active_problems

    @cached_property
    def _hash_tuple(self) -> tuple:
        """Tupla utilizada para el almacenamiento en caché de estado."""
        return tuple(getattr(self, k) for k in sorted(_VALID_METRIC_KEYS)) + (self.grade, self.analyzed)

    def get_metric(self, key: str, default: float) -> float:
        """Obtiene el valor de una métrica específica de forma segura."""
        return self.metrics_snapshot.get(key, default)

    @property
    def is_empty(self) -> bool:
        """Verifica si el contexto contiene datos de análisis válidos."""
        return not self.analyzed or not isinstance(self.score, int) or self.score < 0

    def __hash__(self) -> int:
        return hash(self._hash_tuple)

    @property
    def is_valid_structure(self) -> bool:
        """Verifica que la calificación (grade) no sea una ruta o entrada peligrosa."""
        return _ensure_safe_text(self.grade) if (self.grade and len(self.grade) > 0) else True

    def _is_valid_metric_key(self, key: str) -> bool:
        """Verifica si la clave es una métrica válida del sistema."""
        return key in _VALID_METRIC_KEYS

    def _has_circular_refs(self, source: Any, memo: Optional[Set[int]] = None) -> bool:
        """Verifica recursivamente si la fuente contiene referencias circulares."""
        if memo is None: memo = set()
        if id(source) in memo: return True
        if isinstance(source, (dict, list, tuple)):
            memo.add(id(source))
            if isinstance(source, dict):
                return any(self._has_circular_refs(v, memo) for v in source.values())
            return any(self._has_circular_refs(i, memo) for i in source)
        return False

    def _apply_field(self, source: Any, key: str, spec: MetricSpec) -> Optional[Union[int, float]]:
        """Extrae, valida y convierte un valor específico desde una fuente externa."""
        val = source.get(key) if isinstance(source, dict) else _get_safe_attr(source, key)
        return _is_valid_numeric_update(val, spec)

    def _clean_grade(self, val: Any) -> str:
        """Desinfecta la calificación para asegurar que sea una cadena limpia."""
        if not isinstance(val, (str, int, float)) or isinstance(val, bool): return ""
        try:
            clean: str = str(val).strip()
            # Eliminar caracteres de control y truncar
            clean = _REGEX_CONTROL_CHARS.sub(" ", clean)[:_MAX_GRADE_LEN]
            if not clean or is_protected_path(clean) or not _ensure_safe_text(clean): 
                return ""
            return clean
        except Exception:
            return ""

    def _validate_keys_safe(self, source: Mapping[str, Any]) -> bool:
        """Asegura que ninguna clave de entrada sea una ruta sensible del sistema."""
        return all(isinstance(k, str) and not is_protected_path(k) for k in source.keys())

    def _extract_grade(self, source: Any) -> str:
        """Extrae el campo 'grade' de una fuente de datos."""
        if not isinstance(source, (dict, object)): return ""
        val = source.get("grade") if isinstance(source, dict) else _get_safe_attr(source, "grade")
        return self._clean_grade(val)

    def _validate_and_sanitize_data(self, source: Any) -> Dict[str, Any]:
        """Procesa y sanitiza todas las métricas recibidas de una fuente externa."""
        if isinstance(source, dict) and not self._validate_keys_safe(source):
            return {}
        
        updates: Dict[str, Any] = {}
        for k, spec in _VALIDATORS.items():
            val = self._apply_field(source, k, spec)
            if val is not None:
                # Forzar conversión de tipo estricta antes de la asignación
                try:
                    if spec.type_hint is int:
                        updates[k] = int(val)
                    else:
                        updates[k] = float(val)
                except (ValueError, TypeError):
                    continue
        
        if (cleaned_grade := self._extract_grade(source)):
            updates["grade"] = cleaned_grade
            
        return updates

    def _validate_ingestion_source(self, source: Any) -> bool:
        """Verifica que la fuente de datos sea segura y apta para ingestión."""
        if source is None or callable(source) or isinstance(source, Path): return False
        if self._has_circular_refs(source): return False
        if isinstance(source, dict):
            if not all(isinstance(k, str) and not is_protected_path(k) for k in source.keys()):
                return False
        if isinstance(source, (dict, list, tuple)):
            return not _is_input_too_deep_or_complex(source)
        return True

    def _apply_updates(self, updates: Mapping[str, Any]) -> None:
        """Actualiza el estado interno mediante una transacción atómica y validación de tipos."""
        if not isinstance(updates, dict) or not updates: return
        
        try:
            temp_state = copy.deepcopy(self.__dict__)
            for key, value in updates.items():
                if key in temp_state:
                    current_val = temp_state[key]
                    # Validación estricta: asegurar que no inyectamos tipos incompatibles
                    if isinstance(current_val, (int, float, bool)) and isinstance(value, (int, float)):
                        temp_state[key] = type(current_val)(value)
                    elif isinstance(current_val, str) and isinstance(value, str):
                        temp_state[key] = value
            
            temp_state["analyzed"] = True
            
            # Si pasamos las validaciones, actualizamos el estado real
            self.__dict__.update(temp_state)
            
            # Limpiar cachés de propiedades derivadas al modificar
            for cache_key in ["metrics_snapshot", "_cached_active_problems", "_hash_tuple"]:
                self.__dict__.pop(cache_key, None)
        except (AttributeError, TypeError, KeyError):
            pass

    def ingest(self, source: Any) -> bool:
        """Ingiere y valida datos externos para actualizar el estado de SystemContext."""
        if not self._validate_ingestion_source(source): 
            return False
        try:
            updates = self._validate_and_sanitize_data(source)
            if updates:
                # La lógica de transacción está centralizada en _apply_updates
                self._apply_updates(updates)
                return True
        except (AttributeError, TypeError, Exception):
            pass
        return False

@dataclass
class Answer:
    """Respuesta textual del asistente con metadatos de origen."""
    text: str
    source: str = "local"
    notice: str = ""
    suggestions: List[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        """Indica si la respuesta fue generada por IA externa."""
        return self.source == "gemini"

def _is_safe_path_input(text: str) -> bool:
    """Verifica si el texto de entrada podría ser malinterpretado como una ruta."""
    if not isinstance(text, str): return False
    if _REGEX_STRUCTURE_INJECTION.search(text) or _REGEX_PATH_TRAVERSAL.search(text) or _REGEX_SYSTEM_PATHS.search(text):
        return False
    return not is_protected_path(text)

def _contains_forbidden_patterns(text: str) -> bool:
    """Verifica si el texto contiene patrones que sugieren inyección de comandos."""
    return any(pattern.search(text) for pattern in SECURITY_PATTERNS)

def _has_invalid_meta_characters(text: str) -> bool:
    """Detecta caracteres de control ANSI o secuencias Unicode restringidas."""
    return bool(_REGEX_ANSI_ESCAPE.search(text) or _REGEX_UNICODE_FORMAT.search(text) or any(c in _FORBIDDEN_CHARS for c in text))

def _is_safe_text_length(text: Any) -> bool:
    """Valida la longitud y tipo del texto para evitar desbordamientos."""
    if isinstance(text, Path): return False
    return isinstance(text, str) and 0 < len(text) <= _MAX_TEXT_LENGTH and not is_protected_path(text)

def _ensure_safe_text(text: Any) -> bool:
    """Guardiana principal de seguridad de texto frente a inyecciones."""
    if not _is_safe_text_length(text): return False
    if _has_invalid_meta_characters(text) or _contains_forbidden_patterns(text): return False
    if not _is_safe_path_input(text): return False
    return True

def build_context(metrics: Any = None, health: Any = None, **extra: Any) -> SystemContext:
    """Crea una instancia de SystemContext a partir de múltiples fuentes posibles."""
    ctx: SystemContext = SystemContext()
    for s in (metrics, health, extra):
        if s is not None:
            ctx.ingest(s)
    return ctx

def context_as_text(context: SystemContext) -> str:
    """Serializa las métricas clave de salud a texto plano, garantizando seguridad."""
    if context.is_empty: return ""
    snap: MetricsDict = context.metrics_snapshot

    def gen_lines() -> Iterable[str]:
        for m in _CONTEXT_SCHEMA:
            val = snap.get(m.key, -1.0)
            if val >= 0.0:
                line = f"{m.key}: {val:.{m.precision}f}{m.suffix}"
                if _ensure_safe_text(line):
                    yield line

    return "\n".join(gen_lines())

def explain_area(area: Any) -> str:
    """Retorna una explicación legible para el usuario sobre áreas de la aplicación."""
    if not isinstance(area, str): return "No tengo una explicación para esa área."
    return _validate_response_length(_EXPLANATION_MAP.get(area.strip().lower(), "No tengo una explicación para esa área."))

@lru_cache(maxsize=128)
def _format_problem_message(problems: Tuple[str, ...], score: Union[int, str]) -> str:
    """Genera una frase humanamente legible sobre el estado de salud y puntaje."""
    clean_score: str = str(score)
    if not problems:
        return f"Tu sistema está en buen estado ({clean_score}/100). No hay nada urgente."
    return f"Con un puntaje de {clean_score}/100, por orden de prioridad: {', '.join(problems)}."

@_ensure_query_is_safe
@_ensure_handler_is_safe
def handle_ram(ctx: SystemContext, user_query: str) -> Answer:
    """Respuesta para consultas relacionadas con el uso de memoria RAM."""
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

@_ensure_query_is_safe
@_ensure_handler_is_safe
def handle_disk(ctx: SystemContext, user_query: str) -> Answer:
    """Respuesta para consultas de espacio en disco."""
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

@_ensure_query_is_safe
@_ensure_handler_is_safe
def handle_security(ctx: SystemContext, user_query: str) -> Answer:
    """Respuesta sobre archivos marcados como sospechosos."""
    snap: MetricsDict = ctx.metrics_snapshot
    count: int = int(snap.get("suspicious_count", 0.0))
    warn: int = int(snap.get("suspicious_warnings", 0.0))
    if count == 0:
        texto: str = "No hay archivos sospechosos. La app nunca borra sola, todo va a revisión."
    else:
        texto = f"Hay {count} archivo(s) marcados, {warn} con advertencia. Son señales, no una condena: si no reconocés alguno, usá 'Aislar hallazgos'."
    return Answer(texto, notice=OFFLINE_NOTICE)

@_ensure_query_is_safe
@_ensure_handler_is_safe
def handle_score(ctx: SystemContext, user_query: str) -> Answer:
    """Respuesta global basada en el puntaje de salud."""
    resumen: str = _format_problem_message(ctx.active_problems, ctx.score if ctx.score is not None else "N/A")
    grade_str: str = f" (nota {ctx.grade})" if (ctx.grade and len(ctx.grade) > 0) else ""
    explicacion: str = " El puntaje combina basura, seguridad, memoria, disco, duplicados y programas de inicio."
    return Answer(f"{resumen}{grade_str}{explicacion}", notice=OFFLINE_NOTICE)

@_ensure_query_is_safe
@_ensure_handler_is_safe
def handle_startup(ctx: SystemContext, user_query: str) -> Answer:
    """Respuesta pedagógica sobre el impacto de los programas de inicio."""
    count: int = int(ctx.metrics_snapshot.get("startup_count", 0.0))
    estado: str = f"Tenés {count} programas que arrancan con Windows."
    valoracion: str = "Son bastantes, y cada uno suma tiempo de encendido." if count > 15 else ("Es normal." if count > 8 else "Está bien.")
    cierre: str = " La app los lista, pero desactivalos desde el Administrador de tareas de Windows."
    return Answer(f"{estado} {valoracion}{cierre}", notice=OFFLINE_NOTICE)

# Estructura optimizada para búsqueda de tokens
_TOKENS_MAP: Final[Dict[str, Callable[[SystemContext, str], Answer]]] = {
    "ram": handle_ram, "memoria": handle_ram, "lenta": handle_ram, "lento": handle_ram, "acelerar": handle_ram,
    "espacio": handle_disk, "disco": handle_disk, "lleno": handle_disk, "recuperar": handle_disk, "liberar": handle_disk,
    "seguro": handle_security, "virus": handle_security, "sospechos": handle_security, "borrar": handle_security, "peligro": handle_security,
    "puntaje": handle_score, "salud": handle_score, "nota": handle_score, "score": handle_score,
    "inicio": handle_startup, "arranque": handle_startup, "arranca": handle_startup, "encender": handle_startup
}

def _sanitize_query(question: str) -> str:
    """Normaliza y limpia la consulta del usuario, eliminando caracteres de control."""
    if not isinstance(question, str): return ""
    clean: str = " ".join(_REGEX_CONTROL_CHARS.sub(' ', question).split())[:100]
    if is_protected_path(clean): return ""
    return clean if _ensure_safe_text(clean) else ""

def local_answer(question: str, context: SystemContext) -> Answer:
    """Resuelve consultas usando la lógica heurística local sin conexiones externas."""
    if context.is_empty:
        return Answer("Todavía no corriste ningún análisis. Andá a la pestaña Salud y apretá 'Analizar el sistema': es de solo lectura.", notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_SHORT)
    q_sanitized: str = _sanitize_query(question)
    if not q_sanitized: return Answer("Entrada no válida.")
    
    # Verificación de integridad del contexto antes de procesar
    if not isinstance(context, SystemContext):
        return Answer("Contexto corrupto.")
    
    # Búsqueda optimizada por tokens
    for token in _TOKEN_REGEX.findall(q_sanitized.lower()):
        if (handler := _TOKENS_MAP.get(token)):
            return handler(context, question)
            
    return Answer(_format_problem_message(context.active_problems, context.score if context.score is not None else "N/A"), notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_SHORT)

def available(base: Optional[Union[str, Path]] = None) -> bool:
    """Verifica si el asistente remoto (IA) está habilitado en las configuraciones."""
    try: return bool(settings.assistant_enabled(base))
    except Exception: return False

def _parse_config(raw_cfg: Any) -> AssistantConfig:
    """Parsea la configuración de usuario con valores por defecto seguros."""
    default = AssistantConfig(api_key="", model="gemini-3.1-flash-lite", allow_metrics=True)
    if not isinstance(raw_cfg, dict): 
        return default
    
    try:
        api_key = os.environ.get("LTO_API_KEY", str(raw_cfg.get("asistente_api_key", "")))
        model_raw = raw_cfg.get("asistente_modelo")
        allow_raw = raw_cfg.get("asistente_enviar_metricas")
        
        return AssistantConfig(
            api_key=str(api_key) if isinstance(api_key, str) else "",
            model=str(model_raw) if isinstance(model_raw, str) and _MODEL_NAME_REGEX.match(model_raw) else "gemini-3.1-flash-lite",
            allow_metrics=bool(allow_raw) if isinstance(allow_raw, bool) else True
        )
    except (TypeError, ValueError, AttributeError):
        return default

def _build_payload(question: str, context_text: str) -> Optional[bytes]:
    """Prepara el cuerpo de la petición (JSON) para la API de Gemini."""
    if not context_text or not _ensure_safe_text(context_text): return None
    q = _sanitize_query(question)
    if not q or not _ensure_safe_text(q): return None
    
    # Construcción de estructura segura
    content_payload = {
        "contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\nMétricas:\n{context_text}\n\nPregunta: {q}"}]}]
    }
    
    if not _is_safe_payload_structure(content_payload): return None
    
    try:
        encoded = json.dumps(content_payload).encode("utf-8")
        return encoded if len(encoded) <= _MAX_RESPONSE_BYTES else None
    except (TypeError, ValueError): return None

def _extract_text_from_gemini_json(data: Any) -> Optional[str]:
    """Extrae de manera segura el texto de la respuesta JSON recibida de la API."""
    if not isinstance(data, dict): return None
    
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict): 
        return None
        
    content = candidates[0].get("content")
    if not isinstance(content, dict) or candidates[0].get("finishReason") != "STOP":
        return None
    
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        return None
    
    text_val = parts[0].get("text")
    if not isinstance(text_val, str): 
        return None
    
    if _contains_forbidden_patterns(text_val): 
        return None
    
    sanitized = _validate_response_length(text_val)
    return sanitized if _ensure_safe_text(sanitized) else None

def _call_gemini(question: str, context_text: str, api_key: str, model: str) -> Optional[str]:
    """Realiza la petición POST de forma bloqueante a la API de Google."""
    if not (_API_KEY_REGEX.match(api_key) and _MODEL_NAME_REGEX.match(model) and context_text and _ensure_safe_text(context_text)):
        return None
    
    payload = _build_payload(question, context_text)
    if not payload: return None
    
    target_url = _ENDPOINT_BASE.format(model=model)
    # Validar que el dominio es el correcto y que no hay inyección en la URL
    if not target_url.startswith(_API_HOST_ROOT) or any(c in target_url for c in _FORBIDDEN_CHARS): return None
    
    try:
        # Sanitizar entrada de query en URL
        parsed_url = f"{target_url}?key={urllib.parse.quote(api_key)}"
        req = urllib.request.Request(
            parsed_url, 
            data=payload, 
            headers={"Content-Type": "application/json", "Accept": "application/json"}, 
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as res:
            if res.status != 200: return None
            raw_res = res.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw_res) > _MAX_RESPONSE_BYTES: return None
            return _extract_text_from_gemini_json(json.loads(raw_res.decode("utf-8")))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError, ValueError): return None

def ask(question: str, context: Optional[SystemContext] = None, base: Optional[Union[str, Path]] = None) -> Answer:
    """Orquesta el flujo de consultas entre el motor local y el remoto."""
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
