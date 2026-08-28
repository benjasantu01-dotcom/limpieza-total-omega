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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeAlias, Callable, Optional, Union, NamedTuple

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
    """Configuración validada del asistente para la interacción con la API."""
    api_key: str
    model: str
    allow_metrics: bool

@dataclass(frozen=True)
class MetricSpec:
    """Define los límites y el tipo de conversión para una métrica del sistema."""
    cast_func: Callable[[Any], Any]
    min_val: float
    max_val: float

class ProblemCriterion(NamedTuple):
    """Define una regla de salud lógica para la evaluación de métricas."""
    metric_key: str
    threshold: float
    operator: str  # '<' o '>'
    message_format: str

    def format_if_triggered(self, ctx: SystemContext) -> str | None:
        """
        Evalúa si la métrica contenida en el contexto supera el umbral definido.
        Retorna la cadena formateada si se cumple la condición, o None.
        """
        try:
            val = getattr(ctx, self.metric_key)
            f_val = _safe_float(val, -1.0)
            if f_val < 0:
                return None
            
            is_triggered = (self.operator == "<" and f_val < self.threshold) or \
                           (self.operator == ">" and f_val > self.threshold)
            
            return self.message_format.format(val)[:_MAX_MSG_CHUNK] if is_triggered else None
        except (AttributeError, ValueError, TypeError):
            return None

class AreaExplanation(NamedTuple):
    """Documentación pedagógica de las áreas de la aplicación."""
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

SUGGESTED_QUESTIONS_LIST: Final[list[str]] = list(SUGGESTED_QUESTIONS)
SUGGESTED_QUESTIONS_SHORT: Final[list[str]] = SUGGESTED_QUESTIONS_LIST[:3]

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
_MODEL_NAME_REGEX: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9\.\-_]+$")
_API_KEY_REGEX: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

_KEYWORD_MAP: Final[dict[str, str]] = {
    "ram": "ram", "memoria": "ram", "lenta": "ram", "lento": "ram", "acelerar": "ram",
    "espacio": "disco", "disco": "disco", "lleno": "disco", "recuperar": "disco", "liberar": "disco",
    "seguro": "security", "virus": "security", "sospechos": "security", "borrar": "security", "peligro": "security",
    "puntaje": "score", "salud": "score", "nota": "score", "score": "score",
    "inicio": "startup", "arranque": "startup", "arranca": "startup", "encender": "startup"
}

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

_FORBIDDEN_TYPES: Final[set[type]] = {list, dict, set, tuple, bool}

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Conversión segura a float. Retorna default si el valor no es numérico o es infinito."""
    if val is None or type(val) in _FORBIDDEN_TYPES:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
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

    def ingest(self, source: Any) -> bool:
        """
        Intenta extraer y validar métricas desde una fuente externa (dict u objeto).
        Retorna True si al menos una métrica válida fue procesada.
        """
        if not isinstance(source, (dict, object)):
            return False
            
        found_data = False
        for key, spec in _VALIDATORS.items():
            if _validate_and_assign(self, source, key, spec):
                found_data = True
        
        grade_val = _get_source_value(source, "grade")
        if isinstance(grade_val, str):
            clean_grade = grade_val[:10].strip()
            if _ensure_safe_text(clean_grade):
                self.grade = clean_grade
        
        return found_data

@dataclass
class Answer:
    """Respuesta generada por el asistente con metadatos de fuente y sugerencias."""
    text: str
    source: str = "local"
    notice: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        """Determina si la respuesta provino de una API externa."""
        return self.source == "gemini"

def _is_safe_text_structure(text: str) -> bool:
    """Valida la ausencia de rutas o comandos en un string."""
    if _PATH_INJECTION_REGEX.search(text) or is_protected_path(text):
        return False
    return True

def _ensure_safe_text(text: Any) -> bool:
    """
    Validación de seguridad crítica para strings de entrada/salida.
    Verifica ausencia de caracteres de control, rutas prohibidas y límites de tamaño.
    """
    if not isinstance(text, str) or not text:
        return False
    if len(text) > _MAX_TEXT_LENGTH:
        return False
    if _CONTROL_CHARS_REGEX.search(text):
        return False
    return _is_safe_text_structure(text)

def _get_source_value(source: Any, key: str) -> Any:
    """Centraliza la extracción de valores desde diccionarios u objetos."""
    try:
        if isinstance(source, dict):
            return source.get(key)
        return getattr(source, key, None)
    except Exception:
        return None

def _validate_and_assign(ctx: SystemContext, source: Any, key: str, spec: MetricSpec) -> bool:
    """
    Extrae una métrica individual de 'source' mediante 'key', la valida contra
    las restricciones de 'spec' y la asigna al 'ctx' si es correcta.
    """
    try:
        val = _get_source_value(source, key)
        if val is None or type(val) in _FORBIDDEN_TYPES: 
            return False
        
        clean_val = _safe_float(val, -1.0)
        if clean_val < spec.min_val or clean_val > spec.max_val:
            return False
        
        setattr(ctx, key, spec.cast_func(clean_val))
        return True
    except (ValueError, TypeError, AttributeError):
        return False

def build_context(metrics: MetricSource = None, health: ScoreSource = None, **extra: Any) -> SystemContext:
    """
    Construye el objeto SystemContext validando datos contra los validadores registrados.
    """
    ctx = SystemContext()
    sources = [s for s in (metrics, health, extra) if isinstance(s, (dict, object))]
    
    for src in sources:
        if ctx.ingest(src):
            ctx.analyzed = True
            
    return ctx

def _fmt_metric_sanitized(val: Any, unit: str = "", decimal: int = 0) -> str:
    """Formatea métricas y elimina cualquier residuo de caracteres de control o inyección."""
    raw = _fmt_metric(val, unit, decimal)
    return _PATH_INJECTION_REGEX.sub(" ", _CONTROL_CHARS_REGEX.sub(" ", raw))

def context_as_text(context: SystemContext) -> str:
    """Serializa el estado del sistema en un formato de texto plano y seguro."""
    if not isinstance(context, SystemContext) or not context.analyzed:
        return "No hay métricas disponibles todavía."
    try:
        score_val = _fmt_metric_sanitized(context.score) if context.score is not None else "N/A"
        grade_val = str(context.grade)[:5] if isinstance(context.grade, str) else ""
        lines = (
            f"Puntaje de salud: {score_val}{f' nota {grade_val}' if grade_val else ''}",
            f"Basura: {_fmt_metric_sanitized(context.junk_mb, ' MB')}",
            f"Sospechosos: {_fmt_metric_sanitized(context.suspicious_count)}",
            f"RAM disponible: {_fmt_metric_sanitized(context.memory_available_percent, ' percent')}",
            f"Disco libre: {_fmt_metric_sanitized(context.disk_free_percent, ' percent')}",
            f"Duplicados: {_fmt_metric_sanitized(context.duplicate_mb, ' MB')}",
            f"Inicio: {_fmt_metric_sanitized(context.startup_count)} items"
        )
        texto_unificado = "\n".join(lines)
        if not _ensure_safe_text(texto_unificado):
            return "Error: el contexto generado no cumple los estándares de seguridad."
        return texto_unificado
    except Exception:
        return "Error crítico al procesar métricas de seguridad."

def _fmt_metric(val: Any, unit: str = "", decimal: int = 0) -> str:
    """Formateador base de bajo nivel para convertir valores numéricos a strings."""
    f = _safe_float(val, -1.0)
    return "N/A" if f < 0 else f"{f:.{decimal}f}{unit}"

def explain_area(area: Any) -> str:
    """Devuelve explicaciones pedagógicas de los módulos."""
    if not isinstance(area, str):
        return "No tengo una explicación para esa área."
    return _validate_response_length(_EXPLANATION_MAP.get(area.strip().lower(), "No tengo una explicación para esa área."))

def _identify_active_problems(ctx: SystemContext) -> list[str]:
    """Evalúa el contexto actual contra los criterios de salud de forma eficiente."""
    if not isinstance(ctx, SystemContext) or not ctx.analyzed:
        return []
    
    problemas = []
    for crit in _CRITERIOS_SALUD:
        msg = crit.format_if_triggered(ctx)
        if msg:
            problemas.append(msg)
            if len(problemas) >= 3: break
    return problemas

def handle_ram(ctx: SystemContext, user_query: str) -> Answer:
    """Responde consultas sobre memoria RAM usando métricas de estado actual."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    mem_pct = _safe_float(ctx.memory_available_percent, 50.0)
    total_gb = _safe_float(ctx.memory_total_gb, 0.0)
    
    estado_msg = f"Tenés {mem_pct:.0f}% de RAM disponible{f' de {total_gb:.0f} GB' if total_gb > 0 else ''}."
    accion_msg = "Eso es poco: Windows está usando el disco como memoria y ahí se siente la lentitud. Cerrá lo que no uses; en la pestaña Memoria tenés qué consume más." if mem_pct < 15 else "Eso está bien. Si la PC va lenta, el problema seguramente no es la RAM."
    consejo_final = "No busques un 'liberador de RAM': suben el número de memoria libre pero la PC queda más lenta."
    startup_ad = f" Sí te conviene mirar los {ctx.startup_count} programas de inicio." if ctx.startup_count > 12 else ""
    
    texto = f"{estado_msg} {accion_msg} {consejo_final}{startup_ad}"
    return Answer(_validate_response_length(texto), notice=OFFLINE_NOTICE, suggestions=["¿Conviene desactivar programas de inicio?"])

def handle_disk(ctx: SystemContext, user_query: str) -> Answer:
    """Proporciona diagnóstico de espacio en disco y posibles acciones de recuperación."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    recuperable = _safe_float(ctx.junk_mb) + _safe_float(ctx.duplicate_mb) + _safe_float(ctx.browser_cache_mb)
    
    linea1 = f"Tenés {ctx.disk_free_percent:.0f}% libre en disco."
    linea2 = f"Podés recuperar cerca de {recuperable:.0f} MB: {ctx.junk_mb:.0f} MB de basura, {ctx.duplicate_mb:.0f} MB de duplicados{f' y {ctx.browser_cache_mb:.0f} MB de caché' if ctx.browser_cache_mb > 0 else ''}."
    alerta = " Estás por debajo del 10%, y ahí Windows empieza a andar mal. Es lo primero que atendería." if ctx.disk_free_percent < 10 else ""
    cierre = " Empezá por Limpieza: mueve los candidatos a una carpeta de revisión, no los borra."
    
    return Answer(_validate_response_length(f"{linea1} {linea2}{alerta}{cierre}"), notice=OFFLINE_NOTICE)

def handle_security(ctx: SystemContext, user_query: str) -> Answer:
    """Evalúa hallaggos de seguridad y explica los procedimientos de aislamiento."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    if ctx.suspicious_count == 0:
        texto = "No hay archivos sospechosos en tus Descargas. La app nunca borra sola. La limpieza mueve todo a una carpeta de revisión, y el borrado real es un botón aparte que pide confirmación."
    else:
        info = f"Hay {ctx.suspicious_count} archivo(s) marcados, {ctx.suspicious_warnings} con advertencia."
        sugerencia = "Son señales, no una condena: puede ser un instalador legítimo. Si no reconocés alguno, usá 'Aislar hallazgos' para mandarlo a cuarentena."
        cierre = " La app nunca borra sola; la limpieza mueve todo a revisión, y el borrado real pide confirmación."
        texto = f"{info} {sugerencia}{cierre}"
    
    return Answer(_validate_response_length(texto), notice=OFFLINE_NOTICE)

def handle_score(ctx: SystemContext, user_query: str) -> Answer:
    """Explica el cálculo y significado del puntaje de salud del sistema."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    score_val = str(ctx.score) if ctx.score is not None else "N/A"
    score_display = f"Tu puntaje es {score_val}/100{f' (nota {ctx.grade})' if ctx.grade else ''}."
    problemas = _identify_active_problems(ctx)
    resumen = ("Lo que más te está restando: " + ", ".join(problemas) + ".") if problemas else "No hay nada urgente para arreglar."
    explicacion = " El puntaje combina basura, seguridad, memoria, disco, duplicados y programas de inicio, con la seguridad pesando más."
    
    return Answer(_validate_response_length(f"{score_display} {resumen}{explicacion}"), notice=OFFLINE_NOTICE)

def handle_startup(ctx: SystemContext, user_query: str) -> Answer:
    """Analiza programas en el inicio y su impacto sugerido."""
    if not ctx.analyzed: return Answer("Primero analizá el sistema.")
    count = ctx.startup_count
    estado = f"Tenés {count} programas que arrancan con Windows."
    valoracion = "Son bastantes, y cada uno suma tiempo de encendido. Vale la pena revisarlos." if count > 15 else ("Es una cantidad normal, aunque se puede recortar." if count > 8 else "Está bien así.")
    cierre = " La app te los lista pero no los desactiva a propósito: hacelo desde el Administrador de tareas de Windows."
    
    return Answer(_validate_response_length(f"{estado} {valoracion}{cierre}"), notice=OFFLINE_NOTICE)

_HANDLERS: Final[dict[str, Callable[[SystemContext, str], Answer]]] = {
    "ram": handle_ram,
    "disco": handle_disk,
    "security": handle_security,
    "score": handle_score,
    "startup": handle_startup
}

def _sanitize_query(question: str) -> str:
    """Elimina caracteres de control y acorta el texto de entrada."""
    if not isinstance(question, str): return ""
    clean = _CONTROL_CHARS_REGEX.sub('', question)
    return clean.strip()[:100].lower()

def local_answer(question: str, context: SystemContext) -> Answer:
    """Motor de lógica local: responde consultas heurísticas."""
    q_sanitized = _sanitize_query(question)
    if not _ensure_safe_text(q_sanitized):
        return Answer("Entrada no válida.")

    if not isinstance(context, SystemContext) or not context.analyzed:
        return Answer(
            text="Todavía no corriste ningún análisis. Andá a la pestaña Salud "
                 "y apretá 'Analizar el sistema': es de solo lectura.",
            notice=OFFLINE_NOTICE,
            suggestions=SUGGESTED_QUESTIONS_SHORT,
        )

    # Optimizamos el loop usando un set de tokens únicos procesados una sola vez
    seen_intents = set()
    for token in set(_TOKEN_REGEX.findall(q_sanitized)):
        intent = _KEYWORD_MAP.get(token)
        if intent and intent not in seen_intents:
            seen_intents.add(intent)
            return _HANDLERS[intent](context, question)

    problemas = _identify_active_problems(context)
    puntaje_str = str(context.score) if context.score is not None else "N/A"
    if problemas:
        cuerpo = (f"Con un puntaje de {puntaje_str}/100, por orden de prioridad: "
                  f"{', '.join(problemas)}.")
    else:
        cuerpo = f"Tu sistema está en buen estado ({puntaje_str}/100). No hay nada urgente."
    return Answer(_validate_response_length(cuerpo), notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_SHORT)

def available(base: Union[str, Path, None] = None) -> bool:
    """Consulta la configuración de usuario para determinar si la IA en la nube está habilitada."""
    try:
        return settings.assistant_enabled(base)
    except Exception:
        return False

def _parse_config(raw_cfg: Any) -> AssistantConfig:
    """Extrae y normaliza la configuración del asistente desde un dict crudo."""
    if not isinstance(raw_cfg, dict):
        return AssistantConfig("", "gemini-3.1-flash-lite", True)
    return AssistantConfig(
        api_key=str(raw_cfg.get("asistente_api_key", "")),
        model=str(raw_cfg.get("asistente_modelo", "gemini-3.1-flash-lite")),
        allow_metrics=bool(raw_cfg.get("asistente_enviar_metricas", True))
    )

def _call_gemini(
    question: str, 
    context_text: str, 
    api_key: str, 
    model: str
) -> Optional[str]:
    """Invoca la API de Gemini realizando validaciones de seguridad previas y posteriores."""
    if not isinstance(api_key, str) or not isinstance(model, str) or not api_key: return None
    if not _API_KEY_REGEX.match(api_key) or not _MODEL_NAME_REGEX.match(model): return None
    
    safe_q = _sanitize_query(question)
    safe_c = _CONTROL_CHARS_REGEX.sub(" ", context_text)
    
    if not _ensure_safe_text(safe_q) or not _ensure_safe_text(safe_c): return None
    
    try:
        prompt_full = f"{SYSTEM_PROMPT}\n\nMétricas del sistema:\n{safe_c}\n\nPregunta del usuario: {safe_q}"
        if len(prompt_full) > _MAX_PROMPT_LIMIT: return None
        
        payload = json.dumps({"contents": [{"parts": [{"text": prompt_full}]}]}).encode("utf-8")
        req = urllib.request.Request(
            _ENDPOINT.format(model=model) + f"?key={api_key}", 
            data=payload, 
            headers={"Content-Type": "application/json; charset=utf-8"}, 
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as res:
            if res.status != 200: return None
            
            content_type = res.getheader("Content-Type", "")
            if "application/json" not in content_type: return None

            length_header = res.getheader("Content-Length")
            if length_header and int(length_header) > _MAX_RESPONSE_BYTES: return None
            
            # Leer en chunks y validar límite total
            raw_res = res.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw_res) > _MAX_RESPONSE_BYTES: return None
            
            try:
                data = json.loads(raw_res.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            
            if not isinstance(data, dict): return None
            candidates = data.get("candidates")
            if not isinstance(candidates, list) or not candidates: return None
            
            first_candidate = candidates[0]
            if not isinstance(first_candidate, dict): return None
            
            content = first_candidate.get("content")
            if not isinstance(content, dict): return None
            
            parts = content.get("parts")
            if not isinstance(parts, list): return None
            
            raw_text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
            
            # Sanitización crítica de salida externa
            limpia_final = _PATH_INJECTION_REGEX.sub(" ", _CONTROL_CHARS_REGEX.sub(" ", raw_text.strip()))
            
            # Validación de integridad: el modelo no debe emitir rutas protegidas
            if not _ensure_safe_text(limpia_final): return None
            
            final_text = _validate_response_length(limpia_final)
            return final_text if _ensure_safe_text(final_text) else None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None

def ask(question: str, context: Optional[SystemContext] = None,
        base: Union[str, Path, None] = None) -> Answer:
    """Orquestador de consultas que elige entre motor local y Gemini según configuración."""
    if not _ensure_safe_text(question):
        return Answer("Entrada no válida.")
        
    ctx: SystemContext = context if isinstance(context, SystemContext) else SystemContext()
    respaldo: Answer = local_answer(question, ctx)
    
    if not available(base): return respaldo
    
    try:
        cfg = _parse_config(settings.load(base))
        texto_contexto = context_as_text(ctx) if cfg.allow_metrics else "El usuario no autorizó enviar métricas."
        remoto = _call_gemini(question, texto_contexto, cfg.api_key, cfg.model)
        
        if not remoto:
            respaldo.notice = "No se pudo consultar al asistente en línea, respondí con el motor local."
            return respaldo
        return Answer(remoto, source="gemini", notice=PRIVACY_NOTICE)
    except (Exception, TypeError, ValueError):
        return respaldo
