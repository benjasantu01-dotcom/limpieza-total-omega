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
from typing import Any, Final, TypeAlias, Callable, Optional, Union, TypedDict, NamedTuple

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

class AssistantConfig(TypedDict):
    """Esquema estricto de la configuración cargada desde el archivo de ajustes."""
    asistente_api_key: str
    asistente_modelo: str
    asistente_enviar_metricas: bool

class ProblemCriterion(NamedTuple):
    """Define una regla de salud lógica para la evaluación de métricas."""
    metric_key: str
    threshold: float
    operator: str  # '<' o '>'
    message_format: str

    def format_if_triggered(self, ctx: SystemContext) -> str | None:
        """Evalúa si la métrica rompe el umbral y devuelve el mensaje formateado."""
        val = getattr(ctx, self.metric_key, -1.0)
        f_val = _safe_float(val, -1.0)
        if f_val < 0:
            return None
        
        is_triggered = (self.operator == "<" and f_val < self.threshold) or \
                       (self.operator == ">" and f_val > self.threshold)
        
        return self.message_format.format(val)[:_MAX_MSG_CHUNK] if is_triggered else None

class AreaExplanation(NamedTuple):
    """Documentación pedagógica de las áreas de la aplicación."""
    key: str
    description: str

# TypeAliases para mejorar la legibilidad de las firmas de funciones
MetricSource: TypeAlias = dict[str, Any] | object
ScoreSource: TypeAlias = dict[str, Any] | object
ValidatorSpec: TypeAlias = tuple[Callable[[Any], float], float, float]

# Límites de seguridad para prevenir ataques de denegación de servicio por procesamiento de texto
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

_EXPLICACIONES: Final[tuple[AreaExplanation, ...]] = (
    AreaExplanation("basura", "Archivos temporales y restos de instaladores: ocupan espacio innecesario sin aportar valor operativo."),
    AreaExplanation("seguridad", "Archivos con señales de riesgo: extensiones inusuales o ejecutables sin firma, requieren revisión manual."),
    AreaExplanation("memoria", "Recursos de acceso rápido: si la memoria disponible es baja, Windows utiliza el disco duro, ralentizando todo."),
    AreaExplanation("disco", "Almacenamiento disponible: niveles inferiores al 10% afectan la estabilidad y velocidad de escritura de Windows."),
    AreaExplanation("duplicados", "Copias idénticas del mismo archivo: se pueden eliminar de forma segura ya que el archivo original permanece."),
    AreaExplanation("inicio", "Programas que arrancan con Windows: cada entrada incrementa el tiempo de inicio y el consumo base de memoria."),
)

_VALIDATORS: Final[dict[str, ValidatorSpec]] = {
    "junk_mb": (float, 0.0, 1e9),
    "suspicious_count": (int, 0, 10000),
    "suspicious_warnings": (int, 0, 10000),
    "memory_available_percent": (float, 0.0, 100.0),
    "memory_total_gb": (float, 0.0, 2048.0),
    "disk_free_percent": (float, 0.0, 100.0),
    "duplicate_mb": (float, 0.0, 1e9),
    "startup_count": (int, 0, 1000),
    "quarantined_count": (int, 0, 10000),
    "browser_cache_mb": (float, 0.0, 1e6),
    "score": (int, 0, 100),
}

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Conversión segura a float. Retorna default si el valor no es numérico o es infinito."""
    if val is None or isinstance(val, (list, dict, set, tuple, bool)):
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default

def _validate_response_length(text: str) -> str:
    """Trunca el texto para cumplir con el límite máximo de caracteres del motor."""
    return text[:_MAX_TEXT_LENGTH]

@dataclass
class SystemContext:
    """Contenedor de estado del sistema. Mantiene únicamente métricas agregadas."""
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

@dataclass
class Answer:
    """Respuesta generada por el asistente con metadatos."""
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
    """Validación exhaustiva de integridad y seguridad para texto de entrada/salida."""
    if not isinstance(text, str) or not text:
        return False
    if len(text) > _MAX_TEXT_LENGTH:
        return False
    if _CONTROL_CHARS_REGEX.search(text):
        return False
    return _is_safe_text_structure(text)

def _validate_and_assign(ctx: SystemContext, source: MetricSource, key: str, spec: ValidatorSpec) -> bool:
    """Extrae y valida una métrica individual desde una fuente de datos."""
    cast, min_v, max_v = spec
    
    # Verificación estricta de existencia y tipo de la fuente
    if isinstance(source, dict):
        val = source.get(key)
    elif hasattr(source, key):
        val = getattr(source, key, None)
    else:
        return False
    
    # Validamos que el valor no sea una estructura compleja o nulo
    if val is None or isinstance(val, (dict, list, set, tuple)):
        return False

    clean_val = _safe_float(val, -1.0)
    if clean_val < 0 and key != "score": 
        return False
    
    try:
        if clean_val >= 0:
            clamped = max(float(min_v), min(clean_val, float(max_v)))
            setattr(ctx, key, cast(clamped))
        return True
    except (OverflowError, ValueError, TypeError):
        return False

def build_context(metrics: MetricSource = None, health: ScoreSource = None, **extra: Any) -> SystemContext:
    """Construye el objeto SystemContext validando datos contra _VALIDATORS."""
    ctx = SystemContext()
    found_data = False
    
    for key, spec in _VALIDATORS.items():
        for src in (metrics, health, extra):
            if src is not None and _validate_and_assign(ctx, src, key, spec):
                found_data = True
                break

    for src in (health, extra):
        if src is None: continue
        g_val = src.get("grade") if isinstance(src, dict) else getattr(src, "grade", None)
        if isinstance(g_val, (str, int, float)):
            g_str = str(g_val)[:10].strip()
            if _ensure_safe_text(g_str):
                ctx.grade = g_str
            
    ctx.analyzed = found_data
    return ctx

def _fmt_metric_sanitized(val: Any, unit: str = "", decimal: int = 0) -> str:
    """Formatea métricas y elimina cualquier posible residuo de caracteres de control."""
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
    """Formateador base de bajo nivel para valores numéricos."""
    f = _safe_float(val, -1.0)
    return "N/A" if f < 0 else f"{f:.{decimal}f}{unit}"

def explain_area(area: Any) -> str:
    """Delvuelve explicaciones pedagógicas de los módulos de la app."""
    if not isinstance(area, str):
        return "No tengo una explicación para esa área."
        
    query = area.strip().lower()
    for item in _EXPLICACIONES:
        if item.key == query:
            return _validate_response_length(item.description)
            
    return "No tengo una explicación para esa área."

def _identify_active_problems(ctx: SystemContext) -> list[str]:
    """Recopila la lista de problemas actuales basándose en los criterios definidos."""
    problemas: list[str] = []
    for crit in _CRITERIOS_SALUD:
        msg = crit.format_if_triggered(ctx)
        if msg:
            problemas.append(msg)
            if len(problemas) >= 3: break
    return problemas

def handle_ram(ctx: SystemContext, user_query: str) -> Answer:
    """Motor local: gestiona consultas sobre uso de memoria RAM."""
    mem_pct = _safe_float(ctx.memory_available_percent, 50.0)
    total_gb = _safe_float(ctx.memory_total_gb, 0.0)
    
    estado_msg = f"Tenés {mem_pct:.0f}% de RAM disponible{f' de {total_gb:.0f} GB' if total_gb > 0 else ''}."
    accion_msg = "Eso es poco: Windows está usando el disco como memoria y ahí se siente la lentitud. Cerrá lo que no uses; en la pestaña Memoria tenés qué consume más." if mem_pct < 15 else "Eso está bien. Si la PC va lenta, el problema seguramente no es la RAM."
    consejo_final = "No busques un 'liberador de RAM': suben el número de memoria libre pero la PC queda más lenta."
    startup_ad = f" Sí te conviene mirar los {int(ctx.startup_count)} programas de inicio." if ctx.startup_count > 12 else ""
    
    texto = f"{estado_msg} {accion_msg} {consejo_final}{startup_ad}"
    return Answer(_validate_response_length(texto), notice=OFFLINE_NOTICE, suggestions=["¿Conviene desactivar programas de inicio?"])

def handle_disk(ctx: SystemContext, user_query: str) -> Answer:
    """Motor local: gestiona consultas sobre uso de espacio en disco."""
    recuperable = _safe_float(ctx.junk_mb) + _safe_float(ctx.duplicate_mb) + _safe_float(ctx.browser_cache_mb)
    
    linea1 = f"Tenés {ctx.disk_free_percent:.0f}% libre en disco."
    linea2 = f"Podés recuperar cerca de {recuperable:.0f} MB: {ctx.junk_mb:.0f} MB de basura, {ctx.duplicate_mb:.0f} MB de duplicados{f' y {ctx.browser_cache_mb:.0f} MB de caché' if ctx.browser_cache_mb > 0 else ''}."
    alerta = " Estás por debajo del 10%, y ahí Windows empieza a andar mal. Es lo primero que atendería." if ctx.disk_free_percent < 10 else ""
    cierre = " Empezá por Limpieza: mueve los candidatos a una carpeta de revisión, no los borra."
    
    return Answer(_validate_response_length(f"{linea1} {linea2}{alerta}{cierre}"), notice=OFFLINE_NOTICE)

def handle_security(ctx: SystemContext, user_query: str) -> Answer:
    """Motor local: gestiona consultas sobre hallazgos de seguridad."""
    if ctx.suspicious_count == 0:
        texto = "No hay archivos sospechosos en tus Descargas. La app nunca borra sola. La limpieza mueve todo a una carpeta de revisión, y el borrado real es un botón aparte que pide confirmación."
    else:
        info = f"Hay {ctx.suspicious_count} archivo(s) marcados, {ctx.suspicious_warnings} con advertencia."
        sugerencia = "Son señales, no una condena: puede ser un instalador legítimo. Si no reconocés alguno, usá 'Aislar hallazgos' para mandarlo a cuarentena."
        cierre = " La app nunca borra sola; la limpieza mueve todo a revisión, y el borrado real pide confirmación."
        texto = f"{info} {sugerencia}{cierre}"
    
    return Answer(_validate_response_length(texto), notice=OFFLINE_NOTICE)

def handle_score(ctx: SystemContext, user_query: str) -> Answer:
    """Motor local: gestiona consultas sobre el puntaje de salud del equipo."""
    score_display = f"Tu puntaje es {ctx.score if ctx.score is not None else 'N/A'}/100{f' (nota {ctx.grade})' if ctx.grade else ''}."
    problemas = _identify_active_problems(ctx)
    resumen = ("Lo que más te está restando: " + ", ".join(problemas) + ".") if problemas else "No hay nada urgente para arreglar."
    explicacion = " El puntaje combina basura, seguridad, memoria, disco, duplicados y programas de inicio, con la seguridad pesando más."
    
    return Answer(_validate_response_length(f"{score_display} {resumen}{explicacion}"), notice=OFFLINE_NOTICE)

def handle_startup(ctx: SystemContext, user_query: str) -> Answer:
    """Motor local: gestiona consultas sobre programas que inician con el sistema."""
    estado = f"Tenés {int(ctx.startup_count)} programas que arrancan con Windows."
    valoracion = "Son bastantes, y cada uno suma tiempo de encendido. Vale la pena revisarlos." if ctx.startup_count > 15 else ("Es una cantidad normal, aunque se puede recortar." if ctx.startup_count > 8 else "Está bien así.")
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
    """Sanitiza el input del usuario eliminando caracteres especiales."""
    if not isinstance(question, str): return ""
    clean = re.sub(r'[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e]', '', question)
    return clean.strip()[:100].lower()

def local_answer(question: str, context: SystemContext) -> Answer:
    """Motor de lógica local: responde consultas basándose en reglas heurísticas."""
    if not _ensure_safe_text(question):
        return Answer("Entrada no válida.")

    if not isinstance(context, SystemContext) or not context.analyzed:
        return Answer(
            text="Todavía no corriste ningún análisis. Andá a la pestaña Salud "
                 "y apretá 'Analizar el sistema': es de solo lectura.",
            notice=OFFLINE_NOTICE,
            suggestions=SUGGESTED_QUESTIONS_LIST[:3],
        )

    tokens = set(_TOKEN_REGEX.findall(_sanitize_query(question)))
    found_key = next((_KEYWORD_MAP[t] for t in tokens if t in _KEYWORD_MAP), None)
    
    if found_key and callable(_HANDLERS.get(found_key)):
        return _HANDLERS[found_key](context, question)

    problemas = _identify_active_problems(context)
    puntaje_str = str(context.score) if context.score is not None else "N/A"
    if problemas:
        cuerpo = (f"Con un puntaje de {puntaje_str}/100, por orden de prioridad: "
                  f"{', '.join(problemas)}.")
    else:
        cuerpo = f"Tu sistema está en buen estado ({puntaje_str}/100). No hay nada urgente."
    return Answer(_validate_response_length(cuerpo), notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_LIST[:3])

def available(base: Union[str, Path, None] = None) -> bool:
    """Verifica habilitación de funcionalidades en la nube a través de settings."""
    try:
        return settings.assistant_enabled(base)
    except Exception:
        return False

def _call_gemini(
    question: str, 
    context_text: str, 
    api_key: str, 
    model: str
) -> Optional[str]:
    """Invoca la API de Gemini enviando un prompt construido bajo estrictas políticas."""
    if not isinstance(api_key, str) or not isinstance(model, str) or not api_key: return None
    # Validaciones defensivas de configuración
    if not _API_KEY_REGEX.match(api_key) or not _MODEL_NAME_REGEX.match(model): return None
    if _PATH_INJECTION_REGEX.search(api_key) or _PATH_INJECTION_REGEX.search(model): return None
    
    safe_q = _sanitize_query(question)
    if not _ensure_safe_text(safe_q) or not _ensure_safe_text(context_text): return None
    
    try:
        prompt_full = f"{SYSTEM_PROMPT}\n\nMétricas del sistema:\n{context_text}\n\nPregunta del usuario: {safe_q}"
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
            
            length = res.getheader("Content-Length")
            if length and int(length) > _MAX_RESPONSE_BYTES: return None
            
            raw_res = res.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw_res) > _MAX_RESPONSE_BYTES: return None
            
            try:
                data = json.loads(raw_res.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            
            candidates = data.get("candidates", [])
            if not candidates or not isinstance(candidates[0].get("content", {}).get("parts"), list):
                return None
            
            text = "".join(str(p.get("text", "")) for p in candidates[0]["content"]["parts"] if isinstance(p, dict))
            
            # Sanitización de la respuesta recibida antes de cualquier procesamiento
            limpia_final = _PATH_INJECTION_REGEX.sub(" ", _CONTROL_CHARS_REGEX.sub(" ", text.strip()))
            final_text = _validate_response_length(limpia_final)
            
            return final_text if _ensure_safe_text(final_text) else None
    except (urllib.error.URLError, KeyError, TypeError, ValueError):
        return None

def ask(question: str, context: Optional[SystemContext] = None,
        base: Union[str, Path, None] = None) -> Answer:
    """Orquestador de consultas: elige motor local o remoto aplicando fallbacks seguros."""
    if not _ensure_safe_text(question):
        return Answer("Entrada no válida.")
        
    ctx: SystemContext = context if isinstance(context, SystemContext) else SystemContext()
    respaldo: Answer = local_answer(question, ctx)
    
    if not available(base): return respaldo
    
    try:
        raw_cfg = settings.load(base)
        if not isinstance(raw_cfg, dict): return respaldo
        
        cfg: AssistantConfig = {
            "asistente_api_key": str(raw_cfg.get("asistente_api_key", "")),
            "asistente_modelo": str(raw_cfg.get("asistente_modelo", "gemini-3.1-flash-lite")),
            "asistente_enviar_metricas": bool(raw_cfg.get("asistente_enviar_metricas", True))
        }
        
        texto_contexto = context_as_text(ctx) if cfg["asistente_enviar_metricas"] else "El usuario no autorizó enviar métricas."
        remoto = _call_gemini(question, texto_contexto, cfg["asistente_api_key"], cfg["asistente_modelo"])
        
        if not remoto:
            respaldo.notice = "No se pudo consultar al asistente en línea, respondí con el motor local."
            return respaldo
        return Answer(remoto, source="gemini", notice=PRIVACY_NOTICE)
    except (Exception, TypeError, ValueError):
        return respaldo
