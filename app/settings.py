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
import tempfile
from pathlib import Path
from typing import Any, Final

from safety import is_safe_to_modify, ensure_safe_to_modify

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

# Caché interno para evitar lectura repetitiva de disco
_cached_settings: dict[str, Any] | None = None
_last_base: str | Path | None = None
_last_mtime: float = 0.0

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


def _coerce_bool(valor: Any) -> bool | None:
    """Intenta convertir un valor a booleano; acepta strings representativos."""
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        return valor.strip().lower() in ("1", "true", "si", "sí", "yes")
    return None


def _coerce_int(valor: Any, clave: str) -> int | None:
    """
    Convierte a entero y asegura que el resultado esté dentro del rango permitido
    por _NUMERIC_LIMITS, evitando desbordamientos o configuraciones absurdas.
    """
    try:
        numero = int(valor)
        minimo, maximo = _NUMERIC_LIMITS.get(clave, (0, 10**9))
        return max(minimo, min(maximo, numero))
    except (TypeError, ValueError):
        return None


def _validate_str(clave: str, valor: Any) -> str | None:
    """
    Valida cadenas. Si la clave requiere validación de seguridad (ej. rutas)
    o de enum (ej. temas), verifica contra las reglas del proyecto antes de retornar.
    """
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    if clave == "tema" and texto.lower() not in VALID_THEMES:
        return None
    if clave == "acento" and texto.lower() not in VALID_ACCENTS:
        return None
    if clave == "ultima_carpeta" and texto:
        try:
            ruta_candidata = Path(texto).expanduser().resolve()
            if not is_safe_to_modify(str(ruta_candidata)):
                return None
            return str(ruta_candidata)
        except (OSError, RuntimeError):
            return None
    return texto.lower() if clave in ("tema", "acento") else texto


def _apply_validation_by_type(clave: str, valor: Any, defecto: Any) -> Any:
    """
    Función dispatch que determina la estrategia de validación según el tipo
    del valor por defecto esperado. Es el núcleo de la resiliencia ante datos corruptos.
    """
    if valor is None:
        return None
    if isinstance(defecto, bool):
        return _coerce_bool(valor)
    if isinstance(defecto, int) and not isinstance(valor, bool):
        return _coerce_int(valor, clave)
    if isinstance(defecto, str):
        return _validate_str(clave, valor)
    return None


def settings_path(base: str | Path | None = None) -> Path:
    """Resuelve la ubicación final del archivo de configuración (soporta rutas relativas/home)."""
    if base is not None:
        carpeta = Path(base)
    else:
        carpeta = Path(SETTINGS_DIR).expanduser()
    return carpeta / SETTINGS_FILE


def validate(values: Any) -> dict[str, Any]:
    """
    Crea un diccionario nuevo basado en DEFAULTS, poblándolo únicamente con
    valores válidos encontrados en la entrada `values`. Garantiza que el retorno
    sea siempre un objeto completo y seguro.
    """
    limpio = dict(DEFAULTS)
    if not isinstance(values, dict):
        return limpio

    for clave, defecto in DEFAULTS.items():
        if clave in values:
            coerced = _apply_validation_by_type(clave, values[clave], defecto)
            if coerced is not None:
                limpio[clave] = coerced

    return limpio


def load(base: str | Path | None = None) -> dict[str, Any]:
    """Carga configuración desde disco con caché inteligente o retorna defaults ante error."""
    global _cached_settings, _last_base, _last_mtime
    
    ruta = settings_path(base)
    try:
        if not ruta.exists():
            return dict(DEFAULTS)
        
        stat = ruta.stat()
        if _cached_settings is not None and base == _last_base and stat.st_mtime == _last_mtime:
            return _cached_settings

        contenido = ruta.read_text(encoding="utf-8")
        data = json.loads(contenido)
        _cached_settings = validate(data)
        _last_base = base
        _last_mtime = stat.st_mtime
        return _cached_settings
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return dict(DEFAULTS)


def save(values: Any, base: str | Path | None = None) -> Path | None:
    """
    Persiste la configuración de forma atómica usando un archivo temporal.
    Verifica seguridad de escritura antes de realizar cualquier operación.
    """
    global _cached_settings, _last_mtime
    ruta = settings_path(base)
    temp_name = None
    
    try:
        ensure_safe_to_modify(str(ruta.parent))
        
        limpio = validate(values)
        json_data = json.dumps(limpio, indent=2, ensure_ascii=False)
        
        ruta.parent.mkdir(parents=True, exist_ok=True)
        
        with tempfile.NamedTemporaryFile("w", dir=ruta.parent, delete=False, encoding="utf-8") as tf:
            temp_name = tf.name
            tf.write(json_data)
        
        os.replace(temp_name, ruta)
        
        _cached_settings = limpio
        _last_mtime = ruta.stat().st_mtime
        return ruta
    except (OSError, RuntimeError, PermissionError):
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError:
                pass
        return None


def update(changes: dict[str, Any], base: str | Path | None = None) -> dict[str, Any]:
    """Aplica cambios parciales y guarda el nuevo estado completo."""
    actual = load(base).copy()
    if isinstance(changes, dict):
        actual.update(changes)
    limpio = validate(actual)
    save(limpio, base)
    return limpio


def reset(base: str | Path | None = None) -> dict[str, Any]:
    """Sobrescribe el archivo de configuración con los valores por defecto."""
    global _cached_settings
    limpio = dict(DEFAULTS)
    save(limpio, base)
    _cached_settings = limpio
    return limpio


def get(key: str, base: str | Path | None = None) -> Any:
    """Recupera el valor de una clave específica desde la configuración cargada."""
    return load(base).get(key, DEFAULTS.get(key))


def assistant_api_key(base: str | Path | None = None) -> str:
    """Obtiene la API key (prioridad: variable de entorno -> archivo de configuración)."""
    desde_entorno = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if desde_entorno:
        return desde_entorno
    config = load(base)
    valor = config.get("asistente_clave_api", "")
    return valor.strip() if isinstance(valor, str) else ""


def assistant_enabled(base: str | Path | None = None) -> bool:
    """Valida si el asistente puede operar (debe estar activado y poseer una key válida)."""
    config = load(base)
    return bool(config.get("asistente_activado")) and bool(assistant_api_key(base))


def describe(base: str | Path | None = None) -> list[str]:
    """Genera una vista humana de la configuración para propósitos de reporte/debug."""
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
