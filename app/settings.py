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
from typing import Any, Final, TypeAlias, Callable, TypedDict, Optional

from safety import is_safe_to_modify, is_protected_path

PathLike: TypeAlias = str | Path

class ConfigKey(Enum):
    """Enumeración de claves permitidas en el diccionario de configuración."""
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

VALID_THEMES: Final[frozenset[str]] = frozenset(("oscuro", "claro", "sistema"))
VALID_ACCENTS: Final[frozenset[str]] = frozenset(("menta", "violeta", "magenta", "cian", "ambar"))

_CACHE: dict[str, tuple[float, AppSettings]] = {}

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

_NUMERIC_LIMITS: Final[dict[ConfigKey, tuple[int, int]]] = {
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB: (0, 1024 * 1024),
    ConfigKey.TOP_ARCHIVOS: (1, 500),
    ConfigKey.TOP_PROCESOS: (1, 500),
}

class _Validators:
    """Motor de validación de datos de entrada para `config.json`."""
    
    @staticmethod
    def _is_safe_path(path_str: str) -> bool:
        """Verifica recursivamente que la ruta sea segura y no sea un enlace simbólico/junction."""
        try:
            path_obj = Path(path_str).resolve(strict=False)
            if path_obj.is_symlink() or (hasattr(path_obj, 'is_junction') and path_obj.is_junction()):
                return False
            if is_protected_path(str(path_obj)): return False
            return is_safe_to_modify(str(path_obj))
        except (OSError, RuntimeError, PermissionError, AttributeError):
            return False

    @staticmethod
    def bool(val: Any) -> Optional[bool]:
        """Normaliza entradas de usuario a booleanos, aceptando strings comunes."""
        if isinstance(val, bool): return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in ("1", "true", "si", "sí", "yes"): return True
            if normalized in ("0", "false", "no", "none"): return False
        return None

    @staticmethod
    def int(key: ConfigKey, val: Any) -> Optional[int]:
        """Valida y ajusta valores numéricos dentro de los límites definidos en _NUMERIC_LIMITS."""
        if val is None or isinstance(val, bool): return None
        try:
            parsed_value: int = int(val)
            min_limit, max_limit = _NUMERIC_LIMITS.get(key, (0, 10**9))
            return max(min_limit, min(max_limit, parsed_value))
        except (TypeError, ValueError, OverflowError): return None

    @staticmethod
    def path(val: Any) -> Optional[str]:
        """Valida si la entrada es una ruta absoluta, segura y no bloqueada por `safety`."""
        if val is None or not isinstance(val, (str, Path)): return None
        path_string = str(val).strip()
        if not path_string or len(path_string) > 4096 or any(c in path_string for c in ("\0", "\n", "\r")) or ".." in path_string: return None
        try:
            path_obj = Path(path_string).expanduser()
            if not path_obj.is_absolute(): return None
            resolved = path_obj.resolve(strict=False)
            if not str(resolved).startswith(str(path_obj.anchor)): return None
            return str(resolved) if _Validators._is_safe_path(str(resolved)) else None
        except (OSError, RuntimeError, ValueError, TypeError, PermissionError, AttributeError):
            return None

    @staticmethod
    def _validate_enum_str(text: str, key: ConfigKey) -> Optional[str]:
        """Valida strings restringidos a un conjunto específico de opciones permitidas."""
        val = text.lower()
        if key == ConfigKey.TEMA: return val if val in VALID_THEMES else None
        if key == ConfigKey.ACENTO: return val if val in VALID_ACCENTS else None
        return text if len(text) <= 512 else None

    @staticmethod
    def str(key: ConfigKey, val: Any) -> Optional[str]:
        """Sanitiza strings generales y delega validación de rutas o enums según la clave."""
        if not isinstance(val, str): return None
        text = val.strip()
        if not text or any(ord(c) < 32 for c in text) or ".." in text or len(text) > 1024: return None
        if key == ConfigKey.ULTIMA_CARPETA: return _Validators.path(text)
        return _Validators._validate_enum_str(text, key)

_VALIDATOR_MAP: Final[dict[ConfigKey, Callable[[ConfigKey, Any], Any]]] = {
    ConfigKey.TEMA: _Validators.str,
    ConfigKey.ACENTO: _Validators.str,
    ConfigKey.ABRIR_EN: _Validators.str,
    ConfigKey.ULTIMA_CARPETA: _Validators.str,
    ConfigKey.ASISTENTE_CLAVE_API: _Validators.str,
    ConfigKey.ASISTENTE_MODELO: _Validators.str,
    ConfigKey.MOSTRAR_BARRAS: lambda k, v: _Validators.bool(v),
    ConfigKey.ANIMACIONES: lambda k, v: _Validators.bool(v),
    ConfigKey.CONFIRMAR_SIEMPRE: lambda k, v: _Validators.bool(v),
    ConfigKey.RECORDAR_ULTIMA_CARPETA: lambda k, v: _Validators.bool(v),
    ConfigKey.ANALISIS_EN_PARALELO: lambda k, v: _Validators.bool(v),
    ConfigKey.ASISTENTE_ACTIVADO: lambda k, v: _Validators.bool(v),
    ConfigKey.ASISTENTE_ENVIAR_METRICAS: lambda k, v: _Validators.bool(v),
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB: _Validators.int,
    ConfigKey.TOP_ARCHIVOS: _Validators.int,
    ConfigKey.TOP_PROCESOS: _Validators.int
}

_STR_TO_ENUM: Final[dict[str, ConfigKey]] = {k.value: k for k in ConfigKey}

def settings_path(custom_base: PathLike | None = None) -> Path:
    """Resuelve la ubicación del archivo de configuración, priorizando `custom_base`."""
    if custom_base is None: return SETTINGS_DIR / SETTINGS_FILE
    base = Path(custom_base).expanduser().resolve(strict=False)
    if _Validators._is_safe_path(str(base)):
        return base / SETTINGS_FILE
    return SETTINGS_DIR / SETTINGS_FILE

def validate(raw_values: Any) -> AppSettings:
    """Valida un diccionario arbitrario contra el esquema esperado, aplicando valores por defecto si falla."""
    config = _get_default_config()
    if not isinstance(raw_values, dict): return config
    for key_str, val in raw_values.items():
        key = _STR_TO_ENUM.get(key_str)
        if key and key in _VALIDATOR_MAP:
            validated = _VALIDATOR_MAP[key](key, val)
            if validated is not None: config[key_str] = validated # type: ignore
    return config

def load(custom_base: PathLike | None = None) -> AppSettings:
    """Lee y parsea el archivo de configuración; utiliza caché de `st_mtime` para eficiencia."""
    ruta = settings_path(custom_base)
    ruta_str = str(ruta)
    try:
        if not ruta.exists(): return _get_default_config()
        stat = ruta.stat()
        if ruta_str in _CACHE and _CACHE[ruta_str][0] == stat.st_mtime:
            return _CACHE[ruta_str][1]
        if stat.st_size > MAX_SETTINGS_SIZE or not _Validators._is_safe_path(ruta_str):
            return _get_default_config()
        with open(ruta, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict): return _get_default_config()
        config = validate(data)
        _CACHE[ruta_str] = (stat.st_mtime, config)
        return config
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, PermissionError):
        return _get_default_config()

def save(values: Any, custom_base: PathLike | None = None) -> Path | None:
    """Serializa la configuración al disco usando una escritura atómica mediante archivo temporal."""
    if not isinstance(values, dict): return None
    ruta = settings_path(custom_base)
    if ruta.is_symlink() or (hasattr(ruta, 'is_junction') and ruta.is_junction()): return None
    parent = ruta.parent.resolve(strict=False)
    if not parent.exists():
        try: parent.mkdir(parents=True, exist_ok=True)
        except OSError: return None
    if not _Validators._is_safe_path(str(parent)): return None
    cleaned_settings = validate(values)
    if cleaned_settings["asistente_activado"] and not (cleaned_settings["asistente_clave_api"] or os.environ.get(API_KEY_ENV_VAR)):
        cleaned_settings["asistente_activado"] = False
    temp_path = ruta.with_suffix(f"{ruta.suffix}.{os.getpid()}.tmp")
    try:
        encoded_data = json.dumps(cleaned_settings, indent=2, ensure_ascii=False).encode("utf-8")
        if len(encoded_data) > MAX_SETTINGS_SIZE: return None
        with open(temp_path, "wb") as f:
            f.write(encoded_data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, ruta)
        _CACHE[str(ruta)] = (ruta.stat().st_mtime, cleaned_settings)
        return ruta
    except (OSError, IOError, PermissionError, RuntimeError, TypeError, ValueError):
        if temp_path.exists():
            try: temp_path.unlink()
            except OSError: pass
        return None

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    """Aplica cambios parciales y guarda el resultado si hubo modificaciones válidas."""
    current = load(custom_base)
    needs_save = False
    for k, v in changes.items():
        key_enum = _STR_TO_ENUM.get(k)
        if key_enum and key_enum in _VALIDATOR_MAP:
            val = _VALIDATOR_MAP[key_enum](key_enum, v)
            if val is not None and val != current.get(k):
                current[k] = val # type: ignore
                needs_save = True
    if needs_save: save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    """Restaura la configuración a los valores de fábrica."""
    default_config = _get_default_config()
    save(default_config, custom_base)
    return default_config

def get(key: str, custom_base: PathLike | None = None) -> Any:
    """Obtiene un valor individual, con fallback a valores de fábrica."""
    return load(custom_base).get(key, DEFAULTS.get(key))

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    """Devuelve la API Key del asistente priorizando la variable de entorno sobre el archivo."""
    env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return env_key if env_key else load(custom_base).get("asistente_clave_api", "").strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    """Determina si el asistente IA está habilitado basado en la configuración y presencia de clave."""
    if os.environ.get(API_KEY_ENV_VAR): return True
    settings = load(custom_base)
    return bool(settings.get("asistente_activado", False)) and bool(settings.get("asistente_clave_api", ""))

def describe(custom_base: PathLike | None = None) -> list[str]:
    """Genera una descripción legible por humanos de la configuración actual."""
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
