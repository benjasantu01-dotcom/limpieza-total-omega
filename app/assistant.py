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
from typing import Any, Final, TypeAlias, Callable, Optional, Union, Generator, TypedDict

import settings
from safety import is_protected_path, filter_safe_paths

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
    "available",
    "explain_area",
]

class AssistantConfig(TypedDict):
    """Esquema de configuración cargado desde el archivo de ajustes."""
    asistente_api_key: str
    asistente_modelo: str
    asistente_enviar_metricas: bool

MetricSource: TypeAlias = Any
ScoreSource: TypeAlias = Any

_MAX_TEXT_LENGTH: Final[int] = 1000
_MAX_RESPONSE_BYTES: Final[int] = 32768

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
    "El asistente en línea envía a Google solo números agregados: MB de "
    "basura, cantidad de archivos sospechosos, porcentaje de RAM y disco "
    "libres, cantidad de programas de inicio y el puntaje de salud. Nunca "
    "envía rutas, nombres ni contenido de archivos. Podés apagarlo en Ajustes."
)

OFFLINE_NOTICE: Final[str] = (
    "Respondido por el motor local, sin conexión ni envío de datos. "
    "Para preguntas escritas con tus palabras, activá el asistente en Ajustes."
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
    "- No prometas resultados mágicos. Si algo no vale la pena, decilo.\n"
    "- Los 'limpiadores de RAM' que liberan memoria a la fuerza empeoran el "
    "rendimiento: la RAM ocupada como caché es lo que hace que los programas "
    "abran rápido. Si te preguntan por eso, explicalo.\n"
    "- Nunca digas que borraste o cambiaste algo: vos solo aconsejás. Indicá "
    "qué pestaña de la app usar.\n"
    "- Si te preguntan algo que no se puede saber con estas métricas, decí "
    "que hace falta correr el análisis correspondiente.\n"
    "- Máximo 6 líneas."
)

_ENDPOINT: Final[str] = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TIMEOUT_SECONDS: Final[int] = 30
_PATH_REGEX: Final[re.Pattern] = re.compile(r"([a-zA-Z]:\\|/|\\|\.\.|\0|[\u202e\u202d])")
_CONTROL_CHARS_REGEX: Final[re.Pattern] = re.compile(r"[\x00-\x1f\x7f]")
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

_CRITERIOS_SALUD: Final = (
    ("disk_free_percent", 10.0, "<", "{:.0f}% de disco libre"),
    ("suspicious_warnings", 0, ">", "{:d} archivo(s) sospechosos"),
    ("memory_available_percent", 15.0, "<", "{:.0f}% de RAM"),
    ("junk_mb", 1000.0, ">", "{:.0f} MB de basura"),
    ("duplicate_mb", 500.0, ">", "{:.0f} MB en duplicados"),
    ("startup_count", 15, ">", "{:d} programas de inicio")
)

@dataclass
class SystemContext:
    """
    Representa el estado actual del sistema mediante métricas agregadas.
    Todas las métricas deben ser valores numéricos simples sin rastro de rutas.
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


@dataclass
class Answer:
    """Respuesta generada por el asistente con metadatos de origen."""
    text: str
    source: str = "local"
    notice: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        """Indica si la respuesta fue generada por el motor Gemini."""
        return self.source == "gemini"

def _ensure_safe_text(text: Any) -> bool:
    """
    Verifica que el texto sea seguro: longitud máxima, sin caracteres de control
    y, crucialmente, sin patrones que sugieran rutas o inyecciones de path.
    """
    if not isinstance(text, str) or not text:
        return False
    if len(text) > _MAX_TEXT_LENGTH:
        return False
    if _CONTROL_CHARS_REGEX.search(text):
        return False
    if is_protected_path(text) or _PATH_REGEX.search(text):
        return False
    return True

def _safe_assign(obj: SystemContext, attr: str, val: Any, cast: Callable = float, min_val: float = 0.0, max_val: float = float('inf')) -> None:
    """
    Asigna de forma robusta un valor a un atributo de SystemContext.
    Aplica transformación de tipo, validación de finitud y límites (clamping).
    """
    if val is None:
        return
    try:
        if not isinstance(val, (int, float, str)):
            return
        
        clean = cast(val)
        if isinstance(clean, (int, float)) and math.isfinite(clean):
            setattr(obj, attr, max(min_val, min(clean, max_val)))
    except (ValueError, TypeError):
        pass

def _fmt_metric(val: Any, unit: str = "", decimal: int = 0) -> str:
    """
    Convierte una métrica a string formateado, asegurando que el valor sea numérico válido.
    Retorna 'N/A' en caso de datos corruptos para evitar que la UI falle.
    """
    if val is None or not isinstance(val, (int, float)) or not math.isfinite(val): return "N/A"
    return f"{val:.{decimal}f}{unit}"

def _get_metric_val(source: dict[str, Any] | object, key: str, default: Any) -> Any:
    """
    Intenta extraer un valor de una fuente (dict o clase) de forma defensiva,
    garantizando que el resultado sea procesable numéricamente.
    """
    if source is None:
        return default
    
    val = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
    
    if val is None or not isinstance(val, (int, float, str)):
        return default
    
    try:
        num = float(val)
        return num if math.isfinite(num) else default
    except (ValueError, TypeError):
        return default

def build_context(metrics: MetricSource = None, health: ScoreSource = None, **extra: Any) -> SystemContext:
    """
    Constructor centralizado para el estado del sistema. Aplica saneamiento
    estricto a cada campo para asegurar la integridad total de la estructura.
    """
    ctx = SystemContext()
    
    if isinstance(metrics, (dict, object)):
        _safe_assign(ctx, "junk_mb", _get_metric_val(metrics, "junk_mb", 0.0))
        _safe_assign(ctx, "suspicious_count", _get_metric_val(metrics, "suspicious_count", 0), int)
        _safe_assign(ctx, "suspicious_warnings", _get_metric_val(metrics, "suspicious_warnings", 0), int)
        _safe_assign(ctx, "memory_available_percent", _get_metric_val(metrics, "memory_available_percent", 0.0), max_val=100.0)
        _safe_assign(ctx, "memory_total_gb", _get_metric_val(metrics, "memory_total_gb", 0.0))
        _safe_assign(ctx, "disk_free_percent", _get_metric_val(metrics, "disk_free_percent", 0.0), max_val=100.0)
        _safe_assign(ctx, "duplicate_mb", _get_metric_val(metrics, "duplicate_mb", 0.0))
        _safe_assign(ctx, "startup_count", _get_metric_val(metrics, "startup_count", 0), int)
        _safe_assign(ctx, "quarantined_count", _get_metric_val(metrics, "quarantined_count", 0), int)
        _safe_assign(ctx, "browser_cache_mb", _get_metric_val(metrics, "browser_cache_mb", 0.0))
        ctx.analyzed = True

    if isinstance(health, (dict, object)):
        raw_score = _get_metric_val(health, "score", None)
        if raw_score is not None: _safe_assign(ctx, "score", raw_score, int, max_val=100)
        grade = _get_metric_val(health, "grade", "")
        if grade is not None:
            ctx.grade = str(grade)[:10]
        ctx.analyzed = True

    for k, v in extra.items():
        if k in ctx.__annotations__ and v is not None:
            attr_val = getattr(ctx, k)
            if isinstance(attr_val, (int, float)):
                _safe_assign(ctx, k, v, cast=type(attr_val))
    return ctx

def context_as_text(context: SystemContext) -> str:
    """
    Serializa el contexto a formato texto para Gemini. Aplica una doble capa
    de limpieza contra caracteres de control y posibles rutas disfrazadas.
    """
    if not isinstance(context, SystemContext) or not context.analyzed:
        return "No hay métricas disponibles todavía."
    try:
        score_val = _fmt_metric(context.score)
        grade_val = str(context.grade)[:5] if isinstance(context.grade, str) else ""
        lines = (
            f"Puntaje de salud: {score_val}{f' nota {grade_val}' if grade_val else ''}",
            f"Basura: {_fmt_metric(context.junk_mb, ' MB')}",
            f"Sospechosos: {context.suspicious_count}",
            f"RAM disponible: {_fmt_metric(context.memory_available_percent, ' percent')}",
            f"Disco libre: {_fmt_metric(context.disk_free_percent, ' percent')}",
            f"Duplicados: {_fmt_metric(context.duplicate_mb, ' MB')}",
            f"Inicio: {context.startup_count} items"
        )
        texto_unificado = "\n".join(lines)
        texto_limpio = _PATH_REGEX.sub(" ", _CONTROL_CHARS_REGEX.sub(" ", texto_unificado))
        if not _ensure_safe_text(texto_limpio):
            return "Error de seguridad en la serialización de contexto."
        return texto_limpio
    except Exception:
        return "Error al procesar métricas para el asistente."

def explain_area(area: Any) -> str:
    """
    Proporciona una explicación educativa sobre los conceptos clave de la aplicación.
    Ayuda al usuario a entender el 'porqué' detrás de las métricas.
    """
    explicaciones: Final[dict[str, str]] = {
        "basura": "Archivos temporales y restos de instaladores: ocupan espacio innecesario sin aportar valor operativo.",
        "seguridad": "Archivos con señales de riesgo: extensiones inusuales o ejecutables sin firma, requieren revisión manual.",
        "memoria": "Recursos de acceso rápido: si la memoria disponible es baja, Windows utiliza el disco duro, ralentizando todo.",
        "disco": "Almacenamiento disponible: niveles inferiores al 10% afectan la estabilidad y velocidad de escritura de Windows.",
        "duplicados": "Copias idénticas del mismo archivo: se pueden eliminar de forma segura ya que el archivo original permanece.",
        "inicio": "Programas que arrancan con Windows: cada entrada incrementa el tiempo de inicio y el consumo base de memoria.",
    }
    if isinstance(area, str):
        return explicaciones.get(area.strip().lower(), "No tengo una explicación para esa área.")
    return "No tengo una explicación para esa área."

def handle_ram(ctx: SystemContext, user_query: str) -> Answer:
    """Responde preguntas sobre el estado actual de la memoria RAM."""
    partes = [
        f"Tenés {ctx.memory_available_percent:.0f}% de RAM disponible"
        f"{f' de {ctx.memory_total_gb:.0f} GB' if ctx.memory_total_gb > 0 else ''}."
    ]
    if ctx.memory_available_percent < 15:
        partes.append("Eso es poco: Windows está usando el disco como memoria y ahí se siente la lentitud. Cerrá lo que no uses; en la pestaña Memoria tenés qué consume más.")
    else:
        partes.append("Eso está bien. Si la PC va lenta, el problema seguramente no es la RAM.")
    
    partes.append("No busques un 'liberador de RAM': suben el número de memoria libre pero la PC queda más lenta.")
    
    if ctx.startup_count > 12:
        partes.append(f"Sí te conviene mirar los {ctx.startup_count} programas de inicio: cada uno arranca con Windows.")
        
    return Answer(" ".join(partes), notice=OFFLINE_NOTICE, suggestions=["¿Conviene desactivar programas de inicio?"])

def handle_disk(ctx: SystemContext, user_query: str) -> Answer:
    """Responde preguntas sobre el uso del almacenamiento y espacio recuperable."""
    recuperable = ctx.junk_mb + ctx.duplicate_mb + ctx.browser_cache_mb
    partes = [
        f"Tenés {ctx.disk_free_percent:.0f}% libre en disco.",
        f"Podés recuperar cerca de {recuperable:.0f} MB:",
        f"{ctx.junk_mb:.0f} MB de basura, {ctx.duplicate_mb:.0f} MB de duplicados"
        f"{f' y {ctx.browser_cache_mb:.0f} MB de caché' if ctx.browser_cache_mb else ''}."
    ]
    if ctx.disk_free_percent < 10:
        partes.append("Estás por debajo del 10%, y ahí Windows empieza a andar mal. Es lo primero que atendería.")
    
    partes.append("Empezá por Limpieza: mueve los candidatos a una carpeta de revisión, no los borra.")
    
    return Answer(" ".join(partes), notice=OFFLINE_NOTICE)

def handle_security(ctx: SystemContext, user_query: str) -> Answer:
    """Responde preguntas sobre archivos sospechosos encontrados."""
    partes = []
    if ctx.suspicious_count == 0:
        partes.append("No hay archivos sospechosos en tus Descargas.")
    else:
        partes.append(f"Hay {ctx.suspicious_count} archivo(s) marcados, {ctx.suspicious_warnings} con advertencia.")
        partes.append("Son señales, no una condena: puede ser un instalador legítimo. Si no reconocés alguno, usá 'Aislar hallazgos' para mandarlo a cuarentena.")
    
    partes.append("La app nunca borra sola. La limpieza mueve todo a una carpeta de revisión, y el borrado real es un botón aparte que pide confirmación.")
    
    return Answer(" ".join(partes), notice=OFFLINE_NOTICE)

def handle_score(ctx: SystemContext, user_query: str) -> Answer:
    """Responde preguntas sobre el puntaje de salud del sistema."""
    partes = [f"Tu puntaje es {ctx.score if ctx.score is not None else 'N/A'}/100{f' (nota {ctx.grade})' if ctx.grade else ''}."]
    problemas = list(_gen_problems(ctx))
    
    if problemas:
        partes.append("Lo que más te está restando: " + ", ".join(problemas) + ".")
    else:
        partes.append("No hay nada urgente para arreglar.")
        
    partes.append("El puntaje combina basura, seguridad, memoria, disco, duplicados y programas de inicio, con la seguridad pesando más.")
    
    return Answer(" ".join(partes), notice=OFFLINE_NOTICE)

def handle_startup(ctx: SystemContext, user_query: str) -> Answer:
    """Responde preguntas sobre programas que arrancan con el sistema."""
    partes = [f"Tenés {ctx.startup_count} programas que arrancan con Windows."]
    if ctx.startup_count > 15:
        partes.append("Son bastantes, y cada uno suma tiempo de encendido. Vale la pena revisarlos.")
    elif ctx.startup_count > 8:
        partes.append("Es una cantidad normal, aunque se puede recortar.")
    else:
        partes.append("Está bien así.")
    
    partes.append("La app te los lista pero no los desactiva a propósito: hacelo desde el Administrador de tareas de Windows.")
    
    return Answer(" ".join(partes), notice=OFFLINE_NOTICE)

_HANDLERS: Final[dict[str, Callable[[SystemContext, str], Answer]]] = {
    "ram": handle_ram,
    "disco": handle_disk,
    "security": handle_security,
    "score": handle_score,
    "startup": handle_startup
}

def _sanitize_query(question: str) -> str:
    """Limpia la consulta del usuario de caracteres no permitidos."""
    return re.sub(r'[\x00-\x1f\x7f]', '', (question or "").strip())[:100].lower()

def local_answer(question: str, context: SystemContext) -> Answer:
    """
    Determina la respuesta mediante heurísticas locales basadas en keywords.
    Si no hay un área clara, ofrece un resumen de salud priorizado.
    """
    if not isinstance(context, SystemContext) or not context.analyzed:
        return Answer(
            text="Todavía no corriste ningún análisis. Andá a la pestaña Salud "
                 "y apretá 'Analizar el sistema': es de solo lectura.",
            notice=OFFLINE_NOTICE,
            suggestions=SUGGESTED_QUESTIONS_LIST[:3],
        )

    clean_text = _sanitize_query(question)
    for token in _TOKEN_REGEX.findall(clean_text):
        if token in _KEYWORD_MAP:
            return _HANDLERS[_KEYWORD_MAP[token]](context, clean_text)

    problemas = list(_gen_problems(context))
    puntaje_str = str(context.score) if context.score is not None else "N/A"
    if problemas:
        cuerpo = (f"Con un puntaje de {puntaje_str}/100, por orden de prioridad: "
                  f"{', '.join(problemas)}.")
    else:
        cuerpo = f"Tu sistema está en buen estado ({puntaje_str}/100). No hay nada urgente."
    return Answer(cuerpo, notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_LIST[:3])

def _gen_problems(ctx: SystemContext) -> Generator[str, None, None]:
    """
    Genera un listado de problemas detectados priorizados por criticidad.
    Se limita a un máximo de 3 elementos para mantener la respuesta concisa.
    """
    encontrados = 0
    for attr, limit, op, msg in _CRITERIOS_SALUD:
        val = getattr(ctx, attr, 0)
        # Evaluamos la condición lógica asegurando finitud
        if (op == "<" and val < limit) or (op == ">" and val > limit):
            yield msg.format(val)
            encontrados += 1
            if encontrados >= 3:
                break

def available(base: Union[str, Path, None] = None) -> bool:
    """Verifica si la configuración del sistema permite el uso del asistente en línea."""
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
    """Ejecuta una solicitud POST al endpoint de Gemini protegiendo los datos enviados."""
    if not isinstance(api_key, str) or not isinstance(model, str) or not api_key: return None
    if not _API_KEY_REGEX.match(api_key) or not _MODEL_NAME_REGEX.match(model): return None
    
    # Validar que los parámetros no sean rutas disfrazadas o archivos protegidos
    if not filter_safe_paths([api_key, model]): return None
    
    safe_q = _sanitize_query(question)
    if not _ensure_safe_text(safe_q) or not _ensure_safe_text(context_text): return None
    
    try:
        payload = json.dumps({
            "contents": [{
                "parts": [{"text": f"{SYSTEM_PROMPT}\n\nMétricas del sistema:\n{context_text}\n\nPregunta del usuario: {safe_q}"}]
            }]
        }).encode("utf-8")
        
        req = urllib.request.Request(
            _ENDPOINT.format(model=model) + f"?key={api_key}", 
            data=payload, 
            headers={"Content-Type": "application/json; charset=utf-8"}, 
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as res:
            if res.status != 200: return None
            raw_res = res.read(_MAX_RESPONSE_BYTES)
            if not raw_res: return None
            data = json.loads(raw_res.decode("utf-8"))
            if not isinstance(data, dict): return None
            
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list) or not candidates: return None
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
        final_text = text.strip()[:_MAX_TEXT_LENGTH]
        
        return final_text if _ensure_safe_text(final_text) else None
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

def ask(question: str, context: Optional[SystemContext] = None,
        base: Union[str, Path, None] = None) -> Answer:
    """Orquestador de alto nivel para determinar si responder localmente o via API."""
    ctx: SystemContext = context if isinstance(context, SystemContext) else SystemContext()
    respaldo: Answer = local_answer(question, ctx)
    
    if not _ensure_safe_text(question): 
        return respaldo
        
    if not available(base): return respaldo
    
    try:
        configuracion = settings.load(base)
        if not isinstance(configuracion, dict): return respaldo
        
        cfg: AssistantConfig = {
            "asistente_api_key": str(configuracion.get("asistente_api_key", "")),
            "asistente_modelo": str(configuracion.get("asistente_modelo", "gemini-3.1-flash-lite")),
            "asistente_enviar_metricas": bool(configuracion.get("asistente_enviar_metricas", True))
        }
        texto_contexto = context_as_text(ctx) if cfg["asistente_enviar_metricas"] else "El usuario no autorizó enviar métricas."
        
        remoto = _call_gemini(question, texto_contexto, cfg["asistente_api_key"], cfg["asistente_modelo"])
        if not remoto:
            respaldo.notice = "No se pudo consultar al asistente en línea, respondí con el motor local."
            return respaldo
        return Answer(remoto, source="gemini", notice=PRIVACY_NOTICE)
    except Exception:
        return respaldo
