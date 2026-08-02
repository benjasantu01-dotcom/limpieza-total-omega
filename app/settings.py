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
from typing import Any, Final, TypeAlias

from safety import is_safe_to_modify

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
_last_mtime: float = 0.0
_path_cache: dict[PathLike, Path] = {}

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
    if isinstance(val, bool): return val
    if isinstance(val, str) and val.strip().lower() in ("1", "true", "si", "sí", "yes"): return True
    return None

def _validate_int(key: str, val: Any) -> int | None:
    if isinstance(val, bool): return None
    try:
        parsed = int(val)
        low, high = _NUMERIC_LIMITS.get(key, (0, 10**9))
        return max(low, min(high, parsed))
    except (TypeError, ValueError): return None

def _validate_str(key: str, val: Any) -> str | None:
    if not isinstance(val, str): return None
    text = val.strip()
    if not text: return "" if key == "ultima_carpeta" else None
    if key == "tema" and text.lower() not in VALID_THEMES: return None
    if key == "acento" and text.lower() not in VALID_ACCENTS: return None
    if key == "ultima_carpeta":
        try:
            path = Path(text).expanduser().resolve()
            return str(path) if is_safe_to_modify(str(path)) else None
        except (OSError, RuntimeError, ValueError): return None
    return text.lower() if key in ("tema", "acento") else text

_VALIDATOR_MAP: Final = {
    bool: _validate_bool,
    int: _validate_int,
    str: _validate_str
}

def settings_path(path_or_base: PathLike | None = None) -> Path:
    key = path_or_base or SETTINGS_DIR
    if key in _path_cache: return _path_cache[key]
    try:
        base = Path(key).expanduser()
        while not is_safe_to_modify(str(base)) and base != base.parent:
            base = base.parent
        res = base.resolve() / SETTINGS_FILE
    except (OSError, RuntimeError, ValueError):
        res = SETTINGS_DIR.resolve() / SETTINGS_FILE
    _path_cache[key] = res
    return res

def validate(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict): return DEFAULTS.copy()
    limpio = {}
    for clave, defecto in DEFAULTS.items():
        val = values.get(clave)
        if val is None:
            limpio[clave] = defecto
            continue
        validator = _VALIDATOR_MAP.get(type(defecto))
        coerced = validator(clave, val) if validator else val
        limpio[clave] = coerced if coerced is not None else defecto
    return limpio

def load(path_or_base: PathLike | None = None) -> dict[str, Any]:
    global _cached_settings, _last_path, _last_mtime
    ruta = settings_path(path_or_base)
    if not ruta.exists(): return DEFAULTS.copy()
    try:
        stat = ruta.stat()
        if _cached_settings is not None and ruta == _last_path and stat.st_mtime == _last_mtime:
            return _cached_settings.copy()
        if stat.st_size > MAX_SETTINGS_SIZE: return DEFAULTS.copy()
        data = json.loads(ruta.read_text(encoding="utf-8"))
        _cached_settings = validate(data)
        _last_path, _last_mtime = ruta, stat.st_mtime
        return _cached_settings.copy()
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return DEFAULTS.copy()

def save(values: Any, path_or_base: PathLike | None = None) -> Path | None:
    global _cached_settings, _last_path, _last_mtime
    ruta = settings_path(path_or_base)
    if not is_safe_to_modify(str(ruta.parent)) or (ruta.exists() and ruta.is_symlink()): return None
    if ruta.parent.exists() and not os.access(ruta.parent, os.W_OK): return None
    limpio = validate(values)
    try:
        json_data = json.dumps(limpio, indent=2, ensure_ascii=False)
        ruta.parent.mkdir(parents=True, exist_ok=True)
    except (TypeError, ValueError, OSError): return None
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
    actual = load(path_or_base)
    actual.update(changes)
    save(actual, path_or_base)
    return actual

def reset(path_or_base: PathLike | None = None) -> dict[str, Any]:
    save(DEFAULTS, path_or_base)
    return DEFAULTS.copy()

def get(key: str, path_or_base: PathLike | None = None) -> Any:
    return load(path_or_base).get(key, DEFAULTS.get(key))

def assistant_api_key(path_or_base: PathLike | None = None) -> str:
    desde_entorno = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return desde_entorno or load(path_or_base).get("asistente_clave_api", "").strip()

def assistant_enabled(path_or_base: PathLike | None = None) -> bool:
    config = load(path_or_base)
    return bool(config.get("asistente_activado")) and bool(assistant_api_key(path_or_base))

def describe(path_or_base: PathLike | None = None) -> list[str]:
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
