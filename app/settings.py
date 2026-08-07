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
from typing import Any, Final, TypeAlias, Callable, TypedDict

from safety import is_safe_to_modify, ensure_safe_to_modify

PathLike: TypeAlias = str | Path

class AppSettings(TypedDict):
    """Estructura esperada de la configuración de la aplicación."""
    tema: str
    acento: str
    mostrar_barras: bool
    animaciones: bool
    confirmar_siempre: bool
    abrir_en: str
    recordar_ultima_carpeta: bool
    ultima_carpeta: str
    duplicados_tamano_minimo_kb: int
    top_archivos: int
    top_procesos: int
    analisis_en_paralelo: bool
    asistente_activado: bool
    asistente_clave_api: str
    asistente_enviar_metricas: bool
    asistente_modelo: str

__all__ = [
    "DEFAULTS", "SETTINGS_DIR", "SETTINGS_FILE", "API_KEY_ENV_VAR",
    "VALID_THEMES", "VALID_ACCENTS", "settings_path", "load", "save",
    "update", "reset", "validate", "get", "assistant_api_key",
    "assistant_enabled", "describe",
]

SETTINGS_DIR: Final = Path("~/LimpiezaTotalOmega").expanduser()
SETTINGS_FILE: Final = "config.json"
MAX_SETTINGS_SIZE: Final = 1024 * 64
API_KEY_ENV_VAR: Final = "OMEGA_GEMINI_KEY"

VALID_THEMES: Final = ("oscuro", "claro", "sistema")
VALID_ACCENTS: Final = ("menta", "violeta", "magenta", "cian", "ambar")

_cached_settings: AppSettings | None = None
_current_path: Path | None = None
_path_cache: dict[str, Path] = {}

DEFAULTS: Final[AppSettings] = {
    "tema": "oscuro",
    "acento": "menta",
    "mostrar_barras": True,
    "animaciones": True,
    "confirmar_siempre": True,
    "abrir_en": "Salud",
    "recordar_ultima_carpeta": True,
    "ultima_carpeta": "",
    "duplicados_tamano_minimo_kb": 64,
    "top_archivos": 15,
    "top_procesos": 15,
    "analisis_en_paralelo": True,
    "asistente_activado": False,
    "asistente_clave_api": "",
    "asistente_enviar_metricas": True,
    "asistente_modelo": "gemini-3.1-flash-lite",
}

_NUMERIC_LIMITS: Final[dict[str, tuple[int, int]]] = {
    "duplicados_tamano_minimo_kb": (0, 1024 * 1024),
    "top_archivos": (1, 500),
    "top_procesos": (1, 500),
}

class _Validators:
    """Namespace para funciones de validación de datos de entrada."""
    @staticmethod
    def bool(key: str, val: Any) -> bool | None:
        if isinstance(val, bool): return val
        if isinstance(val, str) and val.strip().lower() in ("1", "true", "si", "sí", "yes"): return True
        if isinstance(val, str) and val.strip().lower() in ("0", "false", "no", "none"): return False
        return None

    @staticmethod
    def int(key: str, val: Any) -> int | None:
        if val is None or isinstance(val, bool): return None
        try:
            parsed = int(val)
            low, high = _NUMERIC_LIMITS.get(key, (0, 10**9))
            return max(low, min(high, parsed))
        except (TypeError, ValueError): return None

    @staticmethod
    def path(val: Any) -> str | None:
        if not val: return ""
        try:
            path = Path(str(val)).expanduser().resolve()
            return str(path) if is_safe_to_modify(str(path)) else None
        except (OSError, RuntimeError, ValueError, TypeError, PermissionError):
            return None

    @staticmethod
    def str(key: str, val: Any) -> str | None:
        if not isinstance(val, (str, Path)): return None
        text = str(val).strip()
        if not text: return "" if key in ("ultima_carpeta", "asistente_clave_api") else None
        text_lower = text.lower()
        if key == "tema": return text_lower if text_lower in VALID_THEMES else None
        if key == "acento": return text_lower if text_lower in VALID_ACCENTS else None
        if key == "ultima_carpeta": return _Validators.path(text)
        return text if len(text) <= 256 else None

_VALIDATOR_MAP: Final[dict[str, Callable[[str, Any], Any]]] = {
    "tema": _Validators.str, "acento": _Validators.str, "ultima_carpeta": _Validators.str, 
    "asistente_clave_api": _Validators.str, "asistente_modelo": _Validators.str, "abrir_en": _Validators.str,
    "mostrar_barras": _Validators.bool, "animaciones": _Validators.bool, "confirmar_siempre": _Validators.bool, 
    "recordar_ultima_carpeta": _Validators.bool, "analisis_en_paralelo": _Validators.bool, 
    "asistente_activado": _Validators.bool, "asistente_enviar_metricas": _Validators.bool,
    "duplicados_tamano_minimo_kb": _Validators.int, "top_archivos": _Validators.int, "top_procesos": _Validators.int
}

def settings_path(path_or_base: PathLike | None = None) -> Path:
    """Calcula la ruta absoluta al archivo config.json, validando el directorio padre."""
    key = str(path_or_base or SETTINGS_DIR)
    if key in _path_cache: return _path_cache[key]
    try:
        candidate = Path(key).expanduser().resolve()
        ensure_safe_to_modify(candidate)
        res = candidate / SETTINGS_FILE
    except (OSError, RuntimeError, ValueError, PermissionError):
        res = SETTINGS_DIR.expanduser().resolve() / SETTINGS_FILE
    _path_cache[key] = res
    return res

def validate(values: Any) -> AppSettings:
    """Limpia y valida un diccionario de entrada contra el esquema DEFAULTS."""
    config = DEFAULTS.copy()
    if not isinstance(values, dict): return config
    for clave, validador in _VALIDATOR_MAP.items():
        if clave in values:
            resultado = validador(clave, values[clave])
            if resultado is not None: config[clave] = resultado
    return config

def load(path_or_base: PathLike | None = None) -> AppSettings:
    """Lee el archivo desde disco, valida el contenido y gestiona el caché en memoria."""
    global _cached_settings, _current_path
    ruta = settings_path(path_or_base)
    if _cached_settings is not None and _current_path == ruta:
        return _cached_settings.copy()
    
    try:
        if ruta.exists() and 0 < ruta.stat().st_size <= MAX_SETTINGS_SIZE:
            data = json.loads(ruta.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _cached_settings = validate(data)
                _current_path = ruta
                return _cached_settings.copy()
        raise ValueError
    except (OSError, PermissionError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        _cached_settings = DEFAULTS.copy()
        _current_path = ruta
        return _cached_settings.copy()

def save(values: Any, path_or_base: PathLike | None = None) -> Path | None:
    """Persiste la configuración con reemplazo atómico tras validar cambios."""
    global _cached_settings, _current_path
    if not isinstance(values, dict): return None
    ruta = settings_path(path_or_base)
    
    if not is_safe_to_modify(str(ruta.parent)): return None
        
    limpio = validate(values)
    if limpio.get("asistente_activado") and not (limpio.get("asistente_clave_api") or os.environ.get(API_KEY_ENV_VAR)):
        limpio["asistente_activado"] = False
    
    if _cached_settings == limpio and _current_path == ruta: return ruta

    temp_path: Path | None = None
    try:
        parent_dir = ruta.parent
        ensure_safe_to_modify(parent_dir)
        if not parent_dir.exists(): parent_dir.mkdir(parents=True, exist_ok=True)
        
        fd, temp_name = tempfile.mkstemp(dir=parent_dir, text=True)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as tf:
            json.dump(limpio, tf, indent=2, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
        
        ensure_safe_to_modify(ruta)
        os.replace(temp_path, ruta)
        _cached_settings, _current_path = limpio, ruta
        return ruta
    except (OSError, PermissionError, RuntimeError, Exception):
        return None
    finally:
        if temp_path and temp_path.exists():
            try: temp_path.unlink()
            except OSError: pass

def update(changes: dict[str, Any], path_or_base: PathLike | None = None) -> AppSettings:
    """Aplica cambios parciales a la configuración actual y guarda a disco."""
    actual = load(path_or_base)
    actual.update(changes)
    save(actual, path_or_base)
    return actual

def reset(path_or_base: PathLike | None = None) -> AppSettings:
    """Reestablece todos los valores a los definidos en DEFAULTS."""
    save(DEFAULTS, path_or_base)
    return DEFAULTS.copy()

def get(key: str, path_or_base: PathLike | None = None) -> Any:
    """Recupera el valor de una clave específica evitando copias innecesarias."""
    return load(path_or_base).get(key, DEFAULTS.get(key))

def assistant_api_key(path_or_base: PathLike | None = None) -> str:
    """Retorna la API Key priorizando la variable de entorno por seguridad."""
    desde_entorno = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if desde_entorno: return desde_entorno
    return load(path_or_base).get("asistente_clave_api", "").strip()

def assistant_enabled(path_or_base: PathLike | None = None) -> bool:
    """Verifica si el asistente está activo y dispone de una clave válida."""
    config = load(path_or_base)
    return bool(config.get("asistente_activado")) and bool(assistant_api_key(path_or_base))

def describe(path_or_base: PathLike | None = None) -> list[str]:
    """Genera un reporte legible de la configuración actual."""
    actual = load(path_or_base)
    clave = assistant_api_key(path_or_base)
    origen = f"variable de entorno {API_KEY_ENV_VAR}" if os.environ.get(API_KEY_ENV_VAR) else ("archivo de configuración" if clave else "no configurada")
    return [
        "Configuración actual", "", f"  Archivo: {settings_path(path_or_base)}", "",
        "  Apariencia", f"    Tema: {actual['tema']}", f"    Acento: {actual['acento']}",
        f"    Barras visuales: {'sí' if actual['mostrar_barras'] else 'no'}", "",
        "  Comportamiento", f"    Confirmar siempre: {'sí' if actual['confirmar_siempre'] else 'no'}",
        f"    Pestaña inicial: {actual['abrir_en']}", f"    Recordar carpeta: {'sí' if actual['recordar_ultima_carpeta'] else 'no'}", "",
        "  Rendimiento", f"    Duplicados desde: {actual['duplicados_tamano_minimo_kb']} KB",
        f"    Top de archivos: {actual['top_archivos']}", f"    Análisis en paralelo: {'sí' if actual['analisis_en_paralelo'] else 'no'}", "",
        "  Asistente IA", f"    Activado: {'sí' if actual['asistente_activado'] else 'no'}",
        f"    Clave: {origen}", f"    Modelo: {actual['asistente_modelo']}", ""
    ]
