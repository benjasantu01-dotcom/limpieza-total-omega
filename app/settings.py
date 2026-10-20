"""
settings.py — preferencias del usuario, guardadas entre sesiones.

Guarda un JSON chico en la carpeta del usuario. Todo valor se valida al
cargar: un archivo editado a mano, corrupto o de una versión vieja nunca debe
dejar la app sin arrancar, así que cualquier valor inválido se reemplaza
silenciosamente por el de fábrica.
"""

from __future__ import annotations

import json
import os
import stat
import hashlib
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeAlias, Callable, TypedDict, Optional, TypeVar, ParamSpec, NamedTuple, TypeGuard
from functools import lru_cache

from safety import is_safe_to_modify, is_protected_path

PathLike: TypeAlias = str | Path
SettingsDict: TypeAlias = dict[str, Any]
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
    """Esquema de configuración: define las claves persistidas y tipos esperados."""
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
    """Define los límites inferior y superior permitidos para valores numéricos."""
    min: int
    max: int

class _ValidatorEntry(NamedTuple):
    """
    Encapsula una lógica de validación específica.
    El campo 'func' recibe la clave y el valor crudo, retornando el valor
    normalizado si es válido, o None en caso contrario.
    """
    func: Callable[[ConfigKey, Any], Any]

class _ValidationResult(NamedTuple):
    """Contenedor para determinar si un valor es válido y su forma normalizada."""
    is_valid: bool
    value: Any

def _is_dict(val: Any) -> TypeGuard[SettingsDict]:
    """Verifica si el objeto es un diccionario para ser procesado como settings."""
    return isinstance(val, dict)

def _is_valid_type(val: Any, key: ConfigKey) -> bool:
    """Valida si el tipo del valor coincide con el esquema definido en DEFAULTS."""
    default_val = DEFAULTS.get(key.value)
    return isinstance(val, type(default_val))

__all__ = [
    "DEFAULTS", "SETTINGS_DIR", "SETTINGS_FILE", "API_KEY_ENV_VAR",
    "VALID_THEMES", "VALID_ACCENTS", "settings_path", "load", "save",
    "update", "reset", "validate", "get", "assistant_api_key",
    "assistant_enabled", "describe",
]

def _get_default_settings_dir() -> Path:
    """Calcula el directorio base de configuración en el home del usuario."""
    try:
        return Path.home() / "LimpiezaTotalOmega"
    except (OSError, RuntimeError):
        return Path(os.getcwd()) / "LimpiezaTotalOmega"

SETTINGS_DIR: Final = _get_default_settings_dir()
SETTINGS_FILE: Final = "config.json"
MAX_SETTINGS_SIZE: Final = 1024 * 64
API_KEY_ENV_VAR: Final = "OMEGA_GEMINI_KEY"

_BOOL_TRUE_SET: Final = frozenset(("1", "true", "si", "sí", "yes"))
_BOOL_FALSE_SET: Final = frozenset(("0", "false", "no", "none"))

VALID_THEMES: Final[frozenset[str]] = frozenset(("oscuro", "claro", "sistema"))
VALID_ACCENTS: Final[frozenset[str]] = frozenset(("menta", "violeta", "magenta", "cian", "ambar"))
VALID_MODELS: Final[frozenset[str]] = frozenset(("gemini-3.1-flash-lite", "gemini-3.1-pro"))

_KEY_TO_ENUM: Final[dict[str, ConfigKey]] = {k.value: k for k in ConfigKey}

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

_NUMERIC_LIMITS: Final[MappingProxyType[ConfigKey, _NumericRange]] = MappingProxyType({
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB: _NumericRange(0, 1024 * 1024),
    ConfigKey.TOP_ARCHIVOS: _NumericRange(1, 500),
    ConfigKey.TOP_PROCESOS: _NumericRange(1, 500),
})

_ENUM_VALS: Final[MappingProxyType[ConfigKey, frozenset[str]]] = MappingProxyType({
    ConfigKey.TEMA: VALID_THEMES,
    ConfigKey.ACENTO: VALID_ACCENTS,
    ConfigKey.ASISTENTE_MODELO: VALID_MODELS
})

def _is_dir_safe(path: Path) -> bool:
    """Valida que el directorio sea seguro: sin puntos de reanálisis y con permisos estrictos de escritura."""
    try:
        if not path.exists(): return True
        real_path = Path(os.path.realpath(path))
        if not real_path.is_dir() or _Validators._is_reparse_point(real_path): return False
        st = real_path.stat()
        if hasattr(os, 'getuid') and st.st_uid != os.getuid(): return False
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH): return False
        return True
    except (OSError, PermissionError): return False

class _SettingsManager:
    """Singleton que gestiona el estado de la configuración."""
    def __init__(self) -> None:
        self.settings_cache: dict[str, tuple[float, int, AppSettings]] = {}
        self.path_cache: dict[Optional[str], Path] = {}

    def clear(self) -> None:
        """Invalidar caché actual para obligar una lectura fresca del disco."""
        self.settings_cache.clear()

    def perform_atomic_write(self, config_path: Path, serialized: str) -> bool:
        """Escribe datos en archivo temporal con reintentos para manejar bloqueos transitorios."""
        if not isinstance(serialized, str): return False
        temp_path: Path = config_path.with_suffix(".tmp")
        try:
            if not is_safe_to_modify(str(config_path)): return False
            if not _is_dir_safe(config_path.parent): return False
            if config_path.exists() and (config_path.is_symlink() or not config_path.is_file()): return False
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(serialized)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, config_path)
            self.clear()
            return True
        except (OSError, IOError, PermissionError):
            return False

_MANAGER = _SettingsManager()

def type_check(func: Callable[P, T | None]) -> Callable[P, T | None]:
    """Decorador: Captura errores de conversión para asegurar que los validadores retornen None en vez de fallar."""
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        try:
            return func(*args, **kwargs)
        except (ValueError, TypeError, AttributeError, OverflowError, KeyError):
            return None
    return wrapper

class _Validators:
    """Namespace con lógica atómica de validación por tipo de dato."""

    @staticmethod
    @lru_cache(maxsize=32)
    def _is_reparse_point(path_str: str | Path) -> bool:
        try:
            p = Path(os.path.realpath(str(path_str)))
            if p.is_symlink(): return True
            if not p.exists(): return False
            return bool(p.stat().st_file_attributes & 0x400) if hasattr(p.stat(), 'st_file_attributes') else False
        except (OSError, PermissionError):
            return True

    @staticmethod
    @lru_cache(maxsize=128)
    def _run_safety_checks(path_str: str) -> bool:
        try:
            p = Path(path_str).expanduser()
            if not p.is_absolute(): return False
            if p.exists():
                resolved = Path(os.path.realpath(p))
                if any(_Validators._is_reparse_point(str(parent)) for parent in resolved.parents):
                    return False
                return not is_protected_path(str(resolved)) and is_safe_to_modify(str(resolved))
            return not is_protected_path(str(p)) and is_safe_to_modify(str(p))
        except (OSError, PermissionError, RuntimeError, IndexError):
            return False

    @staticmethod
    def _check_path_safety(p: Path) -> bool:
        return p.is_absolute() and _Validators._run_safety_checks(str(p))

    @staticmethod
    def _is_safe_path(path_str: str) -> bool:
        if not path_str or len(path_str) > 2048 or any(c in path_str for c in ("\0", "\r", "\n", "\033")): return False
        if path_str.startswith(("\\\\", "//")): return False
        if is_protected_path(path_str): return False
        try:
            return _Validators._check_path_safety(Path(path_str))
        except (OSError, RuntimeError, PermissionError, AttributeError, ValueError):
            return False

    @staticmethod
    def bool(key: ConfigKey, val: Any) -> Optional[bool]:
        if isinstance(val, bool): return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in _BOOL_TRUE_SET: return True
            if normalized in _BOOL_FALSE_SET: return False
        return None

    @staticmethod
    @type_check
    def int(key: ConfigKey, val: Any) -> Optional[int]:
        if val is None or not isinstance(val, (int, str)): return None
        parsed_value = int(val)
        limit = _NUMERIC_LIMITS.get(key)
        if limit: return max(limit.min, min(limit.max, parsed_value))
        return parsed_value

    @staticmethod
    def path(key: ConfigKey, val: Any) -> Optional[str]:
        if val == "": return ""
        if not isinstance(val, str): return None
        path_string = val.strip()
        if not path_string or "\0" in path_string or len(path_string) > 2048: return None
        return path_string if _Validators._is_safe_path(path_string) else None

    @staticmethod
    def _validate_enum_str(text: str, key: ConfigKey) -> Optional[str]:
        val = text.lower()
        allowed = _ENUM_VALS.get(key)
        if allowed: return val if val in allowed else None
        return text if len(text) <= 512 else None

    @staticmethod
    @type_check
    def str(key: ConfigKey, val: Any) -> Optional[str]:
        if val is None or not isinstance(val, str): return None
        text = val.strip()
        if not text or "\0" in text or any(ord(c) < 32 for c in text) or ".." in text or len(text) > 1024: return None
        return _Validators._validate_enum_str(text, key)

BOOL_KEYS: Final[frozenset[ConfigKey]] = frozenset((
    ConfigKey.MOSTRAR_BARRAS, ConfigKey.ANIMACIONES, ConfigKey.CONFIRMAR_SIEMPRE,
    ConfigKey.RECORDAR_ULTIMA_CARPETA, ConfigKey.ANALISIS_EN_PARALELO,
    ConfigKey.ASISTENTE_ACTIVADO, ConfigKey.ASISTENTE_ENVIAR_METRICAS
))

INT_KEYS: Final[frozenset[ConfigKey]] = frozenset((
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB, ConfigKey.TOP_ARCHIVOS, ConfigKey.TOP_PROCESOS
))

def _get_validator_for_key(k: ConfigKey) -> _ValidatorEntry:
    if k in BOOL_KEYS: return _ValidatorEntry(_Validators.bool)
    if k in INT_KEYS: return _ValidatorEntry(_Validators.int)
    if k == ConfigKey.ULTIMA_CARPETA: return _ValidatorEntry(_Validators.path)
    return _ValidatorEntry(_Validators.str)

_VALIDATOR_MAP: Final[MappingProxyType[ConfigKey, _ValidatorEntry]] = MappingProxyType({
    k: _get_validator_for_key(k) for k in ConfigKey
})

def validate(raw_values: Any) -> AppSettings:
    """Valida iterativamente el diccionario bruto; los valores inválidos son omitidos."""
    if not _is_dict(raw_values): return dict(DEFAULTS)
    config = dict(DEFAULTS)
    for key_str, raw_val in raw_values.items():
        if (key_enum := _KEY_TO_ENUM.get(key_str)):
            try:
                validated_val = _VALIDATOR_MAP[key_enum].func(key_enum, raw_val)
                if validated_val is not None: config[key_enum.value] = validated_val
            except (ValueError, TypeError, AttributeError, KeyError): continue
    return config # type: ignore

def _is_file_secure_to_read(st: os.stat_result) -> bool:
    """Comprueba permisos, metadatos y asegura que no es un enlace simbólico o vínculo duro peligroso."""
    try:
        if st.st_nlink != 1: return False
        if not stat.S_ISREG(st.st_mode): return False
        if st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IWGRP | stat.S_IWOTH): return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid(): return False
        if st.st_size == 0 or st.st_size > MAX_SETTINGS_SIZE: return False
        return True
    except (OSError, PermissionError, AttributeError, ValueError): return False

def _read_and_parse_json(file_path: Path) -> Optional[SettingsDict]:
    if not is_safe_to_modify(str(file_path)): return None
    try:
        with open(file_path, "rb") as f:
            try:
                st = os.fstat(f.fileno())
            except (OSError, PermissionError): return None
            if not _is_file_secure_to_read(st): return None
            content = f.read(MAX_SETTINGS_SIZE + 1)
            if not content or len(content) > MAX_SETTINGS_SIZE: return None
            data = json.loads(content.decode("utf-8"))
            return data if _is_dict(data) else None
    except (OSError, IOError, PermissionError, json.JSONDecodeError, UnicodeDecodeError, MemoryError):
        return None

def _coerce_and_verify(settings: AppSettings) -> AppSettings:
    """Asegura coherencia y tipos correctos entre los valores cargados y la lógica de seguridad."""
    final = dict(DEFAULTS)
    for key, val in settings.items():
        key_enum = ConfigKey(key) if key in _KEY_TO_ENUM else None
        if key_enum and val is not None and _is_valid_type(val, key_enum):
            final[key] = val
    if final.get("asistente_activado") and not (final.get("asistente_clave_api") or os.environ.get(API_KEY_ENV_VAR)):
        final["asistente_activado"] = False
    return final # type: ignore

def load(custom_base: PathLike | None = None) -> AppSettings:
    primary_path = settings_path(custom_base)
    try:
        if primary_path.exists() and primary_path.is_file():
            st = primary_path.stat()
            cache_key = str(primary_path)
            cached = _MANAGER.settings_cache.get(cache_key)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                return cached[2].copy() # type: ignore
            
            data = _read_and_parse_json(primary_path)
            if data is not None:
                settings = _coerce_and_verify(validate(data))
                _MANAGER.settings_cache[cache_key] = (st.st_mtime, st.st_size, settings)
                return settings
    except (OSError, PermissionError): pass
    return dict(DEFAULTS) # type: ignore

def settings_path(custom_base: PathLike | None = None) -> Path:
    cache_key = str(custom_base) if custom_base else None
    if cache_key in _MANAGER.path_cache:
        return _MANAGER.path_cache[cache_key]
    try:
        raw_base = Path(custom_base).resolve() if custom_base else Path(SETTINGS_DIR).resolve()
        if _Validators._is_safe_path(str(raw_base)) and not _Validators._is_reparse_point(str(raw_base)):
            if not raw_base.exists(): 
                try: raw_base.mkdir(parents=True, exist_ok=True)
                except (OSError, PermissionError): return SETTINGS_DIR / SETTINGS_FILE
            if raw_base.is_dir() and _is_dir_safe(raw_base):
                full_path = raw_base / SETTINGS_FILE
                _MANAGER.path_cache[cache_key] = full_path
                return full_path
    except (OSError, RuntimeError, PermissionError, ValueError): pass
    return SETTINGS_DIR / SETTINGS_FILE

def save(values: Any, custom_base: PathLike | None = None) -> Optional[Path]:
    if not _is_dict(values): return None
    config_path = settings_path(custom_base)
    try:
        cleaned_settings = _coerce_and_verify(validate(values))
        if not is_safe_to_modify(str(config_path)): return None
        try:
            serialized = json.dumps(cleaned_settings, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
        if len(serialized.encode("utf-8")) > MAX_SETTINGS_SIZE: return None
        return config_path if _MANAGER.perform_atomic_write(config_path, serialized) else None
    except (OSError, PermissionError, AttributeError, TypeError): return None

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    if not changes or not _is_dict(changes): return load(custom_base)
    current = load(custom_base)
    modified = False
    for k, v in changes.items():
        if (key_enum := _KEY_TO_ENUM.get(k)):
            try:
                val = _VALIDATOR_MAP[key_enum].func(key_enum, v)
                if val is not None and val != current.get(k):
                    current[k] = val
                    modified = True
            except (ValueError, TypeError, KeyError): continue
    if modified: save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    config_path = settings_path(custom_base)
    if is_safe_to_modify(str(config_path)):
        save(DEFAULTS, custom_base)
        _MANAGER.clear()
    return dict(DEFAULTS) # type: ignore

def get(key: str, custom_base: PathLike | None = None) -> Any:
    return load(custom_base).get(key)

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    if env_key := os.environ.get(API_KEY_ENV_VAR, "").strip(): return env_key
    return str(load(custom_base).get("asistente_clave_api", "")).strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    if os.environ.get(API_KEY_ENV_VAR): return True
    settings = load(custom_base)
    return bool(settings.get("asistente_activado")) and bool(str(settings.get("asistente_clave_api", "")).strip())

def describe(custom_base: PathLike | None = None) -> list[str]:
    current = load(custom_base)
    api_key_env = os.environ.get(API_KEY_ENV_VAR)
    api_key_file = str(current.get("asistente_clave_api", "")).strip()
    origin = f"variable de entorno {API_KEY_ENV_VAR}" if api_key_env else ("archivo de configuración" if api_key_file else "no configurada")
    return [
        "Configuración actual", "", f"  Archivo: {settings_path(custom_base)}", "",
        "  Apariencia", f"    Tema: {current.get('tema')}", f"    Acento: {current.get('acento')}",
        f"    Barras visuales: {'sí' if current.get('mostrar_barras') else 'no'}", "",
        "  Comportamiento", f"    Confirmar siempre: {'sí' if current.get('confirmar_siempre') else 'no'}",
        f"    Pestaña inicial: {current.get('abrir_en')}", f"    Recordar carpeta: {'sí' if current.get('recordar_ultima_carpeta') else 'no'}", "",
        "  Rendimiento", f"    Duplicados desde: {current.get('duplicados_tamano_minimo_kb')} KB",
        f"    Top de archivos: {current.get('top_archivos')}", f"    Análisis en paralelo: {'sí' if current.get('analisis_en_paralelo') else 'no'}", "",
        "  Asistente IA", f"    Activado: {'sí' if current.get('asistente_activado') else 'no'}",
        f"    Clave: {origin}", f"    Modelo: {current.get('asistente_modelo')}", ""
    ]
