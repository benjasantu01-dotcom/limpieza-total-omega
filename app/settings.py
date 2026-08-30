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
import shutil
import time
from enum import Enum
from pathlib import Path
from typing import Any, Final, TypeAlias, Callable, TypedDict, Optional, TypeVar, ParamSpec, NamedTuple, TypeGuard
from functools import lru_cache

from safety import is_safe_to_modify, is_protected_path, ensure_safe_to_modify

PathLike: TypeAlias = str | Path
T = TypeVar("T")
P = ParamSpec("P")

class ConfigKey(Enum):
    """Enumeración de todas las claves válidas dentro del JSON de configuración."""
    TEMA = "tema"
    ACENTO = "acento"
    MOSTRAR_BARRAS = "mostrar_barras"
    ANIMACIONES = "animaciones"
    CONFIRMAR_SIEMPRE = "confirmar_siempre"
    ABRIR_EN = "abrir_en"
    RECORDAR_ULTIMA_CARPETA = "recordar_ultima_carpeta"
    ULTIMA_CARPETA = "ultima_carpeta"
    DUPLICADOS_TAMANO_MINIMO_KB = "duplicados_tamano_minimo_kb"
    TOP_ARCHIVOS = "top_archivos"
    TOP_PROCESOS = "top_procesos"
    ANALISIS_EN_PARALELO = "analisis_en_paralelo"
    ASISTENTE_ACTIVADO = "asistente_activado"
    ASISTENTE_CLAVE_API = "asistente_clave_api"
    ASISTENTE_ENVIAR_METRICAS = "asistente_enviar_metricas"
    ASISTENTE_MODELO = "asistente_modelo"

class AppSettings(TypedDict):
    """Define el esquema estricto de la configuración persistida en disco."""
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

class _NumericRange(NamedTuple):
    """Límites definidos para validar entradas numéricas y evitar desbordamientos."""
    min: int
    max: int

def _is_dict(val: Any) -> TypeGuard[dict[Any, Any]]:
    """Helper para verificar que un objeto sea un diccionario utilizable."""
    return isinstance(val, dict)

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

_CACHE: dict[str, tuple[float, AppSettings]] = {}
_CACHE_TTL: Final = 0.5

VALID_THEMES: Final[frozenset[str]] = frozenset(("oscuro", "claro", "sistema"))
VALID_ACCENTS: Final[frozenset[str]] = frozenset(("menta", "violeta", "magenta", "cian", "ambar"))

_STR_TO_ENUM: Final[dict[str, ConfigKey]] = {k.value: k for k in ConfigKey}

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

_NUMERIC_LIMITS: Final[dict[ConfigKey, _NumericRange]] = {
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB: _NumericRange(0, 1024 * 1024),
    ConfigKey.TOP_ARCHIVOS: _NumericRange(1, 500),
    ConfigKey.TOP_PROCESOS: _NumericRange(1, 500),
}

_ENUM_VALS: Final[dict[ConfigKey, frozenset[str]]] = {
    ConfigKey.TEMA: VALID_THEMES,
    ConfigKey.ACENTO: VALID_ACCENTS
}

def type_check(func: Callable[P, T | None]) -> Callable[P, T | None]:
    """Decorador: Filtra llamadas donde el argumento de valor es None."""
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        val = args[1] if len(args) > 1 else kwargs.get("val")
        if val is None: return None
        return func(*args, **kwargs)
    return wrapper

class _Validators:
    """
    Motor central de validación: Sanitiza entradas provenientes de archivos 
    JSON o de la interfaz de usuario, garantizando que el estado interno 
    siempre cumpla con los tipos y las restricciones de seguridad.
    """
    
    @staticmethod
    def _run_safety_checks(path_obj: Path) -> bool:
        """Verifica restricciones de sistema: evita enlaces simbólicos o junctions."""
        try:
            if path_obj.is_symlink() or (hasattr(path_obj, 'is_junction') and path_obj.is_junction()):
                return False
            return not is_protected_path(str(path_obj)) and is_safe_to_modify(str(path_obj))
        except (OSError, PermissionError):
            return False

    @staticmethod
    def _is_safe_path(path_str: str) -> bool:
        """Determina si una cadena representa una ruta válida, absoluta y segura."""
        if not path_str or ".." in path_str: return False
        try:
            p = Path(path_str).expanduser()
            if not p.is_absolute():
                return False
            return _Validators._run_safety_checks(p)
        except (OSError, RuntimeError, PermissionError, AttributeError):
            return False

    @staticmethod
    def bool(key: ConfigKey, val: Any) -> Optional[bool]:
        """Convierte entradas variadas (str/int/bool) a un valor booleano estricto."""
        if isinstance(val, bool): return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in ("1", "true", "si", "sí", "yes"): return True
            if normalized in ("0", "false", "no", "none"): return False
        return None

    @staticmethod
    @type_check
    def int(key: ConfigKey, val: Any) -> Optional[int]:
        """Castea a int dentro del rango [min, max] definido en _NUMERIC_LIMITS."""
        try:
            parsed_value: int = int(val)
            limit = _NUMERIC_LIMITS.get(key, _NumericRange(0, 10**9))
            return max(limit.min, min(limit.max, parsed_value))
        except (TypeError, ValueError, OverflowError): return None

    @staticmethod
    def path(key: ConfigKey, val: Any) -> Optional[str]:
        """Valida rutas: verifica formato, longitud y seguridad mediante _is_safe_path."""
        if not isinstance(val, (str, Path)): return None
        path_string = str(val).strip()
        if not path_string or len(path_string) > 4096 or "\0" in path_string: 
            return None
        return path_string if _Validators._is_safe_path(path_string) else None

    @staticmethod
    def _validate_enum_str(text: str, key: ConfigKey) -> Optional[str]:
        """Verifica que el string esté dentro del conjunto permitido para la clave."""
        val = text.lower()
        if key in _ENUM_VALS: return val if val in _ENUM_VALS[key] else None
        return text if len(text) <= 512 else None

    @staticmethod
    @type_check
    def str(key: ConfigKey, val: Any) -> Optional[str]:
        """Valida strings generales, filtrando caracteres de control y validando contra Enums."""
        if not isinstance(val, str): return None
        text = val.strip()
        if not text or "\0" in text or any(ord(c) < 32 for c in text) or ".." in text or len(text) > 1024: return None
        if key == ConfigKey.ULTIMA_CARPETA: return _Validators.path(key, text)
        return _Validators._validate_enum_str(text, key)

_VALIDATOR_MAP: Final[dict[ConfigKey, Callable[[ConfigKey, Any], Any]]] = {
    ConfigKey.TEMA: _Validators.str,
    ConfigKey.ACENTO: _Validators.str,
    ConfigKey.ABRIR_EN: _Validators.str,
    ConfigKey.ULTIMA_CARPETA: _Validators.path,
    ConfigKey.ASISTENTE_CLAVE_API: _Validators.str,
    ConfigKey.ASISTENTE_MODELO: _Validators.str,
    ConfigKey.MOSTRAR_BARRAS: _Validators.bool,
    ConfigKey.ANIMACIONES: _Validators.bool,
    ConfigKey.CONFIRMAR_SIEMPRE: _Validators.bool,
    ConfigKey.RECORDAR_ULTIMA_CARPETA: _Validators.bool,
    ConfigKey.ANALISIS_EN_PARALELO: _Validators.bool,
    ConfigKey.ASISTENTE_ACTIVADO: _Validators.bool,
    ConfigKey.ASISTENTE_ENVIAR_METRICAS: _Validators.bool,
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB: _Validators.int,
    ConfigKey.TOP_ARCHIVOS: _Validators.int,
    ConfigKey.TOP_PROCESOS: _Validators.int
}

def settings_path(custom_base: PathLike | None = None) -> Path:
    """Retorna la ruta absoluta del archivo de configuración, validando la base."""
    if custom_base is None: return SETTINGS_DIR / SETTINGS_FILE
    try:
        base_str = str(custom_base)
        if _Validators._is_safe_path(base_str):
            return Path(base_str).expanduser().resolve(strict=False) / SETTINGS_FILE
    except (OSError, RuntimeError):
        pass
    return SETTINGS_DIR / SETTINGS_FILE

def validate(raw_values: Any) -> AppSettings:
    """Valida un dict contra AppSettings; usa DEFAULTS en caso de error o dato faltante."""
    config = DEFAULTS.copy()
    if not _is_dict(raw_values): return config
    for key_str, val in raw_values.items():
        key = _STR_TO_ENUM.get(key_str)
        if key and key in _VALIDATOR_MAP:
            validated = _VALIDATOR_MAP[key](key, val)
            if validated is not None:
                config[key.value] = validated
    return config

@lru_cache(maxsize=4)
def _read_disk(ruta_str: str, mtime: float) -> AppSettings:
    """Carga interna: valida el archivo en disco, retornando DEFAULTS ante cualquier error."""
    ruta = Path(ruta_str)
    if not ruta.exists() or not os.access(ruta, os.R_OK):
        return DEFAULTS.copy()
    
    stat_info = ruta.stat()
    if stat_info.st_size > MAX_SETTINGS_SIZE or stat_info.st_size < 2:
        return DEFAULTS.copy()
            
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            data: Any = json.load(f)
            if not _is_dict(data): return DEFAULTS.copy()
            return validate(data)
    except (json.JSONDecodeError, UnicodeDecodeError, PermissionError):
        return DEFAULTS.copy()

def load(custom_base: PathLike | None = None) -> AppSettings:
    """Carga la configuración desde disco; usa cache de corta duración para minimizar I/O."""
    ruta = settings_path(custom_base)
    ruta_str = str(ruta)
    now = time.monotonic()
    
    if ruta_str in _CACHE:
        ts, data = _CACHE[ruta_str]
        if now - ts < _CACHE_TTL:
            return data.copy()
            
    try:
        mtime = ruta.stat().st_mtime if ruta.exists() else 0.0
        data = _read_disk(ruta_str, mtime).copy()
        _CACHE[ruta_str] = (now, data)
        return data
    except (OSError, PermissionError, RuntimeError):
        return DEFAULTS.copy()

def save(values: Any, custom_base: PathLike | None = None) -> Path | None:
    """Persiste la configuración en disco mediante reemplazo atómico y validación previa."""
    if not _is_dict(values): return None
    cleaned_settings = validate(values)
    
    api_key_from_env = os.environ.get(API_KEY_ENV_VAR)
    has_api_key = bool(cleaned_settings.get("asistente_clave_api")) or bool(api_key_from_env)
    if cleaned_settings.get("asistente_activado") and not has_api_key:
        cleaned_settings["asistente_activado"] = False
    
    ruta = settings_path(custom_base)
    try:
        parent = ruta.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        elif not parent.is_dir():
            return None
        
        ensure_safe_to_modify(str(parent))
        if not os.access(parent, os.W_OK): return None
            
        if ruta.exists():
            if not is_safe_to_modify(str(ruta)): return None
            ensure_safe_to_modify(str(ruta))
        
        usage = shutil.disk_usage(parent)
        if usage.free < MAX_SETTINGS_SIZE * 2: return None
        
        data = json.dumps(cleaned_settings, indent=2, ensure_ascii=False)
        encoded_data = data.encode("utf-8")
        if len(encoded_data) > MAX_SETTINGS_SIZE: return None
        
        temp_path = ruta.with_suffix(f"{ruta.suffix}.tmp")
        try:
            with open(temp_path, "wb") as f:
                f.write(encoded_data)
                f.flush()
                try: os.fsync(f.fileno())
                except (OSError, AttributeError, NotImplementedError): pass
            os.replace(temp_path, ruta)
        finally:
            if temp_path.exists():
                try: os.remove(temp_path)
                except OSError: pass
            
        _read_disk.cache_clear()
        _CACHE.clear()
        return ruta
    except (TypeError, ValueError, OSError, IOError, PermissionError, RuntimeError, json.JSONDecodeError):
        return None

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    """Aplica cambios parciales validados a la configuración actual."""
    current = load(custom_base)
    modified = False
    for k, v in changes.items():
        key_enum = _STR_TO_ENUM.get(k)
        if key_enum and key_enum in _VALIDATOR_MAP:
            val = _VALIDATOR_MAP[key_enum](key_enum, v)
            if val is not None and val != current.get(k):
                current[k] = val
                modified = True
    if modified: save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    """Reestablece la configuración a los valores por defecto de fábrica."""
    save(DEFAULTS, custom_base)
    return DEFAULTS.copy()

def get(key: str, custom_base: PathLike | None = None) -> Any:
    """Retorna un valor individual de la configuración actual."""
    return load(custom_base).get(key, DEFAULTS.get(key))

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    """Determina la clave API: prioriza la variable de entorno sobre el archivo de configuración."""
    env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return env_key if env_key else load(custom_base).get("asistente_clave_api", "").strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    """Valida si el asistente puede ejecutarse: requiere clave y estado activo."""
    if os.environ.get(API_KEY_ENV_VAR): return True
    settings = load(custom_base)
    return bool(settings.get("asistente_activado", False)) and bool(settings.get("asistente_clave_api", "").strip())

def describe(custom_base: PathLike | None = None) -> list[str]:
    """Genera una representación textual de la configuración para fines de reporte."""
    current = load(custom_base)
    api_key_env = os.environ.get(API_KEY_ENV_VAR)
    api_key_file = current.get("asistente_clave_api", "").strip()
    api_present = bool(api_key_env) or bool(api_key_file)
    
    origin = f"variable de entorno {API_KEY_ENV_VAR}" if api_key_env else ("archivo de configuración" if api_key_file else "no configurada")
    return [
        "Configuración actual", "", f"  Archivo: {settings_path(custom_base)}", "",
        "  Apariencia", f"    Tema: {current['tema']}", f"    Acento: {current['acento']}",
        f"    Barras visuales: {'sí' if current['mostrar_barras'] else 'no'}", "",
        "  Comportamiento", f"    Confirmar siempre: {'sí' if current['confirmar_siempre'] else 'no'}",
        f"    Pestaña inicial: {current['abrir_en']}", f"    Recordar carpeta: {'sí' if current['recordar_ultima_carpeta'] else 'no'}", "",
        "  Rendimiento", f"    Duplicados desde: {current['duplicados_tamano_minimo_kb']} KB",
        f"    Top de archivos: {current['top_archivos']}", f"    Análisis en paralelo: {'sí' if current['analisis_en_paralelo'] else 'no'}", "",
        "  Asistente IA", f"    Activado: {'sí' if current['asistente_activado'] else 'no'}",
        f"    Clave: {origin}", f"    Modelo: {current['asistente_modelo']}", ""
    ]
