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
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeAlias, Callable, TypedDict, Optional, TypeVar, ParamSpec, NamedTuple, TypeGuard
from functools import lru_cache

from safety import is_safe_to_modify, is_protected_path, UnsafePathError, ensure_safe_to_modify

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
    """Define los umbrales seguros para valores numéricos configurables."""
    min: int
    max: int

class _ValidatorEntry(NamedTuple):
    """Empaqueta la lógica de validación para una clave de configuración específica."""
    func: Callable[[ConfigKey, Any], Any]

def _is_dict(val: Any) -> TypeGuard[SettingsDict]:
    """Verifica que el objeto sea un diccionario válido para la configuración."""
    return isinstance(val, dict)

def _is_app_settings(val: Any) -> TypeGuard[AppSettings]:
    """Verifica que la estructura cumpla con el esquema AppSettings."""
    if not isinstance(val, dict): return False
    return all(key.value in val for key in ConfigKey)

__all__ = [
    "DEFAULTS", "SETTINGS_DIR", "SETTINGS_FILE", "API_KEY_ENV_VAR",
    "VALID_THEMES", "VALID_ACCENTS", "settings_path", "load", "save",
    "update", "reset", "validate", "get", "assistant_api_key",
    "assistant_enabled", "describe",
]

# Ubicación base donde se almacena el estado persistente del usuario
SETTINGS_DIR: Final = Path("~/LimpiezaTotalOmega").expanduser()
SETTINGS_FILE: Final = "config.json"
# Límite de seguridad: archivos demasiado grandes podrían indicar corrupción o ataques
MAX_SETTINGS_SIZE: Final = 1024 * 64
API_KEY_ENV_VAR: Final = "OMEGA_GEMINI_KEY"

# Cache de configuración en memoria: evita parseo de JSON redundante
_CACHE: dict[Path, tuple[float, AppSettings]] = {}
_PATH_CACHE: dict[Path, Path] = {}

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

def type_check(func: Callable[P, T | None]) -> Callable[P, T | None]:
    """Decorador: Filtra llamadas y captura excepciones críticas de conversión."""
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        try:
            return func(*args, **kwargs)
        except (ValueError, TypeError, AttributeError, OverflowError, KeyError):
            return None
    return wrapper

class _Validators:
    """Namespace de validadores estáticos para asegurar la integridad de la configuración."""

    @staticmethod
    @lru_cache(maxsize=64)
    def _is_reparse_point(path: Path) -> bool:
        try:
            return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
        except (OSError, PermissionError):
            return True

    @staticmethod
    @lru_cache(maxsize=128)
    def _run_safety_checks(path_str: str) -> bool:
        try:
            path_obj = Path(path_str).expanduser()
            for part in path_obj.parts:
                if _Validators._is_reparse_point(Path(part)):
                    return False
            resolved = path_obj.resolve(strict=False)
            if not is_protected_path(str(resolved)):
                return is_safe_to_modify(str(resolved))
            return False
        except (OSError, PermissionError, RuntimeError, UnsafePathError, IndexError):
            return False

    @staticmethod
    def _is_safe_path(path_str: str) -> bool:
        if not path_str or len(path_str) > 2048 or "\0" in path_str: return False
        if path_str.startswith(("\\\\", "//")): return False
        try:
            p = Path(path_str).expanduser()
            if not p.is_absolute(): return False
            return _Validators._run_safety_checks(str(p))
        except (OSError, RuntimeError, PermissionError, AttributeError, ValueError):
            return False

    @staticmethod
    def bool(key: ConfigKey, val: Any) -> Optional[bool]:
        if isinstance(val, bool): return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in ("1", "true", "si", "sí", "yes"): return True
            if normalized in ("0", "false", "no", "none"): return False
        return None

    @staticmethod
    @type_check
    def int(key: ConfigKey, val: Any) -> Optional[int]:
        if val is None: return None
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
        if val is None: return None
        text = str(val).strip()
        if not text or "\0" in text or any(ord(c) < 32 for c in text) or ".." in text or len(text) > 1024: return None
        if key == ConfigKey.ULTIMA_CARPETA: return _Validators.path(key, text)
        return _Validators._validate_enum_str(text, key)

_KEY_VALIDATOR_MAP: Final = MappingProxyType({
    ConfigKey.MOSTRAR_BARRAS: _Validators.bool,
    ConfigKey.ANIMACIONES: _Validators.bool,
    ConfigKey.CONFIRMAR_SIEMPRE: _Validators.bool,
    ConfigKey.RECORDAR_ULTIMA_CARPETA: _Validators.bool,
    ConfigKey.ANALISIS_EN_PARALELO: _Validators.bool,
    ConfigKey.ASISTENTE_ACTIVADO: _Validators.bool,
    ConfigKey.ASISTENTE_ENVIAR_METRICAS: _Validators.bool,
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB: _Validators.int,
    ConfigKey.TOP_ARCHIVOS: _Validators.int,
    ConfigKey.TOP_PROCESOS: _Validators.int,
    ConfigKey.ULTIMA_CARPETA: _Validators.path,
})

@lru_cache(maxsize=1)
def _get_validator_for_key(key: ConfigKey) -> _ValidatorEntry:
    return _ValidatorEntry(_KEY_VALIDATOR_MAP.get(key, _Validators.str))

@lru_cache(maxsize=1)
def _build_validator_map() -> MappingProxyType[ConfigKey, _ValidatorEntry]:
    return MappingProxyType({k: _get_validator_for_key(k) for k in ConfigKey})

_VALIDATOR_MAP: Final = _build_validator_map()

def settings_path(custom_base: PathLike | None = None) -> Path:
    if custom_base is None: return SETTINGS_DIR / SETTINGS_FILE
    base_path = Path(custom_base).expanduser()
    if base_path in _PATH_CACHE: return _PATH_CACHE[base_path]
    try:
        resolved_parent = base_path.resolve()
        if _Validators._is_safe_path(str(resolved_parent)):
            _PATH_CACHE[base_path] = resolved_parent / SETTINGS_FILE
            return _PATH_CACHE[base_path]
    except (OSError, RuntimeError, PermissionError):
        pass
    return SETTINGS_DIR / SETTINGS_FILE

def validate(raw_values: Any) -> AppSettings:
    config = DEFAULTS.copy()
    if not _is_dict(raw_values): return config
    for key_str, raw_val in raw_values.items():
        key_enum = _KEY_TO_ENUM.get(key_str)
        if key_enum and key_enum in _VALIDATOR_MAP:
            validator = _VALIDATOR_MAP[key_enum].func
            if (validated_val := validator(key_enum, raw_val)) is not None:
                config[key_enum.value] = validated_val
    return config

def load(custom_base: PathLike | None = None) -> AppSettings:
    ruta = settings_path(custom_base)
    candidates = [ruta, ruta.with_suffix(".bak")]
    
    for r in candidates:
        if not r.exists() or not os.access(r, os.R_OK): continue
        try:
            stats = r.stat()
            cached = _CACHE.get(r)
            if cached and cached[0] == stats.st_mtime:
                return cached[1].copy()
            if stats.st_size == 0 or stats.st_size > MAX_SETTINGS_SIZE:
                continue
            with open(r, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if not _is_dict(raw): continue
                final_data = _ensure_settings_integrity(validate(raw))
            _CACHE[r] = (stats.st_mtime, final_data)
            return final_data.copy()
        except (OSError, PermissionError, ValueError, json.JSONDecodeError):
            continue
            
    return DEFAULTS.copy()

def _ensure_settings_integrity(settings: AppSettings) -> AppSettings:
    """Asegura que el diccionario de configuración contenga todas las claves requeridas y con tipos válidos."""
    if not _is_app_settings(settings):
        # Si la estructura base es inválida (ej. versión muy antigua), reconstruimos desde defaults
        clean_settings = DEFAULTS.copy()
        for key in ConfigKey:
            if key.value in settings:
                clean_settings[key.value] = settings[key.value]
        settings = clean_settings

    # Refuerzo: validar tipos individuales tras la inyección de datos externos
    for key in ConfigKey:
        val = settings.get(key.value)
        if not isinstance(val, type(DEFAULTS[key.value])):
            settings[key.value] = DEFAULTS[key.value]
    
    # Lógica de seguridad: si no hay clave, el asistente debe estar apagado
    if settings.get("asistente_activado") and not (settings.get("asistente_clave_api") or os.environ.get(API_KEY_ENV_VAR)):
        settings["asistente_activado"] = False
        
    return settings

def save(values: Any, custom_base: PathLike | None = None) -> Optional[Path]:
    if not _is_dict(values): return None
    ruta = settings_path(custom_base)
    try:
        if not ruta.parent.exists(): ruta.parent.mkdir(parents=True, exist_ok=True)
        ensure_safe_to_modify(ruta.parent)
        cleaned_settings = _ensure_settings_integrity(validate(values))
        serialized = json.dumps(cleaned_settings, indent=2, ensure_ascii=False)
    except (UnsafePathError, TypeError, ValueError, OSError):
        return None
    
    temp_path = ruta.with_suffix(f"{ruta.suffix}.tmp")
    bak_path = ruta.with_suffix(".bak")
    
    try:
        # Asegurar seguridad de la ruta final antes de tocar el disco
        ensure_safe_to_modify(ruta)
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        
        if ruta.exists():
            ensure_safe_to_modify(ruta)
            try: os.replace(ruta, bak_path)
            except OSError: pass
            
        os.replace(temp_path, ruta)
        _CACHE[ruta] = (ruta.stat().st_mtime, cleaned_settings)
        return ruta
    except (OSError, IOError, PermissionError, UnsafePathError):
        if temp_path.exists():
            try: os.remove(temp_path)
            except OSError: pass
        return None

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    current = load(custom_base)
    modified = False
    for k, v in changes.items():
        key_enum = _KEY_TO_ENUM.get(k)
        if key_enum and key_enum in _VALIDATOR_MAP:
            val = _VALIDATOR_MAP[key_enum].func(key_enum, v)
            if val is not None and val != current.get(k):
                current[k] = val
                modified = True
    if modified: save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    save(DEFAULTS, custom_base)
    return DEFAULTS.copy()

def get(key: str, custom_base: PathLike | None = None) -> Any:
    return load(custom_base).get(key, DEFAULTS.get(key))

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    if env_key := os.environ.get(API_KEY_ENV_VAR, "").strip(): return env_key
    return load(custom_base).get("asistente_clave_api", "").strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    if os.environ.get(API_KEY_ENV_VAR): return True
    settings = load(custom_base)
    return bool(settings.get("asistente_activado")) and bool(settings.get("asistente_clave_api", "").strip())

def describe(custom_base: PathLike | None = None) -> list[str]:
    current = load(custom_base)
    api_key_env = os.environ.get(API_KEY_ENV_VAR)
    api_key_file = current.get("asistente_clave_api", "").strip()
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
