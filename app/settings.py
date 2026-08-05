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
from typing import Any, Final, TypeAlias, Callable

from safety import is_safe_to_modify, ensure_safe_to_modify

PathLike: TypeAlias = str | Path

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

SETTINGS_DIR: Final = Path("~/LimpiezaTotalOmega").expanduser()
SETTINGS_FILE: Final = "config.json"
MAX_SETTINGS_SIZE: Final = 1024 * 64

API_KEY_ENV_VAR: Final = "OMEGA_GEMINI_KEY"

VALID_THEMES: Final = ("oscuro", "claro", "sistema")
VALID_ACCENTS: Final = ("menta", "violeta", "magenta", "cian", "ambar")

_cached_settings: dict[str, Any] | None = None
_last_path: Path | None = None
_last_mtime: float = -1.0
_path_cache: dict[str, Path] = {}

DEFAULTS: Final[dict[str, Any]] = {
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

def _validate_bool(key: str, val: Any) -> bool | None:
    """Intenta convertir entradas diversas (bool, int, str) a un booleano estricto."""
    if isinstance(val, bool): return val
    if isinstance(val, str) and val.strip().lower() in ("1", "true", "si", "sí", "yes"): return True
    if isinstance(val, str) and val.strip().lower() in ("0", "false", "no", "none"): return False
    return None

def _validate_int(key: str, val: Any) -> int | None:
    """Valida que sea un entero y lo limita al rango definido en _NUMERIC_LIMITS."""
    if val is None or isinstance(val, bool): return None
    try:
        parsed = int(val)
        low, high = _NUMERIC_LIMITS.get(key, (0, 10**9))
        return max(low, min(high, parsed))
    except (TypeError, ValueError): return None

def _validate_str(key: str, val: Any) -> str | None:
    """Valida strings según su contexto (Enum, ruta segura o texto general)."""
    if not isinstance(val, str): return None
    text = val.strip()
    if not text: return "" if key in ("ultima_carpeta", "asistente_clave_api") else None
    
    text_lower = text.lower()
    if key == "tema": return text_lower if text_lower in VALID_THEMES else None
    if key == "acento": return text_lower if text_lower in VALID_ACCENTS else None
    
    if key == "ultima_carpeta":
        try:
            path = Path(text).expanduser().resolve()
            return str(path) if is_safe_to_modify(str(path)) else None
        except (OSError, RuntimeError, ValueError, TypeError, PermissionError): return None
        
    return text if len(text) <= 256 else None

_VALIDATOR_MAP: Final[dict[str, Callable[[str, Any], Any]]] = {
    "tema": _validate_str, "acento": _validate_str, "ultima_carpeta": _validate_str, 
    "asistente_clave_api": _validate_str, "asistente_modelo": _validate_str, "abrir_en": _validate_str,
    "mostrar_barras": _validate_bool, "animaciones": _validate_bool, "confirmar_siempre": _validate_bool, 
    "recordar_ultima_carpeta": _validate_bool, "analisis_en_paralelo": _validate_bool, 
    "asistente_activado": _validate_bool, "asistente_enviar_metricas": _validate_bool,
    "duplicados_tamano_minimo_kb": _validate_int, "top_archivos": _validate_int, "top_procesos": _validate_int
}

def settings_path(path_or_base: PathLike | None = None) -> Path:
    """Resuelve la ruta absoluta del archivo de configuración, asegurando seguridad."""
    key = str(path_or_base or SETTINGS_DIR)
    if key in _path_cache: return _path_cache[key]
    try:
        base = Path(key).expanduser()
        current = base
        while not is_safe_to_modify(str(current)) and current != current.parent:
            current = current.parent
        res = (current / SETTINGS_FILE).resolve()
    except (OSError, RuntimeError, ValueError, PermissionError):
        res = SETTINGS_DIR.resolve() / SETTINGS_FILE
    _path_cache[key] = res
    return res

def validate(values: Any) -> dict[str, Any]:
    """Limpia y valida un diccionario, sustituyendo valores inválidos por los DEFAULTS."""
    if not isinstance(values, dict):
        return DEFAULTS.copy()
    
    configuracion_final: dict[str, Any] = {}
    for clave, valor_defecto in DEFAULTS.items():
        valor_usuario = values.get(clave)
        validador = _VALIDATOR_MAP.get(clave)
        resultado = validador(clave, valor_usuario) if (validador and valor_usuario is not None) else None
        configuracion_final[clave] = resultado if resultado is not None else valor_defecto
        
    return configuracion_final

def load(path_or_base: PathLike | None = None) -> dict[str, Any]:
    """Carga configuraciones desde disco con caché y validación robusta."""
    global _cached_settings, _last_path, _last_mtime
    ruta = settings_path(path_or_base)
    
    try:
        stat = ruta.stat()
        if _cached_settings is not None and ruta == _last_path and stat.st_mtime == _last_mtime:
            return _cached_settings
        
        if not is_safe_to_modify(str(ruta)): raise PermissionError("Unsafe path")
        if stat.st_size > MAX_SETTINGS_SIZE: raise OSError("Config too large")
        
        data = json.loads(ruta.read_text(encoding="utf-8")) if stat.st_size > 0 else {}
        if not isinstance(data, dict): raise ValueError("Invalid structure")
        
        _cached_settings = validate(data)
        _last_path, _last_mtime = ruta, stat.st_mtime
        return _cached_settings
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, FileNotFoundError, PermissionError, ValueError):
        _cached_settings = DEFAULTS.copy()
        _last_path, _last_mtime = ruta, 0.0
        return _cached_settings

def save(values: Any, path_or_base: PathLike | None = None) -> Path | None:
    """Persiste configuración de forma atómica tras validar que la ruta sea segura."""
    global _cached_settings, _last_path, _last_mtime
    if not isinstance(values, dict): return None
    
    ruta = settings_path(path_or_base)
    limpio = validate(values)
    
    if limpio.get("asistente_activado") and not (limpio.get("asistente_clave_api") or os.environ.get(API_KEY_ENV_VAR)):
        limpio["asistente_activado"] = False
    
    try:
        ensure_safe_to_modify(ruta.parent)
        ensure_safe_to_modify(ruta)
        
        json_data = json.dumps(limpio, indent=2, ensure_ascii=False)
        ruta.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError): return None
    
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=ruta.parent, delete=False, encoding="utf-8") as tf:
            temp_path = Path(tf.name)
            tf.write(json_data)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(temp_path, ruta)
        _cached_settings, _last_path, _last_mtime = limpio, ruta, ruta.stat().st_mtime
        return ruta
    except (OSError, PermissionError, RuntimeError): return None
    finally:
        if temp_path and temp_path.exists():
            try: temp_path.unlink()
            except OSError: pass

def update(changes: dict[str, Any], path_or_base: PathLike | None = None) -> dict[str, Any]:
    """Aplica cambios parciales a la configuración y los guarda."""
    actual = (load(path_or_base)).copy()
    actual.update(changes)
    save(actual, path_or_base)
    return actual

def reset(path_or_base: PathLike | None = None) -> dict[str, Any]:
    """Restaura todos los valores a los definidos en DEFAULTS."""
    save(DEFAULTS, path_or_base)
    return DEFAULTS.copy()

def get(key: str, path_or_base: PathLike | None = None) -> Any:
    """Recupera el valor de una clave, consultando caché primero."""
    if _cached_settings is not None:
        return _cached_settings.get(key, DEFAULTS.get(key))
    return load(path_or_base).get(key, DEFAULTS.get(key))

def assistant_api_key(path_or_base: PathLike | None = None) -> str:
    """Obtiene API Key desde entorno (prioridad) o configuración."""
    desde_entorno = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if desde_entorno: return desde_entorno
    config = _cached_settings if _cached_settings is not None else load(path_or_base)
    return config.get("asistente_clave_api", "").strip()

def assistant_enabled(path_or_base: PathLike | None = None) -> bool:
    """Determina disponibilidad del asistente basado en estado y API Key."""
    config = _cached_settings if _cached_settings is not None else load(path_or_base)
    return bool(config.get("asistente_activado")) and bool(assistant_api_key(path_or_base))

def describe(path_or_base: PathLike | None = None) -> list[str]:
    """Retorna un reporte legible con el estado actual de la configuración."""
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
