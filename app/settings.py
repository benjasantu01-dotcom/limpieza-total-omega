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
from enum import Enum
from pathlib import Path
from typing import Any, Final, TypeAlias, Callable, TypedDict, Optional, TypeVar, ParamSpec, NamedTuple

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

VALID_THEMES: Final[frozenset[str]] = frozenset(("oscuro", "claro", "sistema"))
VALID_ACCENTS: Final[frozenset[str]] = frozenset(("menta", "violeta", "magenta", "cian", "ambar"))

_CACHE: dict[Path, tuple[float, AppSettings]] = {}
_STR_TO_ENUM: Final[dict[str, ConfigKey]] = {k.value: k for k in ConfigKey}

def _get_default_config() -> AppSettings:
    """Retorna el estado de fábrica de la configuración (valores seguros)."""
    return {
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

DEFAULTS: Final[AppSettings] = _get_default_config()

_NUMERIC_LIMITS: Final[dict[ConfigKey, _NumericRange]] = {
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB: _NumericRange(0, 1024 * 1024),
    ConfigKey.TOP_ARCHIVOS: _NumericRange(1, 500),
    ConfigKey.TOP_PROCESOS: _NumericRange(1, 500),
}

def type_check(func: Callable[P, T | None]) -> Callable[P, T | None]:
    """Decorador: Filtra llamadas inválidas o nulas antes de pasar al validador."""
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        val = args[1] if len(args) > 1 else kwargs.get("val")
        if val is None or (isinstance(val, bool) and func.__name__ != "bool"):
            return None
        return func(*args, **kwargs)
    return wrapper

class _Validators:
    """Motor central de validación: Sanitiza entradas según reglas de seguridad y tipos."""
    
    @staticmethod
    def _run_safety_checks(path_obj: Path) -> bool:
        """Verifica que la ruta sea un archivo/carpeta real, no un enlace de sistema peligroso."""
        if path_obj.is_symlink() or (hasattr(path_obj, 'is_junction') and path_obj.is_junction()):
            return False
        if path_obj.exists() and not (path_obj.is_file() or path_obj.is_dir()):
            return False
        return not is_protected_path(str(path_obj)) and is_safe_to_modify(str(path_obj))

    @staticmethod
    def _is_safe_path(path_str: str) -> bool:
        """Valida que una cadena de texto represente una ruta absoluta y segura."""
        if not path_str or ".." in path_str: return False
        try:
            return _Validators._run_safety_checks(Path(path_str).resolve(strict=False))
        except (OSError, RuntimeError, PermissionError, AttributeError):
            return False

    @staticmethod
    def bool(key: ConfigKey, val: Any) -> Optional[bool]:
        """Normaliza entradas truthy/falsy (strings o bools) a booleanos estándar."""
        if isinstance(val, bool): return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in ("1", "true", "si", "sí", "yes"): return True
            if normalized in ("0", "false", "no", "none"): return False
        return None

    @staticmethod
    @type_check
    def int(key: ConfigKey, val: Any) -> Optional[int]:
        """Convierte a entero y aplica restricciones de rango definidas en _NUMERIC_LIMITS."""
        try:
            parsed_value: int = int(val)
            limit = _NUMERIC_LIMITS.get(key, _NumericRange(0, 10**9))
            return max(limit.min, min(limit.max, parsed_value))
        except (TypeError, ValueError, OverflowError): return None

    @staticmethod
    def path(key: ConfigKey, val: Any) -> Optional[str]:
        """
        Valida que una ruta sea absoluta, resuelta y dentro de zonas permitidas.
        Aplica sanitización estricta contra inyección de rutas (puntos, null bytes).
        """
        if not isinstance(val, (str, Path)): return None
        path_string = str(val).strip()
        
        if not path_string or len(path_string) > 4096 or "\0" in path_string or ".." in path_string: 
            return None
            
        try:
            path_obj = Path(path_string).expanduser()
            if not path_obj.is_absolute(): return None
            
            resolved = path_obj.resolve(strict=False)
            if not resolved.is_absolute(): return None
            
            return str(resolved) if _Validators._is_safe_path(str(resolved)) else None
        except (OSError, RuntimeError, ValueError, TypeError, PermissionError, AttributeError):
            return None

    @staticmethod
    def _validate_enum_str(text: str, key: ConfigKey) -> Optional[str]:
        """Verifica que el string pertenezca a un conjunto permitido (Temas/Acentos)."""
        val = text.lower()
        if key == ConfigKey.TEMA: return val if val in VALID_THEMES else None
        if key == ConfigKey.ACENTO: return val if val in VALID_ACCENTS else None
        if key == ConfigKey.ASISTENTE_CLAVE_API: return text.strip() if len(text) <= 512 else None
        return text if len(text) <= 512 else None

    @staticmethod
    @type_check
    def str(key: ConfigKey, val: Any) -> Optional[str]:
        """Sanitiza strings de configuración, filtrando caracteres de control y derivando al validador de enum."""
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
    """Calcula la ruta del config.json, validando que el directorio base no sea de sistema."""
    if custom_base is None: return SETTINGS_DIR / SETTINGS_FILE
    try:
        base = Path(custom_base).expanduser().absolute()
        if _Validators._is_safe_path(str(base)):
            return base / SETTINGS_FILE
    except (OSError, RuntimeError):
        pass
    return SETTINGS_DIR / SETTINGS_FILE

def validate(raw_values: Any) -> AppSettings:
    """Valida un diccionario externo (JSON) contra el esquema AppSettings, forzando valores de fábrica ante corrupción."""
    config = _get_default_config()
    if not isinstance(raw_values, dict): return config
    for key_str, val in raw_values.items():
        key = _STR_TO_ENUM.get(key_str)
        if key and key in _VALIDATOR_MAP:
            validated = _VALIDATOR_MAP[key](key, val)
            if validated is not None:
                config[key.value] = validated
    return config

def load(custom_base: PathLike | None = None) -> AppSettings:
    """Lee configuración desde disco con caché de memoria basada en el mtime del archivo para evitar I/O redundante."""
    ruta = settings_path(custom_base)
    try:
        if not ruta.exists(): return _get_default_config()
        stat_info = ruta.stat()
        mtime = stat_info.st_mtime
        if (cached := _CACHE.get(ruta)) and cached[0] == mtime:
            return cached[1]
        if stat_info.st_size > MAX_SETTINGS_SIZE or stat_info.st_size < 10:
            return _get_default_config()
        with open(ruta, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict): return _get_default_config()
        config = validate(data)
        _CACHE[ruta] = (mtime, config)
        return config
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, PermissionError, RuntimeError):
        return _get_default_config()

def save(values: Any, custom_base: PathLike | None = None) -> Path | None:
    """Guarda configuración de forma atómica: escribe en archivo temporal y luego renombra para evitar corrupción."""
    if not isinstance(values, dict): return None
    
    cleaned_settings = validate(values)
    # Validaciones de integridad: el asistente no puede activarse sin clave
    if cleaned_settings["asistente_activado"] and not (cleaned_settings["asistente_clave_api"] or os.environ.get(API_KEY_ENV_VAR)):
        cleaned_settings["asistente_activado"] = False

    ruta = settings_path(custom_base)
    parent = ruta.parent.absolute()
    
    try:
        # Validación estricta de entorno antes de tocar el disco
        if not _Validators._is_safe_path(str(parent)):
            return None
        ensure_safe_to_modify(str(ruta))
        
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            
        temp_name = None
        encoded_data = json.dumps(cleaned_settings, indent=2, ensure_ascii=False).encode("utf-8")
        if len(encoded_data) > MAX_SETTINGS_SIZE: return None
        
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=parent) as tf:
            temp_name = tf.name
            tf.write(encoded_data)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(temp_name, ruta)
        _CACHE[ruta] = (ruta.stat().st_mtime, cleaned_settings)
        return ruta
    except (TypeError, ValueError, OSError, IOError, PermissionError, RuntimeError):
        return None

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    """Actualiza parcialmente la configuración, persistiendo solo si hubo cambios reales validados."""
    current = load(custom_base).copy()
    modified = False
    for k, v in changes.items():
        key_enum = _STR_TO_ENUM.get(k)
        if key_enum and key_enum in _VALIDATOR_MAP:
            val = _VALIDATOR_MAP[key_enum](key_enum, v)
            if val is not None and val != current.get(k):
                current[k] = val # type: ignore
                modified = True
    if modified: save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    """Restaura la configuración a los valores definidos de fábrica."""
    default_config = _get_default_config()
    save(default_config, custom_base)
    return default_config

def get(key: str, custom_base: PathLike | None = None) -> Any:
    """Accesor seguro a un valor individual, retornando el valor de fábrica si falta la clave."""
    return load(custom_base).get(key, DEFAULTS.get(key))

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    """Retorna la clave de API priorizando la variable de entorno sobre el archivo de configuración."""
    env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return env_key if env_key else load(custom_base).get("asistente_clave_api", "").strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    """Verifica si el asistente tiene los requisitos para operar (clave presente y permiso del usuario)."""
    if os.environ.get(API_KEY_ENV_VAR): return True
    settings = load(custom_base)
    return bool(settings.get("asistente_activado", False)) and bool(settings.get("asistente_clave_api", ""))

def describe(custom_base: PathLike | None = None) -> list[str]:
    """Genera un reporte legible de la configuración actual, útil para auditoría y logs."""
    current = load(custom_base)
    key = assistant_api_key(custom_base)
    origin = f"variable de entorno {API_KEY_ENV_VAR}" if os.environ.get(API_KEY_ENV_VAR) else ("archivo de configuración" if key else "no configurada")
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
