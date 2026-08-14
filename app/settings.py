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
import hashlib
from enum import Enum
from pathlib import Path
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

_cached_settings: AppSettings | None = None
_cached_hash: str | None = None
_current_path: Path | None = None

DEFAULTS: Final[AppSettings] = {
    ConfigKey.TEMA.value: "oscuro",
    ConfigKey.ACENTO.value: "menta",
    ConfigKey.MOSTRAR_BARRAS.value: True,
    ConfigKey.ANIMACIONES.value: True,
    ConfigKey.CONFIRMAR_SIEMPRE.value: True,
    ConfigKey.ABRIR_EN.value: "Salud",
    ConfigKey.RECORDAR_ULTIMA_CARPETA.value: True,
    ConfigKey.ULTIMA_CARPETA.value: "",
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB.value: 64,
    ConfigKey.TOP_ARCHIVOS.value: 15,
    ConfigKey.TOP_PROCESOS.value: 15,
    ConfigKey.ANALISIS_EN_PARALELO.value: True,
    ConfigKey.ASISTENTE_ACTIVADO.value: False,
    ConfigKey.ASISTENTE_CLAVE_API.value: "",
    ConfigKey.ASISTENTE_ENVIAR_METRICAS.value: True,
    ConfigKey.ASISTENTE_MODELO.value: "gemini-3.1-flash-lite",
}

_NUMERIC_LIMITS: Final[dict[ConfigKey, tuple[int, int]]] = {
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB: (0, 1024 * 1024),
    ConfigKey.TOP_ARCHIVOS: (1, 500),
    ConfigKey.TOP_PROCESOS: (1, 500),
}

class _Validators:
    """Namespace de validadores de tipo, asegurando integridad antes de guardar."""
    
    @staticmethod
    def _is_safe_path(path_obj: Path) -> bool:
        """Verifica que la ruta sea segura contra manipulaciones de sistema."""
        try:
            resolved = path_obj.resolve()
            if resolved.is_symlink() or (hasattr(resolved, 'is_junction') and resolved.is_junction()):
                return False
            if is_protected_path(str(resolved)): return False
            target = resolved if resolved.exists() else resolved.parent
            return is_safe_to_modify(str(target))
        except (OSError, RuntimeError, PermissionError):
            return False

    @staticmethod
    def bool(val: Any) -> bool | None:
        """Valida valores booleanos aceptando variantes de texto/numéricas."""
        if val is None: return None
        if isinstance(val, bool): return val
        if not isinstance(val, str): return None
        normalized = val.strip().lower()
        if normalized in ("1", "true", "si", "sí", "yes"): return True
        if normalized in ("0", "false", "no", "none"): return False
        return None

    @staticmethod
    def int(key: ConfigKey, val: Any) -> int | None:
        """Valida enteros dentro de límites definidos para cada clave."""
        if val is None or isinstance(val, bool): return None
        try:
            parsed_value = int(val)
            min_limit, max_limit = _NUMERIC_LIMITS.get(key, (0, 10**9))
            return max(min_limit, min(max_limit, parsed_value))
        except (TypeError, ValueError, OverflowError): 
            return None

    @staticmethod
    def path(val: Any) -> str | None:
        """Valida rutas de sistema, asegurando que sean absolutas y seguras."""
        if val is None or not isinstance(val, (str, Path)): return None
        path_string = str(val).strip()
        if not path_string: return ""
        if any(c in path_string for c in ("\0", "\n", "\r")) or ".." in path_string: return None
        try:
            path_obj = Path(path_string).expanduser()
            if not path_obj.is_absolute(): return None
            return str(path_obj) if _Validators._is_safe_path(path_obj) else None
        except (OSError, RuntimeError, ValueError, TypeError, PermissionError, AttributeError):
            return None

    @staticmethod
    def str(key: ConfigKey, val: Any) -> str | None:
        """Valida strings asegurando limpieza de caracteres de control y límites."""
        if val is None or not isinstance(val, (str, Path)): return None
        text = str(val).strip()
        if any(ord(c) < 32 for c in text) or ".." in text: return None
        if key == ConfigKey.ULTIMA_CARPETA: return _Validators.path(text)
        if not text: return "" if key == ConfigKey.ASISTENTE_CLAVE_API else None
        if key == ConfigKey.TEMA: return text.lower() if text.lower() in VALID_THEMES else None
        if key == ConfigKey.ACENTO: return text.lower() if text.lower() in VALID_ACCENTS else None
        return text if len(text) <= 512 else None

_VALIDATOR_MAP: Final[dict[str, Callable[[ConfigKey, Any], Any]]] = {
    ConfigKey.TEMA.value: _Validators.str,
    ConfigKey.ACENTO.value: _Validators.str,
    ConfigKey.ABRIR_EN.value: _Validators.str,
    ConfigKey.ULTIMA_CARPETA.value: _Validators.str,
    ConfigKey.ASISTENTE_CLAVE_API.value: _Validators.str,
    ConfigKey.ASISTENTE_MODELO.value: _Validators.str,
    ConfigKey.MOSTRAR_BARRAS.value: lambda k, v: _Validators.bool(v),
    ConfigKey.ANIMACIONES.value: lambda k, v: _Validators.bool(v),
    ConfigKey.CONFIRMAR_SIEMPRE.value: lambda k, v: _Validators.bool(v),
    ConfigKey.RECORDAR_ULTIMA_CARPETA.value: lambda k, v: _Validators.bool(v),
    ConfigKey.ANALISIS_EN_PARALELO.value: lambda k, v: _Validators.bool(v),
    ConfigKey.ASISTENTE_ACTIVADO.value: lambda k, v: _Validators.bool(v),
    ConfigKey.ASISTENTE_ENVIAR_METRICAS.value: lambda k, v: _Validators.bool(v),
    ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB.value: _Validators.int,
    ConfigKey.TOP_ARCHIVOS.value: _Validators.int,
    ConfigKey.TOP_PROCESOS.value: _Validators.int
}

def settings_path(custom_base: PathLike | None = None) -> Path:
    """Resuelve la ubicación del archivo de configuración, validando seguridad."""
    if custom_base is None: return SETTINGS_DIR / SETTINGS_FILE
    base = Path(custom_base).expanduser().resolve(strict=False)
    return (base / SETTINGS_FILE) if _Validators._is_safe_path(base) else (SETTINGS_DIR / SETTINGS_FILE)

def validate(raw_values: Any) -> AppSettings:
    """Valida un diccionario crudo, aplicando valores de fábrica ante errores."""
    config = DEFAULTS.copy()
    if not isinstance(raw_values, dict): return config
    for key, validator in _VALIDATOR_MAP.items():
        if key in raw_values:
            try:
                enum_key = ConfigKey(key)
                val = validator(enum_key, raw_values.get(key))
                if val is not None: config[key] = val
            except (ValueError, TypeError, AttributeError):
                continue
    return config

def load(custom_base: PathLike | None = None) -> AppSettings:
    """Carga y valida la configuración desde el disco, usando caché si es posible."""
    global _cached_settings, _current_path, _cached_hash
    ruta = settings_path(custom_base)
    
    if not ruta.exists(): return DEFAULTS.copy()
    
    try:
        if _cached_settings is not None and _current_path == ruta:
            return _cached_settings.copy()
        
        content = ruta.read_bytes()
        if 0 < len(content) <= MAX_SETTINGS_SIZE:
            data = json.loads(content)
            if isinstance(data, dict):
                config = validate(data)
                _cached_settings = config
                _cached_hash = hashlib.md5(content).hexdigest()
                _current_path = ruta
                return config.copy()
    except (OSError, PermissionError, json.JSONDecodeError, ValueError):
        pass
    return DEFAULTS.copy()

def save(values: Any, custom_base: PathLike | None = None) -> Path | None:
    """Guarda la configuración validada mediante escritura atómica y comprobación de hash."""
    global _cached_settings, _current_path, _cached_hash
    if not isinstance(values, dict): return None
    ruta = settings_path(custom_base)
    
    if not _Validators._is_safe_path(ruta.parent) or is_protected_path(str(ruta)):
        return None

    cleaned_settings = validate(values)
    if cleaned_settings.get(ConfigKey.ASISTENTE_ACTIVADO.value) and not (cleaned_settings.get(ConfigKey.ASISTENTE_CLAVE_API.value) or os.environ.get(API_KEY_ENV_VAR)):
        cleaned_settings[ConfigKey.ASISTENTE_ACTIVADO.value] = False
        
    json_data = json.dumps(cleaned_settings, indent=2, ensure_ascii=False).encode("utf-8")
    new_hash = hashlib.md5(json_data).hexdigest()
    
    if _cached_hash == new_hash and _current_path == ruta: return ruta
        
    try:
        if not ruta.parent.exists():
            ruta.parent.mkdir(parents=True, exist_ok=True)
            
        temp = ruta.with_suffix(f".{os.getpid()}.tmp")
        with open(temp, "wb") as f:
            f.write(json_data)
            f.flush()
            os.fsync(f.fileno())
            
        os.replace(temp, ruta)
        _cached_settings, _current_path, _cached_hash = cleaned_settings, ruta, new_hash
        return ruta
    except (OSError, IOError, PermissionError, RuntimeError):
        return None

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    """Actualiza campos específicos de la configuración."""
    current = load(custom_base)
    needs_save = False
    for k, v in changes.items():
        if k in _VALIDATOR_MAP:
            validator = _VALIDATOR_MAP[k]
            val = validator(ConfigKey(k), v)
            if val is not None and val != current.get(k):
                current[k] = val
                needs_save = True
    if needs_save: save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    """Reestablece la configuración a los valores por defecto."""
    save(DEFAULTS, custom_base)
    return DEFAULTS.copy()

def get(key: str, custom_base: PathLike | None = None) -> Any:
    """Obtiene un valor individual de la configuración."""
    return load(custom_base).get(key, DEFAULTS.get(key))

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    """Retorna la API Key, priorizando la variable de entorno sobre el archivo."""
    env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return env_key if env_key else load(custom_base).get(ConfigKey.ASISTENTE_CLAVE_API.value, "").strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    """Verifica si el asistente tiene permiso y credenciales para operar."""
    settings = load(custom_base)
    return bool(settings.get(ConfigKey.ASISTENTE_ACTIVADO.value) and (os.environ.get(API_KEY_ENV_VAR) or settings.get(ConfigKey.ASISTENTE_CLAVE_API.value)))

def describe(custom_base: PathLike | None = None) -> list[str]:
    """Genera una descripción legible de la configuración actual para reportes."""
    current = load(custom_base)
    key = assistant_api_key(custom_base)
    origin = f"variable de entorno {API_KEY_ENV_VAR}" if os.environ.get(API_KEY_ENV_VAR) else ("archivo de configuración" if key else "no configurada")
    return [
        "Configuración actual", "", f"  Archivo: {settings_path(custom_base)}", "",
        "  Apariencia", f"    Tema: {current[ConfigKey.TEMA.value]}", f"    Acento: {current[ConfigKey.ACENTO.value]}",
        f"    Barras visuales: {'sí' if current[ConfigKey.MOSTRAR_BARRAS.value] else 'no'}", "",
        "  Comportamiento", f"    Confirmar siempre: {'sí' if current[ConfigKey.CONFIRMAR_SIEMPRE.value] else 'no'}",
        f"    Pestaña inicial: {current[ConfigKey.ABRIR_EN.value]}", f"    Recordar carpeta: {'sí' if current[ConfigKey.RECORDAR_ULTIMA_CARPETA.value] else 'no'}", "",
        "  Rendimiento", f"    Duplicados desde: {current[ConfigKey.DUPLICADOS_TAMANO_MINIMO_KB.value]} KB",
        f"    Top de archivos: {current[ConfigKey.TOP_ARCHIVOS.value]}", f"    Análisis en paralelo: {'sí' if current[ConfigKey.ANALISIS_EN_PARALELO.value] else 'no'}", "",
        "  Asistente IA", f"    Activado: {'sí' if current[ConfigKey.ASISTENTE_ACTIVADO.value] else 'no'}",
        f"    Clave: {origin}", f"    Modelo: {current[ConfigKey.ASISTENTE_MODELO.value]}", ""
    ]
