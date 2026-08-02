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
from typing import Any, Final, TypeAlias, Callable, Optional, Union

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
    "available",
    "explain_area",
]

# Aliases de tipos para facilitar la lectura del flujo de datos
MetricSource: TypeAlias = Any
ScoreSource: TypeAlias = Any

# Documentación ejecutable de lo que nunca sale del equipo. El test de
# privacidad recorre esta lista, así que agregar algo acá lo protege de verdad.
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
_PATH_REGEX: Final[re.Pattern] = re.compile(r"([a-zA-Z]:\\|/|\\|\.\.|\0)")
_CONTROL_CHARS_REGEX: Final[re.Pattern] = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN_REGEX: Final[re.Pattern] = re.compile(r"\w+")
_MODEL_NAME_REGEX: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9\.\-_]+$")

# Mapeo optimizado mediante conjuntos de palabras clave
_KEYWORD_MAP: Final[dict[str, str]] = {
    "ram": "ram", "memoria": "ram", "lenta": "ram", "lento": "ram", "acelerar": "ram",
    "espacio": "disco", "disco": "disco", "lleno": "disco", "recuperar": "disco", "liberar": "disco",
    "seguro": "security", "virus": "security", "sospechos": "security", "borrar": "security", "peligro": "security",
    "puntaje": "score", "salud": "score", "nota": "score", "score": "score",
    "inicio": "startup", "arranque": "startup", "arranca": "startup", "encender": "startup"
}

@dataclass
class SystemContext:
    """Las métricas agregadas que el asistente puede ver."""
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
    """Respuesta del asistente, incluyendo metadatos de origen y sugerencias."""
    text: str
    source: str = "local"
    notice: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        return self.source == "gemini"

def _ensure_safe_text(text: str) -> bool:
    """Validación defensiva de texto antes de mostrarlo al usuario."""
    if _PATH_REGEX.search(text) or _CONTROL_CHARS_REGEX.search(text):
        return False
    if is_protected_path(text):
        return False
    return True

def build_context(metrics: MetricSource = None, health: ScoreSource = None, **extra: Any) -> SystemContext:
    """
    Transforma fuentes de datos crudos en un objeto SystemContext validado.
    Filtra entradas no numéricas, no finitas o fuera de rango por seguridad.
    """
    ctx = SystemContext()

    def is_valid_num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    def extract(source: Any, attr: str, default: Any, transform: Callable = float) -> Any:
        try:
            val = getattr(source, attr, None)
            return transform(val) if is_valid_num(val) else default
        except (AttributeError, ValueError, TypeError):
            return default

    if metrics is not None:
        ctx.junk_mb = max(0.0, extract(metrics, "junk_mb", 0.0))
        ctx.suspicious_count = max(0, extract(metrics, "suspicious_count", 0, int))
        ctx.suspicious_warnings = max(0, extract(metrics, "suspicious_warnings", 0, int))
        ctx.memory_available_percent = max(0.0, min(extract(metrics, "memory_available_percent", 0.0), 100.0))
        ctx.disk_free_percent = max(0.0, min(extract(metrics, "disk_free_percent", 0.0), 100.0))
        ctx.duplicate_mb = max(0.0, extract(metrics, "duplicate_mb", 0.0))
        ctx.startup_count = max(0, extract(metrics, "startup_count", 0, int))
        ctx.quarantined_count = max(0, extract(metrics, "quarantined_count", 0, int))
        ctx.analyzed = True

    if health is not None:
        score_raw = extract(health, "score", 0, int)
        ctx.score = max(0, min(score_raw, 100))
        grade = getattr(health, "grade", "")
        ctx.grade = str(grade) if isinstance(grade, (str, int, float)) else ""
        ctx.analyzed = True

    for k, v in extra.items():
        if hasattr(ctx, k) and k not in ["analyzed", "grade"] and is_valid_num(v):
            setattr(ctx, k, float(v))

    return ctx


def context_as_text(context: SystemContext) -> str:
    """Serializa el estado del sistema en un formato de texto compacto para prompts."""
    if not isinstance(context, SystemContext) or not context.analyzed:
        return "No hay métricas disponibles todavía."

    lineas = [
        f"Puntaje de salud: {context.score if context.score is not None else 'sin calcular'}"
        f"{f' (nota {context.grade})' if context.grade else ''}",
        f"Archivos basura: {context.junk_mb:.0f} MB",
        f"Archivos sospechosos: {context.suspicious_count} "
        f"({context.suspicious_warnings} con advertencia)",
        f"RAM disponible: {context.memory_available_percent:.0f}%",
        f"Espacio libre en disco: {context.disk_free_percent:.0f}%",
        f"Duplicados recuperables: {context.duplicate_mb:.0f} MB",
        f"Programas de inicio: {context.startup_count}",
        f"Archivos en cuarentena: {context.quarantined_count}",
    ]
    if context.browser_cache_mb:
        lineas.append(f"Caché de navegadores: {context.browser_cache_mb:.0f} MB")
    return "\n".join(lineas)


def explain_area(area: str) -> str:
    """Proporciona una breve explicación pedagógica de un área específica."""
    explicaciones = {
        "basura": "Archivos temporales y restos de instaladores. Ocupan espacio "
                  "sin dar nada a cambio, y son lo más seguro de limpiar.",
        "seguridad": "Señales sospechosas en tus Descargas: doble extensión, "
                     "ejecutables recién bajados, nombres que imitan al sistema. "
                     "Son señales, no una condena.",
        "memoria": "Cuánta RAM queda disponible. Tener poca hace que Windows use "
                   "el disco como memoria, y ahí se siente la lentitud. Ojo: RAM "
                   "ocupada como caché es buena, no es un problema.",
        "disco": "Espacio libre en la unidad del sistema. Por debajo del 10% "
                 "Windows empieza a andar mal, no solo a quedarse sin lugar.",
        "duplicados": "Copias idénticas del mismo archivo. Espacio recuperable "
                      "sin perder nada, porque siempre se conserva una.",
        "inicio": "Programas que arrancan con Windows. Cada uno suma tiempo de "
                  "encendido y consume memoria desde el minuto cero.",
    }
    if isinstance(area, str):
        return explicaciones.get(area.strip().lower(), "No tengo una explicación para esa área.")
    return "No tengo una explicación para esa área."


def handle_ram(ctx: SystemContext, text: str) -> Answer:
    """Procesa consultas sobre el rendimiento de la memoria RAM."""
    partes = [
        f"Tenés {ctx.memory_available_percent:.0f}% de RAM disponible"
        f"{f' de {ctx.memory_total_gb:.0f} GB' if ctx.memory_total_gb else ''}.",
    ]
    if ctx.memory_available_percent < 15:
        partes.append("Eso es poco: Windows está usando el disco como memoria y "
                        "ahí se siente la lentitud. Cerrá lo que no uses; en la "
                        "pestaña Memoria tenés qué consume más.")
    else:
        partes.append("Eso está bien. Si la PC va lenta, el problema seguramente "
                        "no es la RAM.")
    partes.append("No busques un 'liberador de RAM': suben el número de memoria "
                    "libre pero la PC queda más lenta, porque Windows tiene que "
                    "releer del disco lo que acaba de descartar.")
    if ctx.startup_count > 12:
        partes.append(f"Sí te conviene mirar los {ctx.startup_count} programas "
                        "de inicio: cada uno arranca con Windows.")
    return Answer(" ".join(partes), notice=OFFLINE_NOTICE,
                    suggestions=["¿Conviene desactivar programas de inicio?"])

def handle_disk(ctx: SystemContext, text: str) -> Answer:
    """Procesa consultas sobre almacenamiento y limpieza de disco."""
    recuperable = ctx.junk_mb + ctx.duplicate_mb + ctx.browser_cache_mb
    
    mensaje = (
        f"Tenés {ctx.disk_free_percent:.0f}% libre en disco. "
        f"Podés recuperar cerca de {recuperable:.0f} MB: "
        f"{ctx.junk_mb:.0f} MB de basura, "
        f"{ctx.duplicate_mb:.0f} MB de duplicados"
        f"{f' y {ctx.browser_cache_mb:.0f} MB de caché' if ctx.browser_cache_mb else ''}."
    )
    
    advertencia = ""
    if ctx.disk_free_percent < 10:
        advertencia = " Estás por debajo del 10%, y ahí Windows empieza a andar mal. Es lo primero que atendería."
        
    sugerencia = " Empezá por Limpieza: mueve los candidatos a una carpeta de revisión, no los borra, así podés ver qué hay antes de decidir."
    
    return Answer(mensaje + advertencia + sugerencia, notice=OFFLINE_NOTICE)

def handle_security(ctx: SystemContext, text: str) -> Answer:
    """Procesa consultas sobre seguridad de archivos sospechosos."""
    if ctx.suspicious_count == 0:
        cuerpo = ("No hay archivos sospechosos en tus Descargas. Sobre borrar: la "
                    "app nunca borra sola. La limpieza mueve todo a una carpeta de "
                    "revisión, y el borrado real es un botón aparte que pide "
                    "confirmación. Las carpetas de sistema están bloqueadas.")
    else:
        cuerpo = (f"Hay {ctx.suspicious_count} archivo(s) marcados, "
                    f"{ctx.suspicious_warnings} con advertencia. Son señales, no "
                    "una condena: puede ser un instalador legítimo. Si no reconocés "
                    "alguno, usá 'Aislar hallazgos' para mandarlo a cuarentena, que "
                    "es reversible, y corré Windows Defender para el veredicto real.")
    return Answer(cuerpo, notice=OFFLINE_NOTICE)

def handle_score(ctx: SystemContext, text: str) -> Answer:
    """Procesa consultas sobre la explicación del puntaje de salud global."""
    detalle = (f"Tu puntaje es {ctx.score}/100"
                f"{f' (nota {ctx.grade})' if ctx.grade else ''}. ")
    problemas = _rank_problems(ctx)
    if problemas:
        detalle += "Lo que más te está restando: " + ", ".join(problemas[:3]) + "."
    else:
        detalle += "No hay nada urgente para arreglar."
    detalle += (" El puntaje combina basura, seguridad, memoria, disco, duplicados "
                "y programas de inicio, con la seguridad pesando más que el resto.")
    return Answer(detalle, notice=OFFLINE_NOTICE)

def handle_startup(ctx: SystemContext, text: str) -> Answer:
    """Procesa consultas sobre programas que inician con el sistema."""
    cuerpo = f"Tenés {ctx.startup_count} programas que arrancan con Windows. "
    if ctx.startup_count > 15:
        cuerpo += ("Son bastantes, y cada uno suma tiempo de encendido. Vale la "
                    "pena revisarlos. ")
    elif ctx.startup_count > 8:
        cuerpo += "Es una cantidad normal, aunque se puede recortar. "
    else:
        cuerpo += "Está bien así. "
    cuerpo += ("La app te los lista pero no los desactiva a propósito: hacelo desde "
                "el Administrador de tareas de Windows, que guarda respaldo del "
                "cambio y te deja revertirlo.")
    return Answer(cuerpo, notice=OFFLINE_NOTICE)

_HANDLERS: Final[dict[str, Callable[[SystemContext, str], Answer]]] = {
    "ram": handle_ram,
    "disco": handle_disk,
    "security": handle_security,
    "score": handle_score,
    "startup": handle_startup
}

def _sanitize_query(question: str) -> str:
    """Limpia la entrada del usuario para procesamiento de texto seguro."""
    return re.sub(r'[\x00-\x1f\x7f]', '', (question or "").strip())[:200].lower()

def local_answer(question: str, context: SystemContext) -> Answer:
    """
    Procesa la pregunta del usuario utilizando reglas de negocio estáticas.
    """
    if not isinstance(context, SystemContext) or not context.analyzed:
        return Answer(
            text="Todavía no corriste ningún análisis, así que no tengo datos de "
                 "tu sistema. Andá a la pestaña Salud y apretá 'Analizar el "
                 "sistema': es de solo lectura, no modifica nada.",
            notice=OFFLINE_NOTICE,
            suggestions=SUGGESTED_QUESTIONS_LIST[:3],
        )

    clean_text = _sanitize_query(question)
    tokens = _TOKEN_REGEX.findall(clean_text)
    for token in tokens:
        if handler_key := _KEYWORD_MAP.get(token):
            return _HANDLERS[handler_key](context, clean_text)

    problemas = _rank_problems(context)
    if problemas:
        cuerpo = (f"Con un puntaje de {context.score}/100, por orden de prioridad: "
                  + "; ".join(problemas[:3]) + ".")
    else:
        cuerpo = (f"Tu sistema está en buen estado ({context.score}/100). No hay nada "
                  "urgente. Un repaso de limpieza cada tanto es suficiente.")
    return Answer(cuerpo, notice=OFFLINE_NOTICE, suggestions=SUGGESTED_QUESTIONS_LIST[:3])


def _rank_problems(context: SystemContext) -> list[str]:
    """Calcula y ordena los problemas más críticos del sistema."""
    probs = []
    
    if context.disk_free_percent < 10:
        probs.append(f"queda solo {context.disk_free_percent:.0f}% de disco libre, atendelo primero (pestaña Disco y Limpieza)")
    
    if context.suspicious_warnings > 0:
        probs.append(f"{context.suspicious_warnings} archivo(s) sospechosos con advertencia (pestaña Seguridad)")
        
    if context.memory_available_percent < 15:
        probs.append(f"queda {context.memory_available_percent:.0f}% de RAM disponible (pestaña Memoria)")
        
    if context.junk_mb > 1000:
        probs.append(f"{context.junk_mb:.0f} MB de archivos basura (pestaña Limpieza)")
        
    if context.duplicate_mb > 500:
        probs.append(f"{context.duplicate_mb:.0f} MB en duplicados (pestaña Duplicados)")
        
    if context.startup_count > 15:
        probs.append(f"{context.startup_count} programas de inicio (pestaña Inicio)")
        
    return probs


def available(base: str | Path | None = None) -> bool:
    """Verifica si el asistente en línea está configurado y habilitado."""
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
    """
    Envía métricas agregadas a Gemini mediante la librería estándar urllib.
    Realiza una serialización JSON del contexto y la pregunta, validando la
    integridad de la respuesta recibida contra caracteres no seguros.
    """
    if not api_key or not model or not _MODEL_NAME_REGEX.match(model):
        return None
    
    # Seguridad: truncar y validar entradas antes del envío
    safe_q: str = _sanitize_query(question)[:500]
    safe_ctx: str = context_text[:1000]
    
    if not _ensure_safe_text(safe_q):
        return None
        
    try:
        sanitized_context: str = _CONTROL_CHARS_REGEX.sub("", safe_ctx)
        
        cuerpo_json: bytes = json.dumps({
            "contents": [{
                "parts": [{
                    "text": f"{SYSTEM_PROMPT}\n\nMétricas del sistema:\n{sanitized_context}\n\n"
                            f"Pregunta del usuario: {safe_q}"
                }]
            }]
        }).encode("utf-8")

        url: str = _ENDPOINT.format(model=model) + f"?key={api_key}"
        peticion: urllib.request.Request = urllib.request.Request(
            url, 
            data=cuerpo_json, 
            headers={"Content-Type": "application/json"}, 
            method="POST"
        )
        
        with urllib.request.urlopen(peticion, timeout=_TIMEOUT_SECONDS) as respuesta:
            if respuesta.status != 200:
                return None
            datos: Any = json.loads(respuesta.read().decode("utf-8"))
        
        if not isinstance(datos, dict):
            return None

        candidatos: Any = datos.get("candidates", [])
        if not isinstance(candidatos, list) or not candidatos or not isinstance(candidatos[0], dict):
            return None
            
        partes: Any = candidatos[0].get("content", {}).get("parts", [])
        if not isinstance(partes, list):
            return None
            
        texto: str = "".join(p.get("text", "") for p in partes if isinstance(p, dict)).strip()
        
        # Validar seguridad: ni caracteres de control, ni rutas, ni protección violada
        if not texto or len(texto) > 1200 or not _ensure_safe_text(texto):
            return None
            
        return texto
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def ask(question: str, context: SystemContext | None = None,
        base: str | Path | None = None) -> Answer:
    """
    Coordina la resolución de la consulta: intenta una respuesta local basada
    en reglas estáticas y, si el asistente en línea está habilitado en
    settings, intenta obtener una respuesta contextual mediante _call_gemini.
    """
    ctx: SystemContext = context if isinstance(context, SystemContext) else SystemContext()
    respaldo: Answer = local_answer(question, ctx)

    if not available(base):
        return respaldo

    try:
        configuracion: Any = settings.load(base)
        if not isinstance(configuracion, dict):
            return respaldo
            
        clave: str = str(configuracion.get("asistente_api_key", ""))
        modelo: str = str(configuracion.get("asistente_modelo", "gemini-3.1-flash-lite"))
        enviar: bool = bool(configuracion.get("asistente_enviar_metricas", True))
        
        texto_contexto: str = context_as_text(ctx) if enviar else "El usuario no autorizó enviar métricas."
        remoto: Optional[str] = _call_gemini(question, texto_contexto, clave, modelo)

        if not remoto:
            respaldo.notice = ("No se pudo consultar al asistente en línea, así que "
                               "respondí con el motor local.")
            return respaldo

        return Answer(remoto, source="gemini", notice=PRIVACY_NOTICE)
    except Exception:
        return respaldo
