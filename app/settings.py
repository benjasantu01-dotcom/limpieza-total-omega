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
from functools import lru_cache
from typing import Any, Final, TypeAlias, Callable, TypedDict

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

VALID_THEMES: Final[tuple[str, ...]] = ("oscuro", "claro", "sistema")
VALID_ACCENTS: Final[tuple[str, ...]] = ("menta", "violeta", "magenta", "cian", "ambar")

def _get_default_config() -> AppSettings:
    """Retorna un diccionario con los valores por defecto definidos para la app."""
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
    """Contiene lógica de validación estricta para asegurar que los datos no corrompan la app."""
    @staticmethod
    @lru_cache(maxsize=32)
    def _is_safe_path(path_str: str) -> bool:
        """Verifica si una ruta es segura para ser tratada o almacenada, evitando symlinks/junctions."""
        try:
            path_obj = Path(path_str)
            resolved = path_obj.resolve(strict=False)
            if resolved.is_symlink() or (hasattr(resolved, 'is_junction') and resolved.is_junction()):
                return False
            if is_protected_path(str(resolved)): return False
            target = resolved if resolved.exists() else resolved.parent
            return is_safe_to_modify(str(target))
        except (OSError, RuntimeError, PermissionError, AttributeError):
            return False

    @staticmethod
    def bool(val: Any) -> bool | None:
        """Normaliza tipos mixtos a booleano; retorna None si es irrecuperable."""
        if isinstance(val, bool): return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in ("1", "true", "si", "sí", "yes"): return True
            if normalized in ("0", "false", "no", "none"): return False
        return None

    @staticmethod
    def int(key: ConfigKey, val: Any) -> int | None:
        """Valida números dentro de rangos acotados para prevenir errores de lógica."""
        if val is None or isinstance(val, bool): return None
        try:
            parsed_value = int(val)
            min_limit, max_limit = _NUMERIC_LIMITS.get(key, (0, 10**9))
            return max(min_limit, min(max_limit, parsed_value))
        except (TypeError, ValueError, OverflowError): return None

    @staticmethod
    def path(val: Any) -> str | None:
        """Valida la integridad de rutas de usuario, descartando caracteres maliciosos o rutas de sistema."""
        if val is None or not isinstance(val, (str, Path)): return None
        path_string = str(val).strip()
        if not path_string or any(c in path_string for c in ("\0", "\n", "\r")) or ".." in path_string: return None
        try:
            path_obj = Path(path_string).expanduser()
            if not path_obj.is_absolute(): return None
            if is_protected_path(str(path_obj)): return None
            path_str = str(path_obj)
            return path_str if _Validators._is_safe_path(path_str) else None
        except (OSError, RuntimeError, ValueError, TypeError, PermissionError, AttributeError):
            return None

    @staticmethod
    def _validate_enum_str(text: str, key: ConfigKey) -> str | None:
        """Valida que los strings de configuración pertenezcan a los valores permitidos (whitelisting)."""
        if key == ConfigKey.TEMA: return text.lower() if text.lower() in VALID_THEMES else None
        if key == ConfigKey.ACENTO: return text.lower() if text.lower() in VALID_ACCENTS else None
        return text if len(text) <= 512 else None

    @staticmethod
    def str(key: ConfigKey, val: Any) -> str | None:
        """Valida y limpia cadenas de texto evitando inyección o exceso de tamaño."""
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

def settings_path(custom_base: PathLike | None = None) -> Path:
    """Calcula la ruta absoluta del archivo de configuración, validando el directorio base."""
    if custom_base is None: return SETTINGS_DIR / SETTINGS_FILE
    base = Path(custom_base).expanduser().resolve(strict=False)
    if not _Validators._is_safe_path(str(base)): return SETTINGS_DIR / SETTINGS_FILE
    return base / SETTINGS_FILE

def validate(raw_values: Any) -> AppSettings:
    """Valida un diccionario arbitrario contra el esquema AppSettings, descartando claves corruptas."""
    config = _get_default_config()
    if not isinstance(raw_values, dict): return config
    for key, validator in _VALIDATOR_MAP.items():
        if key.value in raw_values:
            try:
                val = validator(key, raw_values.get(key.value))
                if val is not None: config[key.value] = val
            except (ValueError, TypeError, AttributeError): continue
    return config

@lru_cache(maxsize=1)
def _load_internal(ruta: Path) -> AppSettings:
    """Carga interna cacheada por ruta y mtime (gestionado mediante wrapper en load)."""
    if not ruta.exists() or not os.access(ruta, os.R_OK): return _get_default_config()
    try:
        if not _Validators._is_safe_path(str(ruta.parent)): return _get_default_config()
        content = ruta.read_bytes()
        if not content or len(content) > MAX_SETTINGS_SIZE: return _get_default_config()
        data = json.loads(content)
        return validate(data) if isinstance(data, dict) else _get_default_config()
    except (OSError, PermissionError, json.JSONDecodeError, ValueError, TypeError):
        return _get_default_config()

def load(custom_base: PathLike | None = None) -> AppSettings:
    """Carga, valida y cachea el archivo de configuración. Retorna defaults ante cualquier error."""
    ruta = settings_path(custom_base)
    if not ruta.exists(): return _get_default_config()
    
    # Invalidar caché si el archivo cambió
    mtime = ruta.stat().st_mtime
    if hasattr(load, "_last_mtime") and load._last_mtime != mtime:
        _load_internal.cache_clear()
    
    load._last_mtime = mtime
    return _load_internal(ruta)

def save(values: Any, custom_base: PathLike | None = None) -> Path | None:
    """Guarda una configuración validada de forma atómica usando un archivo temporal."""
    if not isinstance(values, dict): return None
    ruta = settings_path(custom_base)
    if not _Validators._is_safe_path(str(ruta.parent)) or is_protected_path(str(ruta)): return None
    if not is_safe_to_modify(str(ruta)): return None
    if ruta.exists() and not os.access(ruta, os.W_OK): return None
    
    cleaned_settings = validate(values)
    if cleaned_settings["asistente_activado"] and not (cleaned_settings["asistente_clave_api"] or os.environ.get(API_KEY_ENV_VAR)):
        cleaned_settings["asistente_activado"] = False
        
    json_data = json.dumps(cleaned_settings, indent=2, ensure_ascii=False).encode("utf-8")
    temp_ruta = ruta.with_suffix(f".{os.getpid()}.tmp")
    
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_ruta, "wb") as f:
            f.write(json_data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_ruta, ruta)
        _load_internal.cache_clear()
        return ruta
    except (OSError, IOError, PermissionError, RuntimeError):
        if temp_ruta.exists():
            try: temp_ruta.unlink()
            except OSError: pass
        return None

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    """Aplica cambios parciales a la configuración actual y los persiste."""
    current = load(custom_base).copy()
    needs_save = False
    for k, v in changes.items():
        try:
            key_enum = ConfigKey(k)
            validator = _VALIDATOR_MAP.get(key_enum)
            if validator:
                val = validator(key_enum, v)
                if val is not None and val != current.get(k):
                    current[k] = val
                    needs_save = True
        except ValueError: continue
    if needs_save: save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    """Restablece la configuración a los valores de fábrica."""
    default_config = _get_default_config()
    save(default_config, custom_base)
    return default_config

def get(key: str, custom_base: PathLike | None = None) -> Any:
    """Obtiene un valor individual de la configuración."""
    return load(custom_base).get(key, DEFAULTS.get(key))

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    """Prioriza la clave API desde variables de entorno sobre el archivo de configuración."""
    env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return env_key if env_key else load(custom_base).get("asistente_clave_api", "").strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    """Verifica si el asistente está habilitado y posee una clave válida."""
    settings = load(custom_base)
    return bool(settings["asistente_activado"]) and bool(os.environ.get(API_KEY_ENV_VAR) or settings["asistente_clave_api"])

def describe(custom_base: PathLike | None = None) -> list[str]:
    """Genera una representación textual formateada de la configuración actual para reportes."""
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
