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
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeAlias, Callable, TypedDict, Optional, TypeVar, ParamSpec, NamedTuple, TypeGuard

from safety import is_safe_to_modify, is_protected_path, ensure_safe_to_modify, UnsafePathError

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

def _is_dict(val: Any) -> TypeGuard[SettingsDict]:
    """Verifica que el objeto sea un diccionario válido para la configuración."""
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
_PATH_CACHE: dict[str, Path] = { "default": SETTINGS_DIR / SETTINGS_FILE }
_SAFETY_CACHE: dict[str, bool] = {}

VALID_THEMES: Final[frozenset[str]] = frozenset(("oscuro", "claro", "sistema"))
VALID_ACCENTS: Final[frozenset[str]] = frozenset(("menta", "violeta", "magenta", "cian", "ambar"))
VALID_MODELS: Final[frozenset[str]] = frozenset(("gemini-3.1-flash-lite", "gemini-3.1-pro"))

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
    """Decorador: Filtra llamadas y captura excepciones de conversión/parsing."""
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        val = args[1] if len(args) > 1 else kwargs.get("val")
        if val is None: return None
        try:
            return func(*args, **kwargs)
        except (ValueError, TypeError, AttributeError, OverflowError, KeyError):
            return None
    return wrapper

class _Validators:
    """Namespace de validadores estáticos para asegurar la integridad de la configuración."""

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        """Determina si una ruta es un junction o symlink para prevenir bucles de recursión."""
        try:
            return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
        except (OSError, PermissionError):
            return True

    @staticmethod
    def _run_safety_checks(path_obj: Path) -> bool:
        """Valida una ruta contra `safety.py` resolviendo el destino real para prevenir traversal."""
        path_str = str(path_obj)
        if path_str in _SAFETY_CACHE:
            return _SAFETY_CACHE[path_str]
        
        try:
            resolved = path_obj.resolve(strict=False)
            is_safe = not _Validators._is_reparse_point(resolved) and \
                      not is_protected_path(str(resolved)) and \
                      is_safe_to_modify(str(resolved))
            # Solo invocamos ensure_safe_to_modify si el check básico pasó, optimizando I/O
            if is_safe:
                ensure_safe_to_modify(str(resolved))
        except (OSError, PermissionError, RuntimeError, UnsafePathError, IndexError):
            is_safe = False
            
        _SAFETY_CACHE[path_str] = is_safe
        return is_safe

    @staticmethod
    def _is_safe_path(path_str: str) -> bool:
        """Verifica que el string de ruta sea absoluto, saneado contra null-bytes y seguro para I/O."""
        if not path_str or len(path_str) > 2048 or "\0" in path_str: return False
        # El chequeo contra _SAFETY_CACHE está integrado en _run_safety_checks
        try:
            p = Path(path_str).expanduser()
            if not p.is_absolute(): return False
            return _Validators._run_safety_checks(p)
        except (OSError, RuntimeError, PermissionError, AttributeError, ValueError):
            return False

    @staticmethod
    def bool(key: ConfigKey, val: Any) -> Optional[bool]:
        """Normaliza tipos mixtos (str/int/bool) a un valor booleano canónico o None."""
        if isinstance(val, bool): return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in ("1", "true", "si", "sí", "yes"): return True
            if normalized in ("0", "false", "no", "none"): return False
        return None

    @staticmethod
    @type_check
    def int(key: ConfigKey, val: Any) -> Optional[int]:
        """Convierte entrada a entero, aplicando los límites definidos en _NUMERIC_LIMITS."""
        if not isinstance(val, (int, str)): return None
        parsed_value = int(val)
        limit = _NUMERIC_LIMITS.get(key)
        if limit: return max(limit.min, min(limit.max, parsed_value))
        return parsed_value

    @staticmethod
    def path(key: ConfigKey, val: Any) -> Optional[str]:
        """Valida que la entrada sea una ruta de sistema segura y accesible antes de persistirla."""
        if val == "": return ""
        if not isinstance(val, (str, Path)): return None
        path_string = str(val).strip()
        return path_string if _Validators._is_safe_path(path_string) else None

    @staticmethod
    def _validate_enum_str(text: str, key: ConfigKey) -> Optional[str]:
        """Filtra strings contra una lista blanca (white-list) definida por el contexto del enum."""
        val = text.lower()
        allowed = _ENUM_VALS.get(key)
        if allowed: return val if val in allowed else None
        return text if len(text) <= 512 else None

    @staticmethod
    @type_check
    def str(key: ConfigKey, val: Any) -> Optional[str]:
        """Sanitiza strings de configuración previniendo inyecciones, caracteres no imprimibles y path traversal."""
        text = str(val).strip()
        if not text or "\0" in text or any(ord(c) < 32 for c in text) or ".." in text or len(text) > 1024: return None
        if key == ConfigKey.ULTIMA_CARPETA: return _Validators.path(key, text)
        return _Validators._validate_enum_str(text, key)

_VALIDATOR_MAP: Final[MappingProxyType[ConfigKey, Callable[[ConfigKey, Any], Any]]] = MappingProxyType({
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
})

def settings_path(custom_base: PathLike | None = None) -> Path:
    """Retorna la ruta absoluta del archivo de configuración, priorizando el caché de rutas."""
    if custom_base is None: return _PATH_CACHE["default"]
    cache_key = str(custom_base)
    if (cached := _PATH_CACHE.get(cache_key)) is not None:
        return cached
    try:
        base = Path(custom_base).expanduser()
        if _Validators._is_safe_path(str(base)):
            resolved = base.resolve() / SETTINGS_FILE
            _PATH_CACHE[cache_key] = resolved
            return resolved
    except (OSError, RuntimeError, PermissionError):
        pass
    return _PATH_CACHE["default"]

def validate(raw_values: Any) -> AppSettings:
    """
    Valida un diccionario arbitrario contra el esquema AppSettings.
    Itera cada clave, valida su tipo y valor mediante el _VALIDATOR_MAP, 
    y asegura que el objeto resultante sea seguro y compatible con la app.
    """
    config = DEFAULTS.copy()
    if not _is_dict(raw_values): return config
    for key_str, val in raw_values.items():
        key_enum = _STR_TO_ENUM.get(key_str)
        if key_enum and (validator := _VALIDATOR_MAP.get(key_enum)):
            validated = validator(key_enum, val)
            if validated is not None:
                config[key_enum.value] = validated
            elif key_enum == ConfigKey.ULTIMA_CARPETA and val == "":
                config[key_enum.value] = ""
    return config

def load(custom_base: PathLike | None = None) -> AppSettings:
    """
    Carga y valida el JSON de configuración desde disco.
    Intenta cargar el archivo original y, en caso de error o archivo corrupto,
    recurre al respaldo (.bak). Retorna valores de fábrica si falla todo.
    Usa un caché temporal basado en el timestamp (mtime) del archivo.
    """
    ruta = settings_path(custom_base)
    ruta_str = str(ruta)
    rutas_a_probar = [ruta, ruta.with_suffix(".json.bak")]
    
    for r in rutas_a_probar:
        try:
            if not r.exists() or not r.is_file(): continue
            stats = r.stat()
            mtime = float(stats.st_mtime)
            if (cached := _CACHE.get(ruta_str)) and cached[0] == mtime:
                return cached[1]
            if 0 < stats.st_size <= MAX_SETTINGS_SIZE:
                with open(r, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    data = validate(content)
                _CACHE[ruta_str] = (mtime, data)
                return data
        except (OSError, PermissionError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
    return DEFAULTS.copy()

def save(values: Any, custom_base: PathLike | None = None) -> Optional[Path]:
    """
    Persiste la configuración de forma atómica.
    1. Valida los datos entrantes.
    2. Crea un archivo temporal (.tmp).
    3. Asegura permisos de escritura y seguridad de la ruta mediante `ensure_safe_to_modify`.
    4. Realiza un back-up del archivo existente.
    5. Reemplaza el archivo original de forma segura (atomic swap).
    """
    if not _is_dict(values): return None
    ruta = settings_path(custom_base)
    cleaned_settings = validate(values)
    
    temp_path: Optional[Path] = None
    try:
        if cleaned_settings.get("asistente_activado") and not (
            cleaned_settings.get("asistente_clave_api") or os.environ.get(API_KEY_ENV_VAR)
        ):
            cleaned_settings["asistente_activado"] = False
            
        parent = ruta.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        ensure_safe_to_modify(str(parent))
        
        if ruta.exists():
            ensure_safe_to_modify(str(ruta))
        
        data = json.dumps(cleaned_settings, indent=2, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_SETTINGS_SIZE: return None
        
        temp_path = ruta.with_suffix(f"{ruta.suffix}.tmp")
        ensure_safe_to_modify(str(temp_path))
        
        with open(temp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        
        if ruta.exists():
            try:
                shutil.copy2(ruta, ruta.with_suffix(".bak"))
            except OSError:
                pass
            
        os.replace(temp_path, ruta)
        _CACHE[str(ruta)] = (float(ruta.stat().st_mtime), cleaned_settings)
        return ruta
        
    except (TypeError, ValueError, OSError, IOError, PermissionError, UnsafePathError):
        if temp_path and temp_path.exists():
            try: temp_path.unlink()
            except OSError: pass
        return None

def update(changes: dict[str, Any], custom_base: PathLike | None = None) -> AppSettings:
    """
    Modificación incremental: carga la configuración actual, aplica parches
    validados y persiste solo si hubo cambios efectivos.
    """
    current = load(custom_base)
    modified = False
    for k, v in changes.items():
        key_enum = _STR_TO_ENUM.get(k)
        if key_enum and (validator := _VALIDATOR_MAP.get(key_enum)):
            val = validator(key_enum, v)
            if val is not None and val != current.get(k):
                current[k] = val
                modified = True
    if modified: save(current, custom_base)
    return current

def reset(custom_base: PathLike | None = None) -> AppSettings:
    """Restaura la configuración a los valores de fábrica definidos en DEFAULTS."""
    save(DEFAULTS, custom_base)
    return DEFAULTS.copy()

def get(key: str, custom_base: PathLike | None = None) -> Any:
    """Extrae un valor único de la configuración, usando el valor por defecto si no existe."""
    return load(custom_base).get(key, DEFAULTS.get(key))

def assistant_api_key(custom_base: PathLike | None = None) -> str:
    """Obtiene la clave API, priorizando la variable de entorno sobre el almacenamiento persistente."""
    if env_key := os.environ.get(API_KEY_ENV_VAR, "").strip(): return env_key
    return load(custom_base).get("asistente_clave_api", "").strip()

def assistant_enabled(custom_base: PathLike | None = None) -> bool:
    """Verifica la elegibilidad del asistente basado en configuración y presencia de clave válida."""
    if os.environ.get(API_KEY_ENV_VAR): return True
    settings = load(custom_base)
    return bool(settings.get("asistente_activado")) and bool(settings.get("asistente_clave_api", "").strip())

def describe(custom_base: PathLike | None = None) -> list[str]:
    """Genera una representación textual de las preferencias actuales para el usuario."""
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
