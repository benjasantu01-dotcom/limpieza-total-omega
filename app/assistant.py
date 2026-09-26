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

def _safe_handler_wrapper(func: Callable[[SystemContext, str], Answer]) -> Callable[[SystemContext, str], Answer]:
    """
    Decorador que estandariza el manejo de errores en las consultas de usuario.
    
    Asegura que cualquier `SystemContext` sea válido antes de procesar y que 
    la ejecución no interrumpa la interfaz en caso de excepciones inesperadas,
    garantizando siempre un objeto `Answer` como retorno.
    """
    @wraps(func)
    def wrapper(ctx: SystemContext, q: str) -> Answer:
        if not isinstance(ctx, SystemContext) or ctx.is_empty: 
            return Answer("Primero analizá el sistema.")
        try:
            result = func(ctx, q)
            if isinstance(result, Answer):
                return result
            logging.error(f"Handler {func.__name__} devolvió un tipo no compatible: {type(result)}")
        except Exception as e:
            logging.error(f"Falla inesperada en {func.__name__}: {str(e)[:50]}")
        return Answer("Error al procesar la respuesta.")
    return wrapper

class AssistantConfig(NamedTuple):
    """
    Configuración estructurada para el cliente de IA (Gemini).
    
    Attributes:
        api_key: Credencial para la API de Google, preferiblemente desde env.
        model: Identificador del modelo (ej. gemini-3.1-flash-lite).
        allow_metrics: Booleano que autoriza la exportación de métricas anonimizadas.
    """
    api_key: str
    model: str
    allow_metrics: bool

@dataclass(frozen=True)
class MetricSpec:
    """
    Define un contrato para métricas numéricas.
    
    Validaciones:
        - Tipo (int/float, no booleano).
        - Rango lógico definido por min_val/max_val.
    """
    cast_func: Callable[[Any], Any]
    min_val: float
    max_val: float

    def is_valid_type(self, val: Any) -> bool:
        """Valida que el valor sea un número real; excluye explícitamente booleanos."""
        return isinstance(val, (int, float)) and not isinstance(val, bool)

class ProblemCriterion(NamedTuple):
    """
    Regla de negocio para determinar si una métrica representa un problema.
    
    Ejemplo: {disk_free_percent, 10.0, "<", "X% de disco libre"}
    Si el valor es < 10, dispara la alerta.
    """
    metric_key: str
    threshold: float
    operator: str # "<" (menor que), ">" (mayor que)
    message_format: str # Template para string format (debe aceptar 1 argumento)

    def _evaluate_metric(self, val: float) -> bool:
        """Aplica lógica booleana sobre el valor comparándolo con el umbral."""
        ops = {"<": operator.lt, ">": operator.gt}
        op_func = ops.get(self.operator)
        return op_func(val, self.threshold) if op_func and math.isfinite(val) else False

    def is_triggered_by(self, ctx: SystemContext) -> bool:
        """Verifica si la condición de riesgo se cumple para el contexto actual."""
        val = ctx.get_metric(self.metric_key, DEFAULT_METRIC_VAL)
        return val >= 0 and self._evaluate_metric(val)

    def format_if_triggered(self, ctx: SystemContext) -> Optional[str]:
        """
        Formatea el mensaje de advertencia. Retorna None si el valor es seguro.
        Realiza saneamiento del string resultante antes de retornar.
        """
        val: float = ctx.get_metric(self.metric_key, DEFAULT_METRIC_VAL)
        
        if val < 0 or not math.isfinite(val) or not self._evaluate_metric(val):
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
_ENDPOINT_BASE: Final[str] = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TIMEOUT_SECONDS: Final[int] = 30

_REGEX_INYECCION: Final[re.Pattern] = re.compile(r"([a-zA-Z]:[\\/]|/|\\|\.\.|\0|[\u202e\u202d\u200e\u200f])")
_REGEX_CONTROL: Final[re.Pattern] = re.compile(r"[\x00-\x1f\x7f\u0080-\u009f\u202b-\u202f\u200b-\u200d\uFEFF]")
_REGEX_PATH_TRAVERSAL: Final[re.Pattern] = re.compile(r"(\.\.[\\/])|([\\/]\.\.)", re.IGNORECASE)

SECURITY_PATTERNS: Final[list[re.Pattern]] = [
    _REGEX_INYECCION,
    _REGEX_CONTROL,
    _REGEX_PATH_TRAVERSAL,
    re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"),                       # ANSI
    re.compile(r"(Get-|Remove-|Set-|Stop-|Start-)[a-zA-Z]+", re.IGNORECASE),   # Comandos PS
    re.compile(r"(exec|eval|subprocess|system\s*\(|rm\s+|del\s+|cmd\.exe|powershell|reg\.exe)", re.IGNORECASE), # Contenido peligroso
    re.compile(r"(\\\\|[a-z]:\\|/etc/|\\\\UNC|C:\\Windows|System32|/proc/|/dev/)", re.IGNORECASE) # Estructuras sistema
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
    """Recursivamente detecta estructuras de datos excesivamente anidadas para evitar ataques DoS."""
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
    Agregador centralizado del estado del sistema. 
    Contiene métricas normalizadas, valida la integridad de los datos entrantes y
    expone métodos de consulta para la lógica de diagnóstico, manteniendo el 
    contrato de anonimidad sobre rutas y archivos.
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
        """Recupera el valor de una métrica, aplicando validación de finitud."""
        val = getattr(self, key, None)
        if not isinstance(val, (int, float)) or not math.isfinite(val):
            return default
        return float(val)

    @cached_property
    def active_problems(self) -> tuple[str, ...]:
        """Retorna tuple de problemas detectados tras evaluar los criterios de salud."""
        if not self.analyzed: return ()
        return tuple(msg for crit in _CRITERIOS_SALUD if (msg := crit.format_if_triggered(self)) is not None)

    @property
    def is_empty(self) -> bool:
        """Verifica si el contexto contiene datos coherentes tras el análisis."""
        return not self.analyzed or self.score is None or not isinstance(self.score, int) or self.score < 0

    def __hash__(self) -> int:
        return hash((self.score, self.grade, self.junk_mb, self.suspicious_count, 
                     self.memory_available_percent, self.disk_free_percent,
                     self.duplicate_mb, self.startup_count))

    @property
    def is_valid_structure(self) -> bool:
        """Valida que la información de grado no contenga inyecciones de comandos."""
        return _ensure_safe_text(self.grade) if self.grade else True

    def _apply_field(self, source: Any, key: str, spec: MetricSpec) -> bool:
        """Valida y asigna un valor a una métrica, aplicando casting seguro y registro."""
        val = _get_source_value(source, key)
        if val is None or not spec.is_valid_type(val): return False
        
        try:
            float_val = float(val)
            if not _is_metric_within_bounds(float_val, spec): return False
            converted = spec.cast_func(float_val)
            object.__setattr__(self, key, converted)
            return True
        except (TypeError, ValueError):
            return False

    def _clean_grade(self, val: Any) -> str:
        """Limpia el string de calificación eliminando caracteres de control."""
        if not isinstance(val, str): return ""
        clean = _REGEX_CONTROL.sub(" ", val)[:10].strip()
        return clean if _ensure_safe_text(clean) else ""

    def ingest(self, source: Any) -> bool:
        """
        Ingesta datos de una fuente externa y los normaliza en el contexto.
        
        Recorre las claves definidas en `_VALIDATORS`, validando rangos y tipos
        de cada métrica. Solo marca como `analyzed` si la estructura resultante
        pasa los chequeos de integridad física.
        """
        if not (isinstance(source, dict) or hasattr(source, "__dict__")):
            return False
        if _is_input_too_deep_or_complex(source):
            return False
            
        found_data = False
        for key, spec in _VALIDATORS.items():
            if self._apply_field(source, key, spec):
                found_data = True
        
        try:
            grade_val = _get_source_value(source, "grade")
            if isinstance(grade_val, str):
                clean_grade = self._clean_grade(grade_val)
                if clean_grade:
                    object.__setattr__(self, 'grade', clean_grade)
                    found_data = True
        except Exception:
            pass
        
        if found_data and _validate_context_integrity(self):
            object.__setattr__(self, 'analyzed', True)
            return True
        return False

@dataclass
class Answer:
    """Encapsula la respuesta del asistente, fuente y sugerencias de seguimiento."""
    text: str
    source: str = "local"
    notice: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        return self.source == "gemini"

def _validate_context_integrity(ctx: SystemContext) -> bool:
    """Verifica límites físicos de las métricas para evitar datos corruptos."""
    return (
        math.isfinite(ctx.junk_mb) and ctx.junk_mb >= 0 and
        math.isfinite(ctx.duplicate_mb) and ctx.duplicate_mb >= 0 and
        0 <= ctx.get_metric("disk_free_percent", 0.0) <= 100 and
        0 <= ctx.get_metric("memory_available_percent", 0.0) <= 100
    )

def _is_safe_text_structure(text: str) -> bool:
    """
    Realiza saneamiento profundo contra inyecciones y patrones de ruta.
    Bloquea explícitamente caracteres de control, rutas UNC y comandos peligrosos.
    """
    if not text: return True
    if any(ord(c) < 32 and c not in '\n\r\t' for c in text): return False
    
    if is_protected_path(text): return False
    if text.startswith(("\\\\", "//", "UNC")): return False
    if any(c in text for c in "<>|&^"): return False
    
    try:
        if any(token in text.lower() for token in ["c:\\", "d:\\", "system32", "/etc/"]): return False
        p = Path(text)
        if p.is_absolute() or text.startswith(("./", "../", "..\\")):
            return False
    except (ValueError, TypeError, OSError):
        pass
    
    return not any(pattern.search(text) for pattern in SECURITY_PATTERNS)

def _ensure_safe_text(text: Any) -> bool:
    """Valida que un objeto sea un string seguro, no vacío y bajo el límite de caracteres."""
    if not isinstance(text, str) or not text or len(text) > _MAX_TEXT_LENGTH:
        return False
    if _REGEX_CONTROL.search(text):
        return False
    return _is_safe_text_structure(text)

def _get_source_value(source: Any, key: str) -> Any:
    """Acceso controlado a atributos para evitar la ejecución de métodos o acceso privado."""
    if not isinstance(key, str) or key.startswith("_"): return None
    try:
        if isinstance(source, dict):
            val = source.get(key)
        else:
            val = getattr(source, key, None)
        
        if callable(val) or (isinstance(key, str) and (key.startswith("__") or key.startswith("_"))):
            return None
        return val
    except Exception:
        return None

def build_context(metrics: Any = None, health: Any = None, **extra: Any) -> SystemContext:
    """Factory: construye y consolida un SystemContext desde múltiples fuentes."""
    ctx = SystemContext()
    for s in (metrics, health, extra):
        if s is not None:
            ctx.ingest(s)
    return ctx

def _fmt_metric_sanitized(val: Any, unit: str = "", decimal: int = 0) -> str:
    """Formatea métricas eliminando caracteres prohibidos para visualización en UI."""
    if not isinstance(val, (int, float, str)): return "N/A"
    raw = _fmt_metric(val, unit, decimal)
    return _REGEX_INYECCION.sub(" ", _REGEX_CONTROL.sub(" ", raw))[:32]

@lru_cache(maxsize=16)
def _generate_context_cached(ctx: SystemContext) -> str:
    """Genera bloque de resumen del sistema optimizado para prompts."""
    return (
        f"Puntaje de salud: {ctx.score if ctx.score is not None else 'N/A'}"
        f"{f' nota {ctx.grade[:5]}' if ctx.grade else ''}\n"
        f"Basura: {_fmt_metric(ctx.junk_mb, ' MB', 0)}\n"
        f"Sospechosos: {int(ctx.suspicious_count)}\n"
        f"RAM disponible: {_fmt_metric(ctx.memory_available_percent, '%', 0)}\n"
        f"Disco libre: {_fmt_metric(ctx.disk_free_percent, '%', 0)}\n"
        f"Duplicados: {_fmt_metric(ctx.duplicate_mb, ' MB', 0)}\n"
        f"Inicio: {int(ctx.startup_count)} items"
    )

def context_as_text(context: SystemContext) -> str:
    """Convierte el contexto en un string serializado listo para ser embebido en prompts."""
    return _generate_context_cached(context) if not context.is_empty else ""

def _fmt_metric(val: Any, unit: str = "", decimal: int = 0) -> str:
    """Utilidad de formateo para convertir métricas numéricas a texto legible."""
    f = _safe_float(val, -1.0)
    if f < 0: return "N/A"
    return f"{f:.{decimal}f}{unit}"

def explain_area(area: Any) -> str:
    """Recupera la descripción amigable de un área del sistema."""
    if not isinstance(area, str):
        return "No tengo una explicación para esa área."
    return _validate_response_length(_EXPLANATION_MAP.get(area.strip().lower(), "No tengo una explicación para esa área."))

@lru_cache(maxsize=32)
def _format_problem_message(problems: tuple[str, ...], score: Union[int, str]) -> str:
    """Crea una oración descriptiva basada en los problemas activos."""
    clean_score = str(score)
    if not problems:
        return f"Tu sistema está en buen estado ({clean_score}/100). No hay nada urgente."
    return f"Con un puntaje de {clean_score}/100, por orden de prioridad: {', '.join(problems)}."

@_safe_handler_wrapper
def handle_ram(ctx: SystemContext, user_query: str) -> Answer:
    """Procesa consultas sobre el estado de la memoria RAM."""
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
    """Procesa consultas sobre el espacio y la limpieza de disco."""
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
    """Procesa consultas sobre riesgos de seguridad y archivos sospechosos."""
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
    """Procesa consultas sobre el desglose del puntaje de salud."""
    score_val = ctx.score if ctx.score is not None else "N/A"
    grade_str = ctx.grade if ctx.grade else ""
    score_display = f"Tu puntaje es {score_val}/100{f' (nota {grade_str})' if grade_str else ''}."
    
    problemas = ctx.active_problems
    resumen = ("Lo que más te está restando: " + ", ".join(problemas[:3]) + ".") if problemas else "No hay nada urgente."
    explicacion = " El puntaje combina basura, seguridad, memoria, disco, duplicados y programas de inicio."
    return Answer(_validate_response_length(f"{score_display} {resumen}{explicacion}"), notice=OFFLINE_NOTICE)

@_safe_handler_wrapper
def handle_startup(ctx: SystemContext, user_query: str) -> Answer:
    """Procesa consultas sobre programas que inician con el sistema."""
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
    """Limpia y trunca la consulta del usuario para evitar abusos."""
    if not isinstance(question, str): return ""
    clean = _REGEX_CONTROL.sub(' ', question).strip()[:100]
    return clean if _ensure_safe_text(clean) else ""

def local_answer(question: str, context: SystemContext) -> Answer:
    """Motor de inferencia local: resuelve consultas basándose en reglas heurísticas."""
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
    
    tokens = _TOKEN_REGEX.findall(q_sanitized.lower())
    for token in tokens:
        if handler := _TOKENS_MAP.get(token):
            return handler(context, question)
            
    cuerpo = _format_problem_message(context.active_problems, context.score or "N/A")
    return Answer(_validate_response_length(cuerpo), notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_SHORT)

def available(base: Union[str, Path, None] = None) -> bool:
    """Determina si la consulta remota a IA está habilitada en la configuración."""
    try:
        return settings.assistant_enabled(base)
    except (TypeError, ValueError, AttributeError, OSError):
        return False

def _parse_config(raw_cfg: Any) -> AssistantConfig:
    """
    Parsea de forma defensiva el archivo de configuración externa hacia un 
    objeto AssistantConfig.
    """
    default = AssistantConfig("", "gemini-3.1-flash-lite", True)
    if not isinstance(raw_cfg, dict):
        return default
    
    try:
        api_key = raw_cfg.get("asistente_api_key")
        model = raw_cfg.get("asistente_modelo")
        metrics_val = raw_cfg.get("asistente_enviar_metricas")
        
        return AssistantConfig(
            api_key=str(api_key) if isinstance(api_key, str) else "",
            model=str(model) if isinstance(model, str) else "gemini-3.1-flash-lite",
            allow_metrics=bool(metrics_val) if isinstance(metrics_val, bool) else True
        )
    except Exception:
        return default

def _build_payload(question: str, context_text: str) -> Optional[bytes]:
    """Serializa la pregunta y contexto en el formato JSON esperado por Gemini."""
    if not context_text or not _ensure_safe_text(context_text): return None
    q = _sanitize_query(question)
    if not q or not _ensure_safe_text(q): return None
    
    full_prompt = f"{SYSTEM_PROMPT}\n\nMétricas:\n{context_text}\n\nPregunta: {q}"
    if len(full_prompt) > _MAX_PROMPT_LIMIT or not _ensure_safe_text(full_prompt): 
        return None
        
    try:
        payload_data = {"contents": [{"parts": [{"text": full_prompt}]}]}
        payload = json.dumps(payload_data).encode("utf-8")
        if not isinstance(payload, bytes) or len(payload) > (_MAX_RESPONSE_BYTES // 2):
            return None
        return payload
    except (TypeError, ValueError, AttributeError):
        return None

def _extract_text_from_gemini_json(data: Any) -> Optional[str]:
    """Travesía segura por la jerarquía de respuesta JSON de Google."""
    if not isinstance(data, dict): return None
    try:
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates: return None
        candidate = candidates[0]
        if not isinstance(candidate, dict): return None
        content = candidate.get("content")
        if not isinstance(content, dict): return None
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts: return None
        part = parts[0]
        if not isinstance(part, dict): return None
        text_val = part.get("text")
        return str(text_val) if isinstance(text_val, str) else None
    except (AttributeError, TypeError, IndexError, KeyError): 
        return None

def _call_gemini(question: str, context_text: str, api_key: str, model: str) -> Optional[str]:
    """Realiza una petición POST segura a la API de Google."""
    if not isinstance(api_key, str) or not _API_KEY_REGEX.match(api_key): return None
    if not isinstance(model, str) or not _MODEL_NAME_REGEX.match(model): return None
    if not context_text: return None
    
    payload = _build_payload(question, context_text)
    if not payload: return None
    
    try:
        url = _ENDPOINT_BASE.format(model=model) + f"?key={api_key}"
        if not url.startswith("https://generativelanguage.googleapis.com/"): return None
        
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as res:
            if res.status != 200: return None
            raw_res = res.read(_MAX_RESPONSE_BYTES + 1)
            if not isinstance(raw_res, bytes) or len(raw_res) > _MAX_RESPONSE_BYTES: return None
            
            try:
                data = json.loads(raw_res.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
                
            raw_text = _extract_text_from_gemini_json(data)
            if isinstance(raw_text, str) and _ensure_safe_text(raw_text):
                return _validate_response_length(raw_text.strip())
            return None
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, KeyError):
        return None

def ask(question: str, context: Optional[SystemContext] = None,
        base: Union[str, Path, None] = None) -> Answer:
    """Punto de acceso unificado para el usuario: orquesta motores locales y remotos."""
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
