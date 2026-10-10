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
import fcntl
import shutil
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeAlias, Callable, TypedDict, Optional, TypeVar, ParamSpec, NamedTuple, TypeGuard
from functools import lru_cache

from safety import is_safe_to_modify, is_protected_path, ensure_safe_to_modify

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

class _SettingsManager:
    """
    Singleton que centraliza el acceso a la caché de settings en memoria
    y rutas de configuración. Evita lecturas redundantes de disco.
    """
    def __init__(self) -> None:
        self.settings_cache: dict[str, tuple[float, AppSettings]] = {}
        self.path_cache: dict[Optional[str], Path] = {}

    def clear(self) -> None:
        """Invalidar caché tras modificaciones de persistencia."""
        self.settings_cache.clear()

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
    """Namespace que centraliza la lógica de validación de entradas de configuración."""

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        """Detecta si la ruta es un punto de reparse (symlink o junction) para evitar traversal."""
        try:
            return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
        except (OSError, PermissionError):
            return True

    @staticmethod
    @lru_cache(maxsize=128)
    def _run_safety_checks(path_str: str) -> bool:
        """Verifica que una cadena de ruta sea segura, resolviéndola y aplicando chequeos de `safety.py`."""
        try:
            p = Path(path_str).expanduser()
            if not p.is_absolute(): return False
            resolved = p.resolve()
            for part in resolved.parts:
                if _Validators._is_reparse_point(Path(part)):
                    return False
            return not is_protected_path(str(resolved)) and is_safe_to_modify(str(resolved))
        except (OSError, PermissionError, RuntimeError, IndexError):
            return False

    @staticmethod
    def _check_path_safety(p: Path) -> bool:
        """Wrapper que valida la absoluta y los chequeos de seguridad de `safety.py`."""
        return p.is_absolute() and _Validators._run_safety_checks(str(p))

    @staticmethod
    def _is_safe_path(path_str: str) -> bool:
        """Valida que la cadena de ruta no sea maliciosa (caracteres especiales, UNC) y sea segura."""
        if not path_str or len(path_str) > 2048 or any(c in path_str for c in ("\0", "^", "\033")): return False
        if path_str.startswith(("\\\\", "//")): return False
        try:
            return _Validators._check_path_safety(Path(path_str))
        except (OSError, RuntimeError, PermissionError, AttributeError, ValueError):
            return False

    @staticmethod
    def bool(key: ConfigKey, val: Any) -> Optional[bool]:
        """Normaliza valores a booleano, permitiendo varias formas de representación (ej. "si", "false")."""
        if isinstance(val, bool): return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in _BOOL_TRUE_SET: return True
            if normalized in _BOOL_FALSE_SET: return False
        return None

    @staticmethod
    @type_check
    def int(key: ConfigKey, val: Any) -> Optional[int]:
        """Convierte a entero y asegura que esté dentro de los rangos definidos en _NUMERIC_LIMITS."""
        if val is None: return None
        parsed_value = int(val)
        limit = _NUMERIC_LIMITS.get(key)
        if limit: return max(limit.min, min(limit.max, parsed_value))
        return parsed_value

    @staticmethod
    def path(key: ConfigKey, val: Any) -> Optional[str]:
        """Valida que una ruta sea un string seguro para el motor de organización."""
        if val == "": return ""
        if not isinstance(val, str): return None
        path_string = val.strip()
        if not path_string or "\0" in path_string or len(path_string) > 2048: return None
        return path_string if _Validators._is_safe_path(path_string) else None

    @staticmethod
    def _validate_enum_str(text: str, key: ConfigKey) -> Optional[str]:
        """Verifica que el string pertenezca al conjunto de valores permitidos para campos con opciones fijas."""
        val = text.lower()
        allowed = _ENUM_VALS.get(key)
        if allowed: return val if val in allowed else None
        return text if len(text) <= 512 else None

    @staticmethod
    @type_check
    def str(key: ConfigKey, val: Any) -> Optional[str]:
        """Limpia cadenas de texto, evitando caracteres de control o directorios padre '..'."""
        if val is None: return None
        text = str(val).strip()
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

def _determine_validator(key: ConfigKey) -> Callable[[ConfigKey, Any], Any]:
    """Asigna la función de validación adecuada según el tipo de clave."""
    if key in BOOL_KEYS: return _Validators.bool
    if key in INT_KEYS: return _Validators.int
    if key == ConfigKey.ULTIMA_CARPETA: return _Validators.path
    return _Validators.str

@lru_cache(maxsize=1)
def _build_validator_map() -> MappingProxyType[ConfigKey, _ValidatorEntry]:
    """Genera el mapa centralizado de validadores para toda la configuración."""
    return MappingProxyType({key: _ValidatorEntry(_determine_validator(key)) for key in ConfigKey})

def settings_path(custom_base: PathLike | None = None) -> Path:
    """
    Retorna la ruta absoluta al archivo de configuración.
    Garantiza que el directorio de configuración sea seguro antes de intentar su uso.
    """
    cache_key = str(custom_base) if custom_base else None
    if cache_key in _MANAGER.path_cache:
        return _MANAGER.path_cache[cache_key]
    
    base_path = (Path(custom_base).resolve() if custom_base else SETTINGS_DIR)
    
    try:
        if _Validators._is_safe_path(str(base_path)) and not _Validators._is_reparse_point(base_path):
            if not base_path.exists():
                base_path.mkdir(parents=True, exist_ok=True)
            if base_path.is_dir() and os.access(base_path, os.R_OK | os.W_OK):
                full_path = base_path / SETTINGS_FILE
                _MANAGER.path_cache[cache_key] = full_path
                return full_path
    except (OSError, RuntimeError, PermissionError):
        pass
    return SETTINGS_DIR / SETTINGS_FILE

def validate(raw_values: Any) -> AppSettings:
    """Valida y limpia una estructura de datos externa contra el esquema oficial."""
    if not _is_dict(raw_values): 
        return DEFAULTS.copy()
    
    config = DEFAULTS.copy()
    validators = _build_validator_map()
    
    for key_str, raw_val in raw_values.items():
        try:
            if (key_enum := _KEY_TO_ENUM.get(key_str)):
                validated_val = validators[key_enum].func(key_enum, raw_val)
                if validated_val is not None:
                    config[key_enum.value] = validated_val
        except (AttributeError, KeyError, Exception):
            continue
    return config # type: ignore

def _is_file_secure_to_read(file_obj: Any) -> bool:
    """
    Verifica mediante fstat que el archivo de configuración sea un archivo regular
    propiedad del usuario y sin permisos de ejecución, previniendo lectura de
    ficheros sensibles o enlaces peligrosos.
    """
    try:
        if file_obj.closed: return False
        st = os.fstat(file_obj.fileno())
        mode = st.st_mode
        if not stat.S_ISREG(mode): return False
        if st.st_size == 0 or st.st_size > MAX_SETTINGS_SIZE: return False
        if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IWGRP | stat.S_IWOTH): return False
        if mode & (stat.S_ISUID | stat.S_ISGID): return False
        if st.st_nlink != 1: return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid(): return False
        return True
    except (OSError, PermissionError, AttributeError, ValueError):
        return False

def _read_and_parse_json(file_path: Path) -> Optional[SettingsDict]:
    """Helper que encapsula la apertura, lock y lectura segura del archivo."""
    try:
        ensure_safe_to_modify(str(file_path.resolve()))
        with open(file_path, "r", encoding="utf-8") as f:
            if not _is_file_secure_to_read(f): return None
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                content = f.read(MAX_SETTINGS_SIZE + 1)
                if not content: return None
                data = json.loads(content)
                return data if _is_dict(data) else None
            finally:
                try: fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except (OSError, IOError): pass
    except (OSError, PermissionError, IOError, json.JSONDecodeError, ValueError, AttributeError, RuntimeError):
        return None

def _is_dir_safe(path: Path) -> bool:
    """Verifica seguridad básica del directorio padre antes de operaciones de escritura."""
    try:
        st = path.stat()
        if not stat.S_ISDIR(st.st_mode): return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid(): return False
        return True
    except OSError: return False

def _load_impl(ruta: Path) -> AppSettings:
    """Implementación de carga: valida seguridad y parsea el contenido."""
    if not ruta.is_file() or ruta.stat().st_size == 0 or _Validators._is_reparse_point(ruta):
        return DEFAULTS.copy()
    
    data = _read_and_parse_json(ruta.resolve())
    return _coerce_and_verify(validate(data)) if data else DEFAULTS.copy()

def load(custom_base: PathLike | None = None) -> AppSettings:
    """Carga los ajustes desde el disco, utilizando caché por mtime."""
    primary_path = settings_path(custom_base)
    backup_path = primary_path.with_suffix(".bak")
    
    for path in [primary_path, backup_path]:
        if path.is_file():
            try:
                mtime = os.path.getmtime(path)
                cache_key = str(path)
                cached = _MANAGER.settings_cache.get(cache_key)
                if cached and cached[0] == mtime:
                    return cached[1].copy()
                
                settings = _load_impl(path)
                _MANAGER.settings_cache[cache_key] = (mtime, settings)
                return settings.copy()
            except (OSError, PermissionError):
                continue
    return DEFAULTS.copy()

def _coerce_and_verify(settings: AppSettings) -> AppSettings:
    """
    Asegura consistencia de tipos y reglas de negocio, revertiendo a defaults ante inconsistencias.
    """
    final: AppSettings = DEFAULTS.copy()
    for key, val in settings.items():
        if key in DEFAULTS and isinstance(val, type(DEFAULTS[key])):
            final[key] = val # type: ignore
        
    if final["asistente_activado"] and not (final["asistente_clave_api"] or os.environ.get(API_KEY_ENV_VAR)):
        final["asistente_activado"] = False
    return final

def save(values: Any, custom_base: PathLike | None = None) -> Optional[Path]:
    """
    Persistencia atómica: Valida el estado, usa archivo temporal, fsync y reemplazo.
    """
    if not _is_dict(values): return None
    config_path = settings_path(custom_base)
    cleaned_settings = _coerce_and_verify(validate(values))
    
    parent = config_path.parent
    try:
        if not _is_dir_safe(parent) or _Validators._is_reparse_point(parent) or is_protected_path(str(parent.resolve())): return None
        ensure_safe_to_modify(str(parent.resolve()))
        usage = shutil.disk_usage(parent)
        if usage.free < MAX_SETTINGS_SIZE * 2 or not os.access(parent, os.W_OK): return None
        serialized = json.dumps(cleaned_settings, indent=2, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > MAX_SETTINGS_SIZE: return None
        if config_path.exists():
            ensure_safe_to_modify(str(config_path.resolve()))
    except (TypeError, ValueError, OSError, PermissionError, AttributeError): return None
    
    temp_path = config_path.with_suffix(".tmp")
    bak_path = config_path.with_suffix(".bak")
    dir_fd = None
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
            if not _is_file_secure_to_read(f):
                raise PermissionError("Archivo temporal inseguro")
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        
        if config_path.exists():
            ensure_safe_to_modify(str(bak_path.resolve()))
            os.replace(config_path, bak_path)
        
        os.replace(temp_path, config_path)
        
        dir_fd = os.open(str(parent), os.O_RDONLY)
        os.fsync(dir_fd)
        _MANAGER.clear()
        return config_path
    except (OSError, IOError, PermissionError, TypeError): 
        return None
    finally:
        if dir_fd is not None:
            try: os.close(dir_fd)
            except OSError: pass
        if temp_path.exists():
            try: os.remove(temp_path)
            except OSError: pass

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    """Actualiza campos específicos en la configuración y persiste solo si hay cambios."""
    if not changes or not _is_dict(changes):
        return load(custom_base)
        
    current = load(custom_base)
    modified = False
    validators = _build_validator_map()
    for k, v in changes.items():
        if (key_enum := _KEY_TO_ENUM.get(k)):
            val = validators[key_enum].func(key_enum, v)
            if val is not None and val != current.get(k):
                current[k] = val
                modified = True
    if modified: 
        save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    """Restaura los valores predeterminados y limpia la caché."""
    save(DEFAULTS, custom_base)
    _MANAGER.clear()
    return DEFAULTS.copy()

def get(key: str, custom_base: PathLike | None = None) -> Any:
    """Acceso rápido a una configuración individual."""
    return load(custom_base).get(key)

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    """Recupera la clave API, priorizando la variable de entorno sobre la persistida."""
    if env_key := os.environ.get(API_KEY_ENV_VAR, "").strip(): return env_key
    return str(load(custom_base).get("asistente_clave_api", "")).strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    """Verifica si el asistente tiene permiso para activarse (flag y clave presentes)."""
    if os.environ.get(API_KEY_ENV_VAR): return True
    settings = load(custom_base)
    return bool(settings.get("asistente_activado")) and bool(str(settings.get("asistente_clave_api", "")).strip())

def describe(custom_base: PathLike | None = None) -> list[str]:
    """Reporte de estado de la configuración para propósitos de logging o UI."""
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
