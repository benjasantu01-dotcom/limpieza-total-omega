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

EL ASISTENTE NO EJECUTA NADA
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
from itertools import islice
from functools import lru_cache
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

class AssistantConfig(NamedTuple):
    """
    Configuración persistida del asistente cargada desde settings.
    
    Attributes:
        api_key: String de la clave API de Google Gemini (validada via Regex).
        model: Identificador del modelo (ej. 'gemini-3.1-flash-lite').
        allow_metrics: Bandera booleana para autorizar el envío de datos agregados al endpoint.
    """
    api_key: str
    model: str
    allow_metrics: bool

@dataclass(frozen=True)
class MetricSpec:
    """
    Define el contrato de validación para métricas numéricas entrantes.
    
    Attributes:
        cast_func: Función para convertir el valor (ej. float o int).
        min_val: Límite inferior físico aceptable.
        max_val: Límite superior físico aceptable.
    """
    cast_func: Callable[[Any], Any]
    min_val: float
    max_val: float

    def is_valid_type(self, val: Any) -> bool:
        """Verifica que el valor sea un número real (no booleano ni contenedor)."""
        return isinstance(val, (int, float)) and not isinstance(val, bool)

class ProblemCriterion(NamedTuple):
    """
    Regla heurística que define cuándo una métrica se considera un 'problema'.
    
    Attributes:
        metric_key: Nombre del atributo en SystemContext a evaluar.
        threshold: Valor límite para comparar.
        operator: Comparador lógico ('<' o '>').
        message_format: Template de string para el mensaje al usuario.
    """
    metric_key: str
    threshold: float
    operator: str
    message_format: str

    def _evaluate_metric(self, val: float) -> bool:
        """Compara el valor de la métrica contra el umbral según el operador definido."""
        if self.operator == "<": return val < self.threshold
        if self.operator == ">": return val > self.threshold
        return False

    def is_triggered_by(self, ctx: SystemContext) -> bool:
        """Determina si la métrica actual en el contexto viola este criterio."""
        val = ctx.get_metric(self.metric_key, -1.0)
        return val >= 0 and self._evaluate_metric(val)

    def format_if_triggered(self, ctx: SystemContext) -> str | None:
        """
        Retorna una cadena descriptiva si el criterio se cumple, de lo contrario None.
        Aplica límites de longitud de seguridad para prevenir desbordamientos.
        """
        try:
            if not self.is_triggered_by(ctx):
                return None
            
            f_val = ctx.get_metric(self.metric_key, -1.0)
            if f_val < 0: return None
            
            msg: str = self.message_format.format(f_val)[:_MAX_MSG_CHUNK]
            return msg if _is_safe_text_structure(msg) else None
        except (ValueError, TypeError, AttributeError, KeyError):
            return None

class AreaExplanation(NamedTuple):
    """Mapeo para descripciones pedagógicas de cada área de la aplicación."""
    key: str
    description: str

# Tipos para validación de datos
MetricSource: TypeAlias = dict[str, Any] | object
ScoreSource: TypeAlias = dict[str, Any] | object

# Constantes de configuración de seguridad y límites
_MAX_TEXT_LENGTH: Final[int] = 1000
_MAX_RESPONSE_BYTES: Final[int] = 32768
_MAX_MSG_CHUNK: Final[int] = 200 
_MAX_PROMPT_LIMIT: Final[int] = 4000 

SENSITIVE_KEYS_NEVER_SENT: Final[tuple[str, ...]] = (
    "rutas de archivos",
    "nombres de archivos",
    "contenido de archivos",
    "nombres de procesos",
    "nombre de usuario",
    "nombre del equipo",
    "números de serie",
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

_ENDPOINT: Final[str] = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TIMEOUT_SECONDS: Final[int] = 30
_PATH_INJECTION_REGEX: Final[re.Pattern] = re.compile(r"([a-zA-Z]:[\\/]|/|\\|\.\.|\0|[\u202e\u202d])")
_CONTROL_CHARS_REGEX: Final[re.Pattern] = re.compile(r"[\x00-\x1f\x7f\u0080-\u009f\u202b-\u202f]")
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
    """Conversión segura a float que maneja tipos inesperados, NaN e infinitos."""
    try:
        if val is None or isinstance(val, bool) or not isinstance(val, (int, float, str)):
            return default
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default

def _validate_response_length(text: str) -> str:
    """Trunca el texto para cumplir con el límite máximo de caracteres del motor."""
    return str(text)[:_MAX_TEXT_LENGTH]

@dataclass
class SystemContext:
    """
    Contenedor de estado del sistema. Mantiene únicamente métricas agregadas.
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
        """Acceso seguro a métricas numéricas del sistema con fallback."""
        val = getattr(self, key, default)
        return _safe_float(val, default)

    def __hash__(self) -> int:
        return hash((self.score, self.junk_mb, self.suspicious_count, self.startup_count, self.memory_available_percent, self.disk_free_percent))

    @property
    def is_valid_structure(self) -> bool:
        return _ensure_safe_text(self.grade) if self.grade else True

    def ingest(self, source: Any) -> bool:
        """Intenta extraer y validar métricas desde una fuente externa."""
        if not isinstance(source, (dict, object)) or isinstance(source, (list, tuple, str, int, float, bool)):
            return False
            
        found_data = False
        for key, spec in _VALIDATORS.items():
            if _validate_and_assign(self, source, key, spec):
                found_data = True
        
        grade_val = _get_source_value(source, "grade")
        if isinstance(grade_val, str):
            clean_grade = _CONTROL_CHARS_REGEX.sub(" ", grade_val)[:10].strip()
            if _is_safe_text_structure(clean_grade):
                self.grade = clean_grade
        return found_data

@dataclass
class Answer:
    text: str
    source: str = "local"
    notice: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        return self.source == "gemini"

def _is_safe_text_structure(text: str) -> bool:
    if not text: return True
    if _PATH_INJECTION_REGEX.search(text) or is_protected_path(text):
        return False
    return True

def _ensure_safe_text(text: Any) -> bool:
    if not isinstance(text, str) or not text:
        return False
    if len(text) > _MAX_TEXT_LENGTH:
        return False
    if _CONTROL_CHARS_REGEX.search(text):
        return False
    return _is_safe_text_structure(text)

def _get_source_value(source: Any, key: str) -> Any:
    """Extrae valores de forma robusta ante estructuras de datos no estándar."""
    try:
        if isinstance(source, dict):
            return source.get(key)
        return getattr(source, key, None)
    except (AttributeError, TypeError, KeyError):
        return None

def _validate_and_assign(ctx: SystemContext, source: Any, key: str, spec: MetricSpec) -> bool:
    """Valida y asigna una métrica específica de forma segura."""
    if not isinstance(spec, MetricSpec):
        return False
    try:
        val = _get_source_value(source, key)
        if val is None or not spec.is_valid_type(val):
            return False
        
        clean_val = _safe_float(val, -1.0)
        if clean_val < spec.min_val or clean_val > spec.max_val or math.isnan(clean_val) or math.isinf(clean_val):
            return False
        
        setattr(ctx, key, spec.cast_func(clean_val))
        return True
    except (ValueError, TypeError, AttributeError):
        return False

def build_context(metrics: MetricSource = None, health: ScoreSource = None, **extra: Any) -> SystemContext:
    ctx = SystemContext()
    sources = [s for s in [metrics, health, extra] 
               if s is not None and isinstance(s, (dict, object)) 
               and not isinstance(s, (list, tuple, str, int, float, bool))]
    
    for src in sources:
        if ctx.ingest(src):
            ctx.analyzed = True
    return ctx

def _fmt_metric_sanitized(val: Any, unit: str = "", decimal: int = 0) -> str:
    raw = _fmt_metric(val, unit, decimal)
    return _PATH_INJECTION_REGEX.sub(" ", _CONTROL_CHARS_REGEX.sub(" ", raw))

@lru_cache(maxsize=16)
def _generate_context_lines_cached(score: Optional[int], grade: str, junk: float, susp: int, ram: float, disk: float, dup: float, start: int) -> str:
    lines = [
        f"Puntaje de salud: {_fmt_metric_sanitized(score) if score is not None else 'N/A'}{f' nota {str(grade)[:5]}' if grade else ''}",
        f"Basura: {_fmt_metric_sanitized(junk, ' MB')}",
        f"Sospechosos: {_fmt_metric_sanitized(susp)}",
        f"RAM disponible: {_fmt_metric_sanitized(ram, ' percent')}",
        f"Disco libre: {_fmt_metric_sanitized(disk, ' percent')}",
        f"Duplicados: {_fmt_metric_sanitized(dup, ' MB')}",
        f"Inicio: {_fmt_metric_sanitized(start)} items"
    ]
    return "\n".join(lines)

def context_as_text(context: SystemContext) -> str:
    if not isinstance(context, SystemContext) or not context.analyzed or not context.is_valid_structure:
        return "No hay métricas disponibles todavía."
    try:
        texto_unificado = _generate_context_lines_cached(
            context.score, context.grade, context.junk_mb, context.suspicious_count,
            context.memory_available_percent, context.disk_free_percent, context.duplicate_mb, context.startup_count
        )
        if not _ensure_safe_text(texto_unificado):
            return "Error: el contexto generado no cumple los estándares de seguridad."
        return texto_unificado
    except Exception:
        return "Error crítico al procesar métricas de seguridad."

def _fmt_metric(val: Any, unit: str = "", decimal: int = 0) -> str:
    f = _safe_float(val, -1.0)
    if f < 0:
        return "N/A"
    try:
        return f"{f:.{decimal}f}{unit}"
    except (ValueError, OverflowError):
        return "N/A"

def explain_area(area: Any) -> str:
    if not isinstance(area, str):
        return "No tengo una explicación para esa área."
    return _validate_response_length(_EXPLANATION_MAP.get(area.strip().lower(), "No tengo una explicación para esa área."))

@lru_cache(maxsize=8)
def _get_active_problems(ctx: SystemContext) -> list[str]:
    return [msg for crit in _CRITERIOS_SALUD if (msg := crit.format_if_triggered(ctx))]

def _format_problem_message(problems: list[str], score: int | str) -> str:
    if not problems:
        return f"Tu sistema está en buen estado ({score}/100). No hay nada urgente."
    return f"Con un puntaje de {score}/100, por orden de prioridad: {', '.join(problems)}."

def _identify_active_problems(ctx: SystemContext) -> list[str]:
    """Obtiene los problemas activos usando caché para evitar re-cálculos costosos."""
    return _get_active_problems(ctx) if ctx.analyzed else []

def handle_ram(ctx: SystemContext, user_query: str) -> Answer:
    """Explica el estado de la RAM y desaconseja el uso de optimizadores externos."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    mem_pct: float = ctx.get_metric("memory_available_percent", 50.0)
    total_gb: float = ctx.get_metric("memory_total_gb", 0.0)
    startup_count: int = int(ctx.get_metric("startup_count", 0.0))
    status_msg = f"Tenés {mem_pct:.0f}% de RAM disponible{f' de {total_gb:.0f} GB' if total_gb > 0 else ''}."
    performance_tip = (
        "Eso es poco: Windows está usando el disco como memoria y ahí se siente la lentitud. Cerrá lo que no uses."
        if mem_pct < 15 else "Eso está bien. Si la PC va lenta, el problema seguramente no es la RAM."
    )
    msg_parts = [status_msg, performance_tip, "No busques un 'liberador de RAM': la PC queda más lenta."]
    if startup_count > 12:
        msg_parts.append(f"Sí te conviene mirar los {startup_count} programas de inicio.")
    full_text = " ".join(msg_parts)
    return Answer(_validate_response_length(full_text), notice=OFFLINE_NOTICE, suggestions=["¿Conviene desactivar programas de inicio?"])

def handle_disk(ctx: SystemContext, user_query: str) -> Answer:
    """Calcula el espacio total recuperable y diagnostica niveles críticos de almacenamiento."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    junk: float = ctx.get_metric("junk_mb", 0.0)
    dup: float = ctx.get_metric("duplicate_mb", 0.0)
    cache: float = ctx.get_metric("browser_cache_mb", 0.0)
    free: float = ctx.get_metric("disk_free_percent", 100.0)
    recuperable: float = junk + dup + cache
    diagnostico = f"Tenés {free:.0f}% libre en disco. Podés recuperar cerca de {recuperable:.0f} MB."
    detalle = f"Esto incluye: {junk:.0f} MB de basura, {dup:.0f} MB de duplicados{f' y {cache:.0f} MB de caché' if cache > 0 else ''}."
    advertencia = " Estás por debajo del 10%: esto afecta la estabilidad. Es urgente." if free < 10 else ""
    accion = " Empezá por Limpieza: mueve los candidatos a revisión."
    full_text = f"{diagnostico} {detalle}{advertencia}{accion}"
    return Answer(_validate_response_length(full_text), notice=OFFLINE_NOTICE)

def handle_security(ctx: SystemContext, user_query: str) -> Answer:
    """Informa sobre el estado de archivos detectados y reafirma la política de no-borrado automático."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    count: int = int(ctx.get_metric("suspicious_count", 0.0))
    warn: int = int(ctx.get_metric("suspicious_warnings", 0.0))
    if count == 0:
        texto = "No hay archivos sospechosos en tus Descargas. La app nunca borra sola, todo va a revisión."
    else:
        info = f"Hay {count} archivo(s) marcados, {warn} con advertencia."
        sugerencia = "Son señales, no una condena: si no reconocés alguno, usá 'Aislar hallazgos'."
        texto = f"{info} {sugerencia} La limpieza solo mueve a cuarentena."
    return Answer(_validate_response_length(texto), notice=OFFLINE_NOTICE)

def handle_score(ctx: SystemContext, user_query: str) -> Answer:
    """Provee un resumen ejecutivo de la salud del sistema basado en las métricas actuales."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    score_val: str = str(ctx.score) if ctx.score is not None else "N/A"
    grade: str = str(ctx.grade) if ctx.grade else ""
    score_display = f"Tu puntaje es {score_val}/100{f' (nota {grade})' if grade else ''}."
    problemas = _identify_active_problems(ctx)
    resumen = ("Lo que más te está restando: " + ", ".join(problemas[:3]) + ".") if problemas else "No hay nada urgente."
    explicacion = " El puntaje combina basura, seguridad, memoria, disco, duplicados y programas de inicio."
    return Answer(_validate_response_length(f"{score_display} {resumen}{explicacion}"), notice=OFFLINE_NOTICE)

def handle_startup(ctx: SystemContext, user_query: str) -> Answer:
    """Evalúa la cantidad de programas de inicio y su impacto en el rendimiento."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    count: int = int(ctx.get_metric("startup_count", 0.0))
    estado = f"Tenés {count} programas que arrancan con Windows."
    valoracion = "Son bastantes, y cada uno suma tiempo de encendido." if count > 15 else ("Es normal." if count > 8 else "Está bien.")
    cierre = " La app los lista, pero desactivalos desde el Administrador de tareas de Windows."
    return Answer(_validate_response_length(f"{estado} {valoracion}{cierre}"), notice=OFFLINE_NOTICE)

_KEYWORD_TO_HANDLER: Final[dict[str, Callable[[SystemContext, str], Answer]]] = {
    "ram": handle_ram, "memoria": handle_ram, "lenta": handle_ram, "lento": handle_ram, "acelerar": handle_ram,
    "espacio": handle_disk, "disco": handle_disk, "lleno": handle_disk, "recuperar": handle_disk, "liberar": handle_disk,
    "seguro": handle_security, "virus": handle_security, "sospechos": handle_security, "borrar": handle_security, "peligro": handle_security,
    "puntaje": handle_score, "salud": handle_score, "nota": handle_score, "score": handle_score,
    "inicio": handle_startup, "arranque": handle_startup, "arranca": handle_startup, "encender": handle_startup
}

def _sanitize_query(question: str) -> str:
    if not isinstance(question, str): return ""
    clean = _CONTROL_CHARS_REGEX.sub('', question)
    return clean.strip()[:100].lower()

def local_answer(question: str, context: SystemContext) -> Answer:
    q_sanitized = _sanitize_query(question)
    if not _ensure_safe_text(q_sanitized):
        return Answer("Entrada no válida.")
    if not isinstance(context, SystemContext) or not context.analyzed or not context.is_valid_structure:
        return Answer(
            text="Todavía no corriste ningún análisis. Andá a la pestaña Salud "
                 "y apretá 'Analizar el sistema': es de solo lectura.",
            notice=OFFLINE_NOTICE,
            suggestions=SUGGESTED_QUESTIONS_SHORT,
        )
    for token in _TOKEN_REGEX.findall(q_sanitized):
        if token in _KEYWORD_TO_HANDLER:
            return _KEYWORD_TO_HANDLER[token](context, question)
    cuerpo = _format_problem_message(
        _identify_active_problems(context), 
        str(context.score) if context.score is not None else "N/A"
    )
    return Answer(_validate_response_length(cuerpo), notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_SHORT)

def available(base: Union[str, Path, None] = None) -> bool:
    try:
        return settings.assistant_enabled(base)
    except Exception:
        return False

def _parse_config(raw_cfg: Any) -> AssistantConfig:
    if not isinstance(raw_cfg, dict):
        return AssistantConfig("", "gemini-3.1-flash-lite", True)
    return AssistantConfig(
        api_key=str(raw_cfg.get("asistente_api_key", "")),
        model=str(raw_cfg.get("asistente_modelo", "gemini-3.1-flash-lite")),
        allow_metrics=bool(raw_cfg.get("asistente_enviar_metricas", True))
    )

def _build_payload(question: str, context_text: str) -> Optional[bytes]:
    try:
        q = _sanitize_query(question)
        if not _ensure_safe_text(q): return None
        data = {"contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\nMétricas:\n{context_text}\n\nPregunta: {q}"}]}]}
        encoded = json.dumps(data).encode("utf-8")
        if len(encoded) > _MAX_PROMPT_LIMIT * 2:
            return None
        return encoded
    except (TypeError, ValueError):
        return None

def _extract_text_from_gemini_json(data: Any) -> Optional[str]:
    """Extrae la respuesta textual del payload JSON de la API con validación estricta."""
    if not isinstance(data, dict):
        return None
    try:
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or len(candidates) == 0 or not isinstance(candidates[0], dict):
            return None
        
        content = candidates[0].get("content")
        if not isinstance(content, dict):
            return None
        
        parts = content.get("parts")
        if not isinstance(parts, list) or len(parts) == 0 or not isinstance(parts[0], dict):
            return None
        
        text_val = parts[0].get("text")
        return str(text_val) if isinstance(text_val, str) else None
    except (KeyError, AttributeError, TypeError, IndexError):
        return None

def _call_gemini(question: str, context_text: str, api_key: str, model: str) -> Optional[str]:
    """Gestiona la llamada remota a Gemini con validaciones estrictas de seguridad."""
    if not _API_KEY_REGEX.match(api_key) or not _MODEL_NAME_REGEX.match(model): return None
    safe_c = _CONTROL_CHARS_REGEX.sub(" ", context_text)
    if not _ensure_safe_text(safe_c) or "Error" in safe_c: return None
    
    payload = _build_payload(question, safe_c)
    if not payload or len(payload) > _MAX_PROMPT_LIMIT: return None
    
    try:
        url = _ENDPOINT.format(model=model) + f"?key={api_key}"
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as res:
            if res.status != 200: return None
            raw_res = res.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw_res) > _MAX_RESPONSE_BYTES: return None
            
            data = json.loads(raw_res.decode("utf-8"))
            raw_text = _extract_text_from_gemini_json(data)
            if not raw_text: return None
            
            clean = _PATH_INJECTION_REGEX.sub(" ", _CONTROL_CHARS_REGEX.sub(" ", raw_text.strip()))
            final = _validate_response_length(clean)
            
            if _ensure_safe_text(final) and not is_protected_path(final):
                return final
            return None
            
    except (urllib.error.URLError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

def ask(question: str, context: Optional[SystemContext] = None,
        base: Union[str, Path, None] = None) -> Answer:
    if not _ensure_safe_text(question):
        return Answer("Entrada no válida.")
    ctx: SystemContext = context if isinstance(context, SystemContext) else SystemContext()
    respaldo: Answer = local_answer(question, ctx)
    if not available(base): return respaldo
    try:
        settings_data = settings.load(base)
        cfg = _parse_config(settings_data)
        texto_contexto = context_as_text(ctx) if cfg.allow_metrics else "El usuario no autorizó enviar métricas."
        remoto = _call_gemini(question, texto_contexto, cfg.api_key, cfg.model)
        if not remoto:
            respaldo.notice = "No se pudo consultar al asistente en línea, respondí con el motor local."
            return respaldo
        return Answer(remoto, source="gemini", notice=PRIVACY_NOTICE)
    except (Exception, TypeError, ValueError):
        return respaldo
