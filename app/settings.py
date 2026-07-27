"""
settings.py — preferencias del usuario, guardadas entre sesiones.

Guarda un JSON chico en la carpeta del usuario. Todo valor se valida al
cargar: un archivo editado a mano, corrupto o de una versión vieja nunca debe
dejar la app sin arrancar, así que cualquier valor inválido se reemplaza
silenciosamente por el de fábrica.

DECISIONES QUE IMPORTAN
-----------------------
1. **El asistente viene apagado.** Encenderlo implica mandar datos a Google,
   y eso lo decide el usuario, no el valor por defecto.

2. **La clave de API se prefiere desde el entorno.** Una clave en un JSON de
   texto plano queda expuesta a cualquier programa que lea la carpeta del
   usuario, y se filtra si el archivo termina en un respaldo en la nube.
   `assistant_api_key()` mira primero la variable de entorno; guardarla en el
   archivo es una opción, no el camino recomendado.

3. **Nada de rutas de sistema.** Las carpetas configurables pasan por
   `safety.is_safe_to_modify` antes de aceptarse, así una preferencia mal
   puesta no puede convertirse en un borrado en `C:\\Windows`.

4. **Este módulo no sabe nada de la interfaz.** Devuelve datos, no widgets,
   para que se pueda testear sin pantalla.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

from safety import is_safe_to_modify

__all__ = [
    "DEFAULTS",
    "SETTINGS_DIR",
    "SETTINGS_FILE",
    "API_KEY_ENV_VAR",
    "VALID_THEMES",
    "VALID_ACCENTS",
    "settings_path",
    "load",
    "save",
    "update",
    "reset",
    "validate",
    "get",
    "assistant_api_key",
    "assistant_enabled",
    "describe",
]

SETTINGS_DIR: Final = "~/LimpiezaTotalOmega"
SETTINGS_FILE: Final = "config.json"

# Variable de entorno preferida para la clave del asistente.
API_KEY_ENV_VAR: Final = "OMEGA_GEMINI_KEY"

VALID_THEMES: Final = ("oscuro", "claro", "sistema")
VALID_ACCENTS: Final = ("menta", "violeta", "magenta", "cian", "ambar")

# Valores de fábrica. Cada clave define además el tipo esperado: si el archivo
# trae otra cosa, se descarta ese valor y se usa este.
DEFAULTS: Final[dict[str, Any]] = {
    # Apariencia
    "tema": "oscuro",
    "acento": "menta",
    "mostrar_barras": True,
    "animaciones": True,
    # Comportamiento
    "confirmar_siempre": True,
    "abrir_en": "Salud",
    "recordar_ultima_carpeta": True,
    "ultima_carpeta": "",
    # Límites de análisis (rendimiento)
    "duplicados_tamano_minimo_kb": 64,
    "top_archivos": 15,
    "top_procesos": 15,
    "analisis_en_paralelo": True,
    # Asistente IA
    "asistente_activado": False,
    "asistente_clave_api": "",
    "asistente_enviar_metricas": True,
    "asistente_modelo": "gemini-3.1-flash-lite",
}

# Límites de los valores numéricos: (mínimo, máximo).
_NUMERIC_LIMITS: Final = {
    "duplicados_tamano_minimo_kb": (0, 1024 * 1024),
    "top_archivos": (1, 500),
    "top_procesos": (1, 500),
}


def settings_path(base: str | Path | None = None) -> Path:
    """Ruta del archivo de configuración.

    Acepta una base para poder testear sin tocar la carpeta real del usuario.
    """
    if base is not None:
        carpeta = Path(base)
    else:
        carpeta = Path(SETTINGS_DIR).expanduser()
    return carpeta / SETTINGS_FILE


def validate(values: Any) -> dict[str, Any]:
    """Devuelve una configuración completa y sana a partir de datos crudos.

    Nunca lanza excepción y nunca devuelve claves de más: lo que no se
    reconoce se descarta, lo que está mal se reemplaza por el valor de fábrica.
    Es lo que permite que un config.json editado a mano no rompa la app.
    """
    limpio = dict(DEFAULTS)
    if not isinstance(values, dict):
        return limpio

    for clave, defecto in DEFAULTS.items():
        if clave not in values:
            continue
        valor = values[clave]

        # Los booleanos aceptan también las cadenas típicas de un JSON a mano.
        if isinstance(defecto, bool):
            if isinstance(valor, bool):
                limpio[clave] = valor
            elif isinstance(valor, str):
                limpio[clave] = valor.strip().lower() in ("1", "true", "si", "sí", "yes")
            continue

        if isinstance(defecto, int) and not isinstance(valor, bool):
            # Solo intentamos convertir si el valor no es un dict/lista.
            if isinstance(valor, (int, float, str)):
                try:
                    numero = int(valor)
                except (TypeError, ValueError):
                    continue
                minimo, maximo = _NUMERIC_LIMITS.get(clave, (0, 10 ** 9))
                limpio[clave] = max(minimo, min(maximo, numero))
            continue

        if isinstance(defecto, str):
            if not isinstance(valor, str):
                continue
            texto = valor.strip()
            if clave == "tema" and texto.lower() not in VALID_THEMES:
                continue
            if clave == "acento" and texto.lower() not in VALID_ACCENTS:
                continue
            if clave == "ultima_carpeta" and texto and not is_safe_to_modify(texto):
                # Una carpeta protegida no se recuerda: si el usuario la
                # eligió por error, no queremos ofrecérsela de nuevo.
                continue
            limpio[clave] = texto.lower() if clave in ("tema", "acento") else texto

    return limpio


def load(base: str | Path | None = None) -> dict[str, Any]:
    """Carga la configuración. Devuelve los valores de fábrica si no hay archivo."""
    ruta = settings_path(base)
    try:
        crudo = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return dict(DEFAULTS)
    return validate(crudo)


def save(values: Any, base: str | Path | None = None) -> Path | None:
    """Guarda la configuración validada. Devuelve la ruta, o None si no pudo."""
    ruta = settings_path(base)
    limpio = validate(values)
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(
            json.dumps(limpio, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return ruta
    except OSError:
        return None


def update(changes: dict[str, Any], base: str | Path | None = None) -> dict[str, Any]:
    """Aplica cambios parciales sobre lo guardado y devuelve el resultado."""
    actual = load(base)
    if isinstance(changes, dict):
        actual.update(changes)
    limpio = validate(actual)
    save(limpio, base)
    return limpio


def reset(base: str | Path | None = None) -> dict[str, Any]:
    """Vuelve todo a los valores de fábrica y lo guarda."""
    limpio = dict(DEFAULTS)
    save(limpio, base)
    return limpio


def get(key: str, base: str | Path | None = None) -> Any:
    """Lee un solo valor, con su valor de fábrica como respaldo."""
    return load(base).get(key, DEFAULTS.get(key))


def assistant_api_key(base: str | Path | None = None) -> str:
    """Clave del asistente: primero el entorno, después el archivo.

    Se prefiere la variable de entorno porque una clave en un JSON de texto
    plano queda legible para cualquier programa del usuario y se filtra si la
    carpeta termina sincronizada en la nube.
    """
    desde_entorno = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if desde_entorno:
        return desde_entorno
    valor = load(base).get("asistente_clave_api", "")
    return valor.strip() if isinstance(valor, str) else ""


def assistant_enabled(base: str | Path | None = None) -> bool:
    """True solo si el usuario lo activó Y hay una clave disponible.

    Las dos condiciones son necesarias: activado sin clave no puede consultar,
    y una clave presente sin activar no autoriza a mandar nada.
    """
    return bool(load(base).get("asistente_activado")) and bool(assistant_api_key(base))


def describe(base: str | Path | None = None) -> list[str]:
    """Resumen legible de la configuración, para mostrar en la app."""
    actual = load(base)
    clave = assistant_api_key(base)
    origen_clave = (
        f"variable de entorno {API_KEY_ENV_VAR}"
        if os.environ.get(API_KEY_ENV_VAR, "").strip()
        else ("archivo de configuración" if clave else "no configurada")
    )

    return [
        "Configuración actual",
        "",
        f"  Archivo:                {settings_path(base)}",
        "",
        "  Apariencia",
        f"    Tema:                 {actual['tema']}",
        f"    Acento:               {actual['acento']}",
        f"    Barras visuales:      {'sí' if actual['mostrar_barras'] else 'no'}",
        "",
        "  Comportamiento",
        f"    Confirmar siempre:    {'sí' if actual['confirmar_siempre'] else 'no'}",
        f"    Pestaña inicial:      {actual['abrir_en']}",
        f"    Recordar carpeta:     {'sí' if actual['recordar_ultima_carpeta'] else 'no'}",
        "",
        "  Rendimiento",
        f"    Duplicados desde:     {actual['duplicados_tamano_minimo_kb']} KB",
        f"    Top de archivos:      {actual['top_archivos']}",
        f"    Análisis en paralelo: {'sí' if actual['analisis_en_paralelo'] else 'no'}",
        "",
        "  Asistente IA",
        f"    Activado:             {'sí' if actual['asistente_activado'] else 'no'}",
        f"    Clave:                {origen_clave}",
        f"    Modelo:               {actual['asistente_modelo']}",
        "",
        "  El asistente viene apagado a propósito: encenderlo manda métricas",
        "  agregadas a Google. Nunca se envían rutas ni contenido de archivos.",
    ]
