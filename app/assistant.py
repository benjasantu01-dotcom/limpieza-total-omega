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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import settings

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

# Documentación ejecutable de lo que nunca sale del equipo. El test de
# privacidad recorre esta lista, así que agregar algo acá lo protege de verdad.
SENSITIVE_KEYS_NEVER_SENT: Final = (
    "rutas de archivos",
    "nombres de archivos",
    "contenido de archivos",
    "nombres de procesos",
    "nombre de usuario",
    "nombre del equipo",
    "números de serie",
)

PRIVACY_NOTICE: Final = (
    "El asistente en línea envía a Google solo números agregados: MB de "
    "basura, cantidad de archivos sospechosos, porcentaje de RAM y disco "
    "libres, cantidad de programas de inicio y el puntaje de salud. Nunca "
    "envía rutas, nombres ni contenido de archivos. Podés apagarlo en Ajustes."
)

OFFLINE_NOTICE: Final = (
    "Respondido por el motor local, sin conexión ni envío de datos. "
    "Para preguntas escritas con tus palabras, activá el asistente en Ajustes."
)

SUGGESTED_QUESTIONS: Final = (
    "¿Qué es lo más urgente que debería arreglar?",
    "¿Por qué mi PC está lenta?",
    "¿Es seguro borrar lo que encontró la limpieza?",
    "¿Cuánto espacio puedo recuperar?",
    "¿Qué significa mi puntaje de salud?",
    "¿Conviene desactivar programas de inicio?",
)

SYSTEM_PROMPT: Final = (
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

_ENDPOINT: Final = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TIMEOUT_SECONDS: Final = 30


@dataclass
class SystemContext:
    """Las métricas agregadas que el asistente puede ver.

    Todos los campos son números o cadenas cortas a propósito: si un campo
    pudiera contener una ruta, el motor remoto la recibiría.
    """

    score: int | None = None
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
    """Respuesta del asistente, con el origen a la vista."""

    text: str
    source: str = "local"  # "local" o "gemini"
    notice: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def is_online(self) -> bool:
        return self.source == "gemini"


def build_context(metrics: Any = None, health: Any = None, **extra: Any) -> SystemContext:
    """Arma el contexto agregado a partir de los resultados ya calculados.

    Esta es la ÚNICA función que decide qué datos son visibles para el
    asistente. Lee atributo por atributo en vez de copiar objetos enteros,
    justamente para que agregar un campo con una ruta a `SystemMetrics` no lo
    exponga por accidente.
    """
    contexto = SystemContext()

    def numero(objeto: Any, nombre: str, defecto: float = 0.0, maximo: float = float('inf')) -> float:
        """Extrae valor numérico, filtra nulos, valida tipo y asegura un máximo razonable."""
        if objeto is None: return defecto
        try:
            val = getattr(objeto, nombre, None)
            if val is None: return defecto
            num = float(val)
            return max(0.0, min(float(num), maximo))
        except (TypeError, ValueError):
            return defecto

    def entero(objeto: Any, nombre: str, defecto: int = 0) -> int:
        """Extrae entero, filtrando negativos y errores de conversión."""
        if objeto is None: return defecto
        try:
            val = getattr(objeto, nombre, None)
            if val is None: return defecto
            return max(0, int(val))
        except (TypeError, ValueError):
            return defecto

    if metrics is not None:
        contexto.junk_mb = numero(metrics, "junk_mb")
        contexto.suspicious_count = entero(metrics, "suspicious_count")
        contexto.suspicious_warnings = entero(metrics, "suspicious_warnings")
        contexto.memory_available_percent = numero(metrics, "memory_available_percent", maximo=100.0)
        contexto.disk_free_percent = numero(metrics, "disk_free_percent", maximo=100.0)
        contexto.duplicate_mb = numero(metrics, "duplicate_mb")
        contexto.startup_count = entero(metrics, "startup_count")
        contexto.quarantined_count = entero(metrics, "quarantined_count")
        contexto.analyzed = True

    if health is not None:
        score_val = entero(health, "score")
        contexto.score = max(0, min(score_val, 100))
        grado = getattr(health, "grade", "")
        contexto.grade = str(grado) if isinstance(grado, (str, int, float)) else ""
        contexto.analyzed = True

    for clave, valor in extra.items():
        if hasattr(contexto, clave) and isinstance(valor, (int, float)):
            setattr(contexto, clave, max(0.0, float(valor)))

    return contexto


def context_as_text(context: SystemContext) -> str:
    """Convierte el contexto en el texto exacto que viaja a la API.

    Se arma a mano, campo por campo, en vez de serializar el objeto entero:
    así lo que sale del equipo es auditable leyendo esta función.
    """
    if not isinstance(context, SystemContext):
        return "No hay métricas disponibles todavía."

    if not context.analyzed:
        return "El usuario todavía no corrió ningún análisis."

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
    """Explicación fija de qué mide cada área del puntaje."""
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


def local_answer(question: str, context: SystemContext) -> Answer:
    """Responde con reglas locales, sin conexión y sin enviar nada."""
    texto = (question or "").strip().lower()

    if not isinstance(context, SystemContext) or not context.analyzed:
        return Answer(
            text="Todavía no corriste ningún análisis, así que no tengo datos de "
                 "tu sistema. Andá a la pestaña Salud y apretá 'Analizar el "
                 "sistema': es de solo lectura, no modifica nada.",
            notice=OFFLINE_NOTICE,
            suggestions=list(SUGGESTED_QUESTIONS[:3]),
        )

    problemas = _rank_problems(context)
    
    # Identificación de categoría mediante mapeo para optimizar búsquedas
    cat = next((k for k in ("ram", "memoria", "lenta", "lento", "acelerar") if k in texto), None)
    if cat:
        partes = [
            f"Tenés {context.memory_available_percent:.0f}% de RAM disponible"
            f"{f' de {context.memory_total_gb:.0f} GB' if context.memory_total_gb else ''}.",
        ]
        if context.memory_available_percent < 15:
            partes.append("Eso es poco: Windows está usando el disco como memoria y "
                          "ahí se siente la lentitud. Cerrá lo que no uses; en la "
                          "pestaña Memoria tenés qué consume más.")
        else:
            partes.append("Eso está bien. Si la PC va lenta, el problema seguramente "
                          "no es la RAM.")
        partes.append("No busques un 'liberador de RAM': suben el número de memoria "
                      "libre pero la PC queda más lenta, porque Windows tiene que "
                      "releer del disco lo que acaba de descartar.")
        if context.startup_count > 12:
            partes.append(f"Sí te conviene mirar los {context.startup_count} programas "
                          "de inicio: cada uno arranca con Windows.")
        return Answer(" ".join(partes), notice=OFFLINE_NOTICE,
                      suggestions=["¿Conviene desactivar programas de inicio?"])

    if any(p in texto for p in ("espacio", "disco", "lleno", "recuperar", "liberar")):
        recuperable = context.junk_mb + context.duplicate_mb + context.browser_cache_mb
        partes = [
            f"Tenés {context.disk_free_percent:.0f}% libre en disco.",
            f"Podés recuperar cerca de {recuperable:.0f} MB: "
            f"{context.junk_mb:.0f} MB de basura, "
            f"{context.duplicate_mb:.0f} MB de duplicados"
            f"{f' y {context.browser_cache_mb:.0f} MB de caché' if context.browser_cache_mb else ''}.",
        ]
        if context.disk_free_percent < 10:
            partes.append("Estás por debajo del 10%, y ahí Windows empieza a andar "
                          "mal, no solo a quedarse sin lugar. Es lo primero que "
                          "atendería.")
        partes.append("Empezá por Limpieza: mueve los candidatos a una carpeta de "
                          "revisión, no los borra, así podés ver qué hay antes de decidir.")
        return Answer(" ".join(partes), notice=OFFLINE_NOTICE)

    if any(p in texto for p in ("seguro", "virus", "sospechos", "borrar", "peligro")):
        if context.suspicious_count == 0:
            cuerpo = ("No hay archivos sospechosos en tus Descargas. Sobre borrar: la "
                      "app nunca borra sola. La limpieza mueve todo a una carpeta de "
                      "revisión, y el borrado real es un botón aparte que pide "
                      "confirmación. Las carpetas de sistema están bloqueadas.")
        else:
            cuerpo = (f"Hay {context.suspicious_count} archivo(s) marcados, "
                      f"{context.suspicious_warnings} con advertencia. Son señales, no "
                      "una condena: puede ser un instalador legítimo. Si no reconocés "
                      "alguno, usá 'Aislar hallazgos' para mandarlo a cuarentena, que "
                      "es reversible, y corré Windows Defender para el veredicto real.")
        return Answer(cuerpo, notice=OFFLINE_NOTICE)

    if any(p in texto for p in ("puntaje", "salud", "nota", "score")):
        detalle = (f"Tu puntaje es {context.score}/100"
                   f"{f' (nota {context.grade})' if context.grade else ''}. ")
        if problemas:
            detalle += "Lo que más te está restando: " + ", ".join(problemas[:3]) + "."
        else:
            detalle += "No hay nada urgente para arreglar."
        detalle += (" El puntaje combina basura, seguridad, memoria, disco, duplicados "
                    "y programas de inicio, con la seguridad pesando más que el resto.")
        return Answer(detalle, notice=OFFLINE_NOTICE)

    if any(p in texto for p in ("inicio", "arranque", "arranca", "encender")):
        cuerpo = f"Tenés {context.startup_count} programas que arrancan con Windows. "
        if context.startup_count > 15:
            cuerpo += ("Son bastantes, y cada uno suma tiempo de encendido. Vale la "
                       "pena revisarlos. ")
        elif context.startup_count > 8:
            cuerpo += "Es una cantidad normal, aunque se puede recortar. "
        else:
            cuerpo += "Está bien así. "
        cuerpo += ("La app te los lista pero no los desactiva a propósito: hacelo desde "
                   "el Administrador de tareas de Windows, que guarda respaldo del "
                   "cambio y te deja revertirlo.")
        return Answer(cuerpo, notice=OFFLINE_NOTICE)

    # Sin coincidencia: se responde lo más útil, que es la prioridad.
    if problemas:
        cuerpo = (f"Con un puntaje de {context.score}/100, por orden de prioridad: "
                  + "; ".join(problemas[:3]) + ".")
    else:
        cuerpo = (f"Tu sistema está en buen estado ({context.score}/100). No hay nada "
                  "urgente. Un repaso de limpieza cada tanto es suficiente.")
    return Answer(cuerpo, notice=OFFLINE_NOTICE, suggestions=list(SUGGESTED_QUESTIONS[:3]))


def _rank_problems(context: SystemContext) -> list[str]:
    """Problemas detectados, del más grave al más leve."""
    problemas: list[tuple[int, str]] = []

    if context.disk_free_percent < 10:
        problemas.append((0, f"queda solo {context.disk_free_percent:.0f}% de disco libre, "
                             "atendelo primero (pestaña Disco y Limpieza)"))
    if context.suspicious_warnings > 0:
        problemas.append((1, f"{context.suspicious_warnings} archivo(s) sospechosos con "
                             "advertencia (pestaña Seguridad)"))
    if context.memory_available_percent < 15:
        problemas.append((2, f"queda {context.memory_available_percent:.0f}% de RAM "
                             "disponible (pestaña Memoria)"))
    if context.junk_mb > 1000:
        problemas.append((3, f"{context.junk_mb:.0f} MB de archivos basura "
                             "(pestaña Limpieza)"))
    if context.duplicate_mb > 500:
        problemas.append((4, f"{context.duplicate_mb:.0f} MB en duplicados "
                             "(pestaña Duplicados)"))
    if context.startup_count > 15:
        problemas.append((5, f"{context.startup_count} programas de inicio "
                             "(pestaña Inicio)"))

    problemas.sort(key=lambda par: par[0])
    return [texto for _, texto in problemas]


def available(base: str | Path | None = None) -> bool:  # noqa: F821
    """True si el motor en línea está activado y tiene clave."""
    try:
        return settings.assistant_enabled(base)
    except Exception:
        return False


def _call_gemini(question: str, context_text: str, api_key: str, model: str) -> str | None:
    """Consulta a Gemini con la librería estándar. Devuelve None si falla."""
    cuerpo = json.dumps({
        "contents": [{
            "parts": [{
                "text": f"{SYSTEM_PROMPT}\n\nMétricas del sistema:\n{context_text}\n\n"
                        f"Pregunta del usuario: {question}"
            }]
        }]
    }).encode("utf-8")

    url = _ENDPOINT.format(model=model) + f"?key={api_key}"
    peticion = urllib.request.Request(
        url, data=cuerpo, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(peticion, timeout=_TIMEOUT_SECONDS) as respuesta:
            datos = json.loads(respuesta.read().decode("utf-8"))
        partes = datos["candidates"][0]["content"]["parts"]
        texto = "".join(p.get("text", "") for p in partes).strip()
        return texto or None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def ask(question: str, context: SystemContext | None = None,
        base: str | Path | None = None) -> Answer:  # noqa: F821
    """Responde una pregunta, usando el motor en línea solo si está permitido."""
    contexto = context if isinstance(context, SystemContext) else SystemContext()
    respaldo = local_answer(question, contexto)

    if not available(base):
        return respaldo

    try:
        clave = settings.assistant_api_key(base)
        configuracion = settings.load(base)
        modelo = configuracion.get("asistente_modelo", "gemini-3.1-flash-lite")
        enviar = bool(configuracion.get("asistente_enviar_metricas", True))
    except Exception:
        return respaldo

    texto_contexto = context_as_text(contexto) if enviar else "El usuario no autorizó enviar métricas."
    remoto = _call_gemini(question, texto_contexto, clave, modelo)

    if not remoto:
        respaldo.notice = ("No se pudo consultar al asistente en línea, así que "
                           "respondí con el motor local.")
        return respaldo

    return Answer(remoto, source="gemini", notice=PRIVACY_NOTICE)
