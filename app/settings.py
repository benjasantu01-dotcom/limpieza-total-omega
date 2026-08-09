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

from safety import is_safe_to_modify

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

VALID_THEMES: Final[tuple[str, ...]] = ("oscuro", "claro", "sistema")
VALID_ACCENTS: Final[tuple[str, ...]] = ("menta", "violeta", "magenta", "cian", "ambar")

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
    """Namespace para funciones de validación. Retornan None si el valor es inválido."""
    
    @staticmethod
    def bool(key: str, val: Any) -> bool | None:
        if isinstance(val, bool): return val
        if not isinstance(val, str): return None
        normalized = val.strip().lower()
        if normalized in ("1", "true", "si", "sí", "yes"): return True
        if normalized in ("0", "false", "no", "none"): return False
        return None

    @staticmethod
    def int(key: str, val: Any) -> int | None:
        if val is None or isinstance(val, bool): return None
        try:
            parsed_value = int(val)
            min_limit, max_limit = _NUMERIC_LIMITS.get(key, (0, 10**9))
            return max(min_limit, min(max_limit, parsed_value))
        except (TypeError, ValueError): 
            return None

    @staticmethod
    def path(val: Any) -> str | None:
        """Valida rutas de forma defensiva antes de persistirlas."""
        if val is None: return ""
        if not isinstance(val, (str, Path)): return None
        try:
            path_string = str(val).strip()
            if not path_string: return ""
            path_obj = Path(path_string).expanduser()
            
            if any(part in ('.', '..', '..\\', '../') for part in path_obj.parts): return None
            if not path_obj.is_absolute(): return None
            
            resolved = path_obj.resolve(strict=False)
            if resolved.is_symlink(): return None
            
            target = resolved if resolved.exists() else resolved.parent
            if is_safe_to_modify(str(target)):
                return str(resolved)
        except (OSError, RuntimeError, ValueError, TypeError, PermissionError):
            pass
        return None

    @staticmethod
    def str(key: str, val: Any) -> str | None:
        if val is None: return None
        if not isinstance(val, (str, Path)): return None
        text = str(val).strip()
        if not text: return "" if key in ("ultima_carpeta", "asistente_clave_api") else None
        
        if key == "tema": return text.lower() if text.lower() in VALID_THEMES else None
        if key == "acento": return text.lower() if text.lower() in VALID_ACCENTS else None
        if key == "ultima_carpeta": return _Validators.path(text)
        return text if len(text) <= 512 else None

_VALIDATOR_MAP: Final[dict[str, Callable[[str, Any], Any]]] = {
    "tema": _Validators.str, "acento": _Validators.str, "ultima_carpeta": _Validators.str, 
    "asistente_clave_api": _Validators.str, "asistente_modelo": _Validators.str, "abrir_en": _Validators.str,
    "mostrar_barras": _Validators.bool, "animaciones": _Validators.bool, "confirmar_siempre": _Validators.bool, 
    "recordar_ultima_carpeta": _Validators.bool, "analisis_en_paralelo": _Validators.bool, 
    "asistente_activado": _Validators.bool, "asistente_enviar_metricas": _Validators.bool,
    "duplicados_tamano_minimo_kb": _Validators.int, "top_archivos": _Validators.int, "top_procesos": _Validators.int
}

def settings_path(path_or_base: PathLike | None = None) -> Path:
    default_res = SETTINGS_DIR / SETTINGS_FILE
    if path_or_base is None: return default_res
    
    key = str(path_or_base)
    if key not in _path_cache:
        try:
            base = Path(key).expanduser().resolve(strict=False)
            candidate = base / SETTINGS_FILE
            is_valid = is_safe_to_modify(str(base))
            _path_cache[key] = candidate if is_valid else default_res
        except (OSError, RuntimeError, PermissionError):
            _path_cache[key] = default_res
    return _path_cache[key]

def validate(values: Any) -> AppSettings:
    config = DEFAULTS.copy()
    if not isinstance(values, dict): return config
    for key, validator in _VALIDATOR_MAP.items():
        if key in values:
            val = validator(key, values.get(key))
            if val is not None: config[key] = val
    return config

def load(path_or_base: PathLike | None = None) -> AppSettings:
    global _cached_settings, _current_path
    ruta = settings_path(path_or_base)
    if _cached_settings is not None and _current_path == ruta:
        return _cached_settings.copy()
    
    try:
        if ruta.exists() and ruta.is_file() and is_safe_to_modify(str(ruta)):
            if 0 < ruta.stat().st_size <= MAX_SETTINGS_SIZE:
                with open(ruta, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _cached_settings = validate(data)
                    _current_path = ruta
                    return _cached_settings.copy()
    except (OSError, PermissionError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    
    _cached_settings = DEFAULTS.copy()
    _current_path = ruta
    return _cached_settings.copy()

def save(values: Any, path_or_base: PathLike | None = None) -> Path | None:
    global _cached_settings, _current_path
    if not isinstance(values, dict): return None
    ruta = settings_path(path_or_base)
    
    parent = ruta.parent
    if not parent.exists():
        try: 
            parent.mkdir(parents=True, exist_ok=True)
        except OSError: 
            return None
        
    if not is_safe_to_modify(str(parent)): return None
        
    cleaned_settings = validate(values)
    if cleaned_settings.get("asistente_activado") and not (cleaned_settings.get("asistente_clave_api") or os.environ.get(API_KEY_ENV_VAR)):
        cleaned_settings["asistente_activado"] = False
    
    if _cached_settings == cleaned_settings and _current_path == ruta: 
        return ruta

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=parent, delete=False, encoding="utf-8") as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(cleaned_settings, temp_file, indent=2, ensure_ascii=False)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, ruta)
        _cached_settings, _current_path = cleaned_settings, ruta
        return ruta
    except (OSError, IOError, PermissionError, RuntimeError):
        if temp_path and temp_path.exists(): 
            try: temp_path.unlink()
            except OSError: pass
        return None

def update(changes: dict[str, Any], path_or_base: PathLike | None = None) -> AppSettings:
    current = load(path_or_base)
    current.update(changes)
    save(current, path_or_base)
    return current

def reset(path_or_base: PathLike | None = None) -> AppSettings:
    save(DEFAULTS, path_or_base)
    return DEFAULTS.copy()

def get(key: str, path_or_base: PathLike | None = None) -> Any:
    return load(path_or_base).get(key, DEFAULTS.get(key))

def assistant_api_key(path_or_base: PathLike | None = None) -> str:
    env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return env_key if env_key else load(path_or_base).get("asistente_clave_api", "").strip()

def assistant_enabled(path_or_base: PathLike | None = None) -> bool:
    settings = load(path_or_base)
    return bool(settings.get("asistente_activado") and assistant_api_key(path_or_base))

def describe(path_or_base: PathLike | None = None) -> list[str]:
    current = load(path_or_base)
    key = assistant_api_key(path_or_base)
    origin = f"variable de entorno {API_KEY_ENV_VAR}" if os.environ.get(API_KEY_ENV_VAR) else ("archivo de configuración" if key else "no configurada")
    return [
        "Configuración actual", "", f"  Archivo: {settings_path(path_or_base)}", "",
        "  Apariencia", f"    Tema: {current['tema']}", f"    Acento: {current['acento']}",
        f"    Barras visuales: {'sí' if current['mostrar_barras'] else 'no'}", "",
        "  Comportamiento", f"    Confirmar siempre: {'sí' if current['confirmar_siempre'] else 'no'}",
        f"    Pestaña inicial: {current['abrir_en']}", f"    Recordar carpeta: {'sí' if current['recordar_ultima_carpeta'] else 'no'}", "",
        "  Rendimiento", f"    Duplicados desde: {current['duplicados_tamano_minimo_kb']} KB",
        f"    Top de archivos: {current['top_archivos']}", f"    Análisis en paralelo: {'sí' if current['analisis_en_paralelo'] else 'no'}", "",
        "  Asistente IA", f"    Activado: {'sí' if current['asistente_activado'] else 'no'}",
        f"    Clave: {origin}", f"    Modelo: {current['asistente_modelo']}", ""
    ]
