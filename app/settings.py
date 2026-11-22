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
import platform
import ctypes
import hashlib
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeAlias, Callable, TypedDict, Optional, TypeVar, ParamSpec, NamedTuple, TypeGuard
from functools import lru_cache
from collections import deque

from safety import is_safe_to_modify, is_protected_path

PathLike: TypeAlias = str | Path
SettingsDict: TypeAlias = dict[str, Any]
ConfigMapping: TypeAlias = dict[str, Any]
NumericRange: TypeAlias = tuple[int, int]
T = TypeVar("T")
P = ParamSpec("P")

# Caché interna: map[path_str, (data, mtime, size, inode, hash)]
CacheEntry: TypeAlias = tuple[MappingProxyType[str, Any], float, int, int, str]

class ConfigKey(Enum):
    """Enumeración de todas las claves permitidas en el esquema de configuración."""
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
    """Esquema de tipos para las preferencias persistidas de la aplicación."""
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
    """Define los límites [min, max] para validar valores numéricos configurables."""
    min: int
    max: int

class _ValidatorEntry:
    """Wrapper para delegar la validación de una clave a su función correspondiente."""
    def __init__(self, func: Callable[[ConfigKey, Any], Any]) -> None:
        self.func = func

class _ValidationResult(TypedDict):
    """Estructura de resultado para normalizaciones que requieren reporte de éxito."""
    is_valid: bool
    value: Any

def _is_dict(val: Any) -> TypeGuard[SettingsDict]:
    """Validador de tipo para asegurar que el objeto sea un contenedor de settings."""
    return isinstance(val, dict)

__all__ = [
    "DEFAULTS", "SETTINGS_DIR", "SETTINGS_FILE", "API_KEY_ENV_VAR",
    "VALID_THEMES", "VALID_ACCENTS", "settings_path", "load", "save",
    "update", "reset", "validate", "get", "assistant_api_key",
    "assistant_enabled", "describe",
]

def _get_default_settings_dir() -> Path:
    """Obtiene la ruta predeterminada de configuración, fallback a CWD si falla home."""
    try:
        return Path.home() / "LimpiezaTotalOmega"
    except (OSError, RuntimeError):
        return Path(os.getcwd()) / "LimpiezaTotalOmega"

SETTINGS_DIR: Final = _get_default_settings_dir()
SETTINGS_FILE: Final = "config.json"
MAX_SETTINGS_SIZE: Final = 1024 * 64
MIN_SETTINGS_SIZE: Final = 32
API_KEY_ENV_VAR: Final = "OMEGA_GEMINI_KEY"

_BOOL_TRUE_SET: Final[tuple[str, ...]] = ("1", "true", "si", "sí", "yes")
_BOOL_FALSE_SET: Final[tuple[str, ...]] = ("0", "false", "no", "none")

VALID_THEMES: Final[frozenset[str]] = frozenset(("oscuro", "claro", "sistema"))
VALID_ACCENTS: Final[frozenset[str]] = frozenset(("menta", "violeta", "magenta", "cian", "ambar"))
VALID_MODELS: Final[frozenset[str]] = frozenset(("gemini-3.1-flash-lite", "gemini-3.1-pro"))

_KEY_TO_ENUM: Final[dict[str, ConfigKey]] = {k.value: k for k in ConfigKey}
_VALID_ENUM_KEYS: Final[frozenset[str]] = frozenset(_KEY_TO_ENUM.keys())

DEFAULTS: Final[AppSettings] = {
    "tema":                        "oscuro",
    "acento":                      "menta",
    "mostrar_barras":              True,
    "animaciones":                 True,
    "confirmar_siempre":           True,
    "abrir_en":                    "Salud",
    "recordar_ultima_carpeta":     True,
    "ultima_carpeta":              "",
    "duplicados_tamano_minimo_kb": 64,
    "top_archivos":                15,
    "top_procesos":                15,
    "analisis_en_paralelo":        True,
    "asistente_activado":          False,
    "asistente_clave_api":         "",
    "asistente_enviar_metricas":   True,
    "asistente_modelo":            "gemini-3.1-flash-lite",
}

_DEFAULT_TYPES: Final[dict[str, type]] = {k: type(v) for k, v in DEFAULTS.items()}

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

class _SettingsManager:
    """
    Gestiona la persistencia en disco mediante escritura atómica y mantiene
    una caché LRU de configuraciones para minimizar operaciones de I/O.
    """
    def __init__(self) -> None:
        self.data_cache: dict[str, CacheEntry] = {}
        self.cache_order: deque[str] = deque()
        self.max_cache_size: int = 8

    def _has_free_space(self, path: Path) -> bool:
        """Verifica disponibilidad de espacio en disco usando syscalls del sistema."""
        try:
            target = path if path.is_dir() else path.parent
            if platform.system() == "Windows":
                free_bytes = ctypes.c_ulonglong(0)
                if not ctypes.windll.kernel32.GetDiskFreeSpaceExW(str(target.anchor), None, None, ctypes.pointer(free_bytes)):
                    return False
                return free_bytes.value > MAX_SETTINGS_SIZE
            else:
                st = os.statvfs(target)
                return (st.f_bavail * st.f_frsize) > MAX_SETTINGS_SIZE
        except (OSError, AttributeError, ValueError):
            return False

    def clear(self, resolved_path_str: str) -> None:
        """Elimina una entrada específica de la caché interna para forzar recarga."""
        if resolved_path_str in self.data_cache:
            del self.data_cache[resolved_path_str]
            if resolved_path_str in self.cache_order:
                self.cache_order.remove(resolved_path_str)

    def perform_atomic_write(self, config_path: Path, serialized: str) -> bool:
        """
        Garantiza la persistencia segura mediante escritura en archivo temporal y
        sustitución atómica (os.replace). Verifica inodos, permisos, ausencia de
        enlaces simbólicos y disponibilidad de espacio para evitar corrupción.
        """
        if any(c in str(config_path) for c in ("\0", "\r", "\n", "\033")): return False
        serialized_bytes = serialized.encode("utf-8")
        if not (MIN_SETTINGS_SIZE <= len(serialized_bytes) <= MAX_SETTINGS_SIZE): return False
        
        fd = -1
        temp_path = None
        try:
            resolved_path = config_path.resolve()
            parent_dir = resolved_path.parent
            if not parent_dir.exists() or not parent_dir.is_dir(): return False
            
            # Verificación de seguridad y estado de disco
            if is_protected_path(str(resolved_path)) or is_protected_path(str(parent_dir)): return False
            if not is_safe_to_modify(str(resolved_path)) or not is_safe_to_modify(str(parent_dir)): return False
            if not self._has_free_space(parent_dir): return False
            if not os.access(parent_dir, os.W_OK): return False
            
            # Seguridad adicional contra sustitución
            if resolved_path.exists():
                st_orig = resolved_path.stat()
                # Verificar permisos, enlaces y tipo de archivo
                if st_orig.st_nlink != 1 or not stat.S_ISREG(st_orig.st_mode) or (st_orig.st_mode & 0o111): return False
                if platform.system() != "Windows":
                    try:
                        if st_orig.st_uid != os.getuid(): return False
                    except (AttributeError, OSError): pass
            
            temp_path = parent_dir / f".tmp_{os.getpid()}_{hashlib.md5(str(resolved_path).encode()).hexdigest()}"
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_EXCL, 0o600)
            
            os.write(fd, serialized_bytes)
            os.fsync(fd)
            
            if os.fstat(fd).st_size != len(serialized_bytes): return False
            
            os.close(fd)
            fd = -1
            
            os.replace(temp_path, resolved_path)
            self.clear(str(resolved_path))
            return True
        except (OSError, PermissionError, IOError):
            return False
        finally:
            if fd != -1: 
                try: os.close(fd)
                except OSError: pass
            if temp_path and temp_path.exists():
                try: os.remove(temp_path)
                except OSError: pass

_MANAGER = _SettingsManager()

def type_check(func: Callable[P, T | None]) -> Callable[P, T | None]:
    """Decorador robusto que captura errores de conversión para permitir validación suave."""
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        try:
            return func(*args, **kwargs)
        except (ValueError, TypeError, AttributeError, OverflowError, KeyError):
            return None
    return wrapper

class _Validators:
    """
    Colección de utilidades para normalizar tipos y validar rutas.
    Implementa verificaciones contra puntos de reparse (reparse points) y
    cadenas maliciosas para asegurar que la configuración solo se lea/escriba
    en ubicaciones seguras del sistema de archivos.
    """
    @staticmethod
    def _parse_as_type(val: Any, target_type: type[T]) -> Optional[T]:
        """Intenta convertir un valor crudo al tipo base esperado."""
        if val is None: return None
        try:
            converted = target_type(val)
            return converted if isinstance(converted, target_type) else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    @lru_cache(maxsize=32)
    def _is_reparse_point(path_str: str) -> bool:
        """Detecta symlinks o reparse points para evitar escapes de directorio."""
        try:
            st = os.lstat(path_str)
            if stat.S_ISLNK(st.st_mode): return True
            return bool(getattr(st, 'st_file_attributes', 0) & 0x400)
        except (OSError, PermissionError):
            return True

    @staticmethod
    @lru_cache(maxsize=256)
    def _run_safety_checks(path_str: str) -> bool:
        """Verifica que la ruta esté saneada y no apunte a sectores protegidos."""
        try:
            p = Path(path_str)
            if not p.is_absolute() or len(path_str) > 260: return False
            
            if is_protected_path(path_str): return False
            
            for parent in p.parents:
                if _Validators._is_reparse_point(str(parent)): return False
            
            return is_safe_to_modify(path_str)
        except (OSError, RuntimeError, PermissionError, ValueError):
            return False

    @staticmethod
    def _check_path_safety(p: Path) -> bool:
        """Valida que una instancia Path cumpla con los requisitos de seguridad."""
        return _Validators._run_safety_checks(str(p.resolve()))

    @staticmethod
    @lru_cache(maxsize=64)
    def _is_safe_path(path_str: str) -> bool:
        """Sanitiza strings de ruta antes de convertirlos a objetos Path."""
        if not path_str or len(path_str) > 260 or any(c in path_str for c in ("\0", "\r", "\n", "\033")): return False
        if path_str.startswith(("\\\\", "//")) or ".." in path_str: return False
        try:
            return _Validators._check_path_safety(Path(path_str))
        except (OSError, RuntimeError, PermissionError, AttributeError, ValueError):
            return False

    @staticmethod
    def bool(key: ConfigKey, val: Any) -> Optional[bool]:
        """Normaliza entradas a booleanos estrictos desde diversos formatos."""
        if isinstance(val, bool): return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in _BOOL_TRUE_SET: return True
            if normalized in _BOOL_FALSE_SET: return False
        return None

    @staticmethod
    @type_check
    def int(key: ConfigKey, val: Any) -> Optional[int]:
        """Valida valores numéricos forzándolos dentro del rango definido para su clave."""
        parsed_value = int(val)
        limit = _NUMERIC_LIMITS.get(key)
        if limit: return max(limit.min, min(limit.max, parsed_value))
        return parsed_value

    @staticmethod
    def path(key: ConfigKey, val: Any) -> Optional[str]:
        """Valida que un string sea una ruta absoluta, segura y no un riesgo."""
        if val == "": return ""
        if not isinstance(val, str): return None
        path_string = val.strip()
        if not path_string or "\0" in path_string or len(path_string) > 260: return None
        if not Path(path_string).is_absolute(): return None
        return path_string if _Validators._is_safe_path(path_string) else None

    @staticmethod
    def _validate_enum_str(text: str, key: ConfigKey) -> Optional[str]:
        """Comprueba que una cadena pertenezca al conjunto de valores permitidos."""
        val = text.lower()
        allowed = _ENUM_VALS.get(key)
        if allowed: return val if val in allowed else None
        return text if len(text) <= 512 else None

    @staticmethod
    @type_check
    def str(key: ConfigKey, val: Any) -> Optional[str]:
        """Normaliza y valida strings generales para prevenir inyecciones."""
        if not isinstance(val, str): return None
        text = val.strip()
        if not text or "\0" in text or any(ord(c) < 32 for c in text) or ".." in text or len(text) > 1024: return None
        return _Validators._validate_enum_str(text, key)

_VALIDATOR_MAP: Final[MappingProxyType[ConfigKey, _ValidatorEntry]] = MappingProxyType({
    ConfigKey.TEMA: _ValidatorEntry(_Validators.str),
    ConfigKey.ACENTO: _ValidatorEntry(_Validators.str),
    ConfigKey.MOSTRAR_BARRAS: _ValidatorEntry(_Validators.bool),
    ConfigKey.ANIMACIONES: _ValidatorEntry(_Validators.bool),
    ConfigKey.CONFIRMAR_SIEMPRE: _ValidatorEntry(_Validators.bool),
    ConfigKey.ABRIR_EN: _ValidatorEntry(_Validators.str),
    ConfigKey.RECORDAR_ULTIMA_CARPETA: _ValidatorEntry(_Validators.bool),
    ConfigKey.ULTIMA_CARPETA: _ValidatorEntry(_Validators.path),
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB: _ValidatorEntry(_Validators.int),
    ConfigKey.TOP_ARCHIVOS: _ValidatorEntry(_Validators.int),
    ConfigKey.TOP_PROCESOS: _ValidatorEntry(_Validators.int),
    ConfigKey.ANALISIS_EN_PARALELO: _ValidatorEntry(_Validators.bool),
    ConfigKey.ASISTENTE_ACTIVADO: _ValidatorEntry(_Validators.bool),
    ConfigKey.ASISTENTE_CLAVE_API: _ValidatorEntry(_Validators.str),
    ConfigKey.ASISTENTE_ENVIAR_METRICAS: _ValidatorEntry(_Validators.bool),
    ConfigKey.ASISTENTE_MODELO: _ValidatorEntry(_Validators.str),
})

def validate(raw_values: Any) -> AppSettings:
    """Valida un objeto de entrada contra el esquema y aplica valores por defecto si falla."""
    if not _is_dict(raw_values): return DEFAULTS.copy()
    config = DEFAULTS.copy()
    for key_str, val in raw_values.items():
        key_enum = _KEY_TO_ENUM.get(key_str)
        if not key_enum: continue
        validator = _VALIDATOR_MAP.get(key_enum)
        if not validator: continue
        validated_val = validator.func(key_enum, val)
        if validated_val is not None:
            config[key_enum.value] = validated_val
    return config 

def _is_file_secure_to_read(file_path: Path, resolved_str: str, st: os.stat_result) -> bool:
    """Verifica integridad, permisos y sanitiza caracteres POSIX del archivo antes de la lectura."""
    try:
        # Check size and strict file type requirements
        if not (MIN_SETTINGS_SIZE <= st.st_size <= MAX_SETTINGS_SIZE): return False
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or (st.st_mode & 0o111): return False
        if any(c in resolved_str for c in ("\0", "\r", "\n", "\033")): return False
        if is_protected_path(resolved_str) or is_protected_path(str(file_path.parent)): return False
        if _Validators._is_reparse_point(str(file_path)): return False
        # Restrict permissions: must be owner-readable only (POSIX)
        if platform.system() != "Windows" and (st.st_mode & 0o077) != 0: return False
        try:
            uid = os.getuid()
        except (AttributeError, OSError):
            uid = -1
        if platform.system() != "Windows" and st.st_uid != uid: return False
        return True
    except (OSError, PermissionError, AttributeError, ValueError): return False

def _validate_structure(data: Any) -> bool:
    """Valida la integridad de la estructura JSON y los tipos de datos básicos."""
    if not isinstance(data, dict) or not data: return False
    for k, default_val in DEFAULTS.items():
        if k in data and data[k] is not None and not isinstance(data[k], type(default_val)):
            return False
    return True

def _read_and_parse_json(file_path: Path, resolved_str: str) -> Optional[tuple[SettingsDict, os.stat_result, str]]:
    """Carga y decodifica el JSON de configuración con verificaciones robustas."""
    try:
        st_initial = file_path.stat()
        if not _is_file_secure_to_read(file_path, resolved_str, st_initial): return None
        
        fd = os.open(file_path, os.O_RDONLY)
        with os.fdopen(fd, "rb") as f:
            # Validar que el archivo no cambió después del primer stat (TOCTOU)
            st_check = os.fstat(fd)
            if st_check.st_ino != st_initial.st_ino or st_check.st_mtime != st_initial.st_mtime: return None
            
            try:
                content = f.read()
                if not content: return None
                data = json.loads(content.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, MemoryError):
                return None
            if not _validate_structure(data): return None
            file_hash = hashlib.sha256(content).hexdigest()
            
        return data, st_check, file_hash
    except (OSError, PermissionError):
        return None

def _coerce_and_verify(settings: ConfigMapping) -> AppSettings:
    """Normaliza y coacciona el estado de las configuraciones para garantizar coherencia."""
    coerced = DEFAULTS.copy()
    if not isinstance(settings, dict): return coerced
    
    for key, value in settings.items():
        key_enum = _KEY_TO_ENUM.get(str(key))
        if not key_enum: continue
        
        validator = _VALIDATOR_MAP.get(key_enum)
        if not validator: continue
        
        try:
            validated_val = validator.func(key_enum, value)
            if validated_val is not None:
                coerced[key_enum.value] = validated_val
        except (ValueError, TypeError, AttributeError):
            continue
    
    # Reglas de coherencia de negocio post-validación
    if coerced.get("asistente_activado") and not (coerced.get("asistente_clave_api") or os.environ.get(API_KEY_ENV_VAR)):
        coerced["asistente_activado"] = False
    return coerced

def load(custom_base: PathLike | None = None) -> AppSettings:
    """Recupera la configuración, utilizando caché interna optimizada."""
    primary_path = settings_path(custom_base)
    try:
        resolved_str = str(primary_path.resolve())
        if not primary_path.exists():
            return DEFAULTS.copy()
            
        st = primary_path.stat()
        cached = _MANAGER.data_cache.get(resolved_str)
        if cached and cached[3] == st.st_ino and cached[1] == st.st_mtime and cached[2] == st.st_size:
            return dict(cached[0])
            
        result = _read_and_parse_json(primary_path, resolved_str)
        if result is not None:
            data, st_meta, f_hash = result
            if cached and cached[4] == f_hash:
                return dict(cached[0])
            
            settings = _coerce_and_verify(validate(data))
            if len(_MANAGER.data_cache) >= _MANAGER.max_cache_size:
                oldest = _MANAGER.cache_order.popleft()
                _MANAGER.data_cache.pop(oldest, None)
            
            _MANAGER.data_cache[resolved_str] = (MappingProxyType(settings), st_meta.st_mtime, st_meta.st_size, st_meta.st_ino, f_hash)
            _MANAGER.cache_order.append(resolved_str)
            return settings
    except (OSError, PermissionError): pass
    return DEFAULTS.copy()

def settings_path(custom_base: PathLike | None = None) -> Path:
    """Resuelve la ubicación segura del archivo de configuración."""
    try:
        target_path = custom_base if custom_base else SETTINGS_DIR
        if any(c in str(target_path) for c in ("\0", "\r", "\n", "\033")): return SETTINGS_DIR / SETTINGS_FILE
        target = Path(target_path).resolve()
        
        if not is_safe_to_modify(str(target)): return SETTINGS_DIR / SETTINGS_FILE
        
        if not target.exists():
            try: target.mkdir(parents=True, exist_ok=True)
            except (OSError, PermissionError): return SETTINGS_DIR / SETTINGS_FILE
        
        if not os.access(target, os.R_OK | os.W_OK) or _Validators._is_reparse_point(str(target)): 
            return SETTINGS_DIR / SETTINGS_FILE
            
        return target / SETTINGS_FILE
    except (OSError, RuntimeError, PermissionError, ValueError): pass
    return SETTINGS_DIR / SETTINGS_FILE

def save(values: Any, custom_base: PathLike | None = None) -> Optional[Path]:
    """Persiste los cambios de configuración de forma segura y atómica."""
    if not _is_dict(values): return None
    config_path = settings_path(custom_base)
    if not is_safe_to_modify(str(config_path)): return None
    try:
        cleaned_settings = _coerce_and_verify(validate(values))
        serialized = json.dumps(cleaned_settings, indent=2, ensure_ascii=False)
        serialized_bytes = serialized.encode("utf-8")
        if _MANAGER.perform_atomic_write(config_path, serialized):
            try:
                st = config_path.stat()
                f_hash = hashlib.sha256(serialized_bytes).hexdigest()
                r_str = str(config_path.resolve())
                if r_str not in _MANAGER.data_cache and len(_MANAGER.data_cache) >= _MANAGER.max_cache_size:
                    _MANAGER.data_cache.pop(_MANAGER.cache_order.popleft(), None)
                if r_str not in _MANAGER.data_cache:
                    _MANAGER.cache_order.append(r_str)
                _MANAGER.data_cache[r_str] = (MappingProxyType(cleaned_settings), st.st_mtime, st.st_size, st.st_ino, f_hash)
            except OSError: pass
            return config_path
        return None
    except (OSError, PermissionError, AttributeError, TypeError, ValueError): return None

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    """Actualiza parcialmente las configuraciones existentes."""
    if not changes or not _is_dict(changes): return load(custom_base)
    current = load(custom_base)
    modified = False
    for key_str, value in changes.items():
        key_enum = _KEY_TO_ENUM.get(key_str)
        if not key_enum: continue
        validator = _VALIDATOR_MAP.get(key_enum)
        if not validator: continue
        val = validator.func(key_enum, value)
        if val is not None and val != current.get(key_str):
            current[key_str] = val
            modified = True
    if modified: save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    """Restablece los valores a los de fábrica."""
    config_path = settings_path(custom_base)
    if is_safe_to_modify(str(config_path)):
        save(DEFAULTS, custom_base)
    return DEFAULTS.copy()

def get(key: str, custom_base: PathLike | None = None) -> Any:
    """Obtiene el valor de una clave específica."""
    settings = load(custom_base)
    return settings.get(key)

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    """Retorna la API key desde entorno o archivo local."""
    if env_key := os.environ.get(API_KEY_ENV_VAR, "").strip(): return env_key
    return str(load(custom_base).get("asistente_clave_api", "")).strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    """Verifica si el asistente está configurado y activo."""
    if os.environ.get(API_KEY_ENV_VAR): return True
    settings = load(custom_base)
    return bool(settings.get("asistente_activado")) and bool(str(settings.get("asistente_clave_api", "")).strip())

def describe(custom_base: PathLike | None = None) -> list[str]:
    """Genera diagnóstico descriptivo del estado actual del sistema."""
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
