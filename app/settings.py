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
from pathlib import Path
from typing import Any, Final, TypeAlias, Callable, TypedDict

from safety import is_safe_to_modify, is_protected_path

PathLike: TypeAlias = str | Path

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
_current_path: Path | None = None
_last_mtime: float = -1.0
_path_cache: dict[str, Path] = {}
_val_cache: dict[tuple[str, Any], Any] = {}

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

_NUMERIC_LIMITS: Final[dict[str, tuple[int, int]]] = {
    "duplicados_tamano_minimo_kb": (0, 1024 * 1024),
    "top_archivos": (1, 500),
    "top_procesos": (1, 500),
}

class _Validators:
    """Namespace de validadores de tipo, asegurando integridad antes de guardar."""
    
    @staticmethod
    def _is_safe_path(path_obj: Path) -> bool:
        """Verifica que una ruta sea absoluta, no un enlace simbólico y pase las reglas de safety."""
        if not path_obj.is_absolute(): return False
        if len(path_obj.parts) < 2: return False
        if any(part in ('.', '..', '..\\', '../') for part in path_obj.parts): return False
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
    def bool(key: str, val: Any) -> bool | None:
        """Normaliza tipos mixtos (string/int/bool) a booleano, o None si es inválido."""
        cache_key = (key, val)
        if cache_key in _val_cache: return _val_cache[cache_key]
        
        if val is None: return None
        if isinstance(val, bool): return val
        if not isinstance(val, str): return None
        normalized = val.strip().lower()
        res = True if normalized in ("1", "true", "si", "sí", "yes") else (False if normalized in ("0", "false", "no", "none") else None)
        _val_cache[cache_key] = res
        return res

    @staticmethod
    def int(key: str, val: Any) -> int | None:
        """Parsea a entero y aplica límites definidos en _NUMERIC_LIMITS."""
        cache_key = (key, val)
        if cache_key in _val_cache: return _val_cache[cache_key]
        
        if val is None or isinstance(val, bool): return None
        try:
            parsed_value = int(val)
            min_limit, max_limit = _NUMERIC_LIMITS.get(key, (0, 10**9))
            res = max(min_limit, min(max_limit, parsed_value))
            _val_cache[cache_key] = res
            return res
        except (TypeError, ValueError, OverflowError): 
            return None

    @staticmethod
    def path(val: Any) -> str | None:
        """Valida y normaliza una ruta, retornando su versión absoluta o None si es insegura."""
        if val is None or not isinstance(val, (str, Path)): return None
        path_string = str(val).strip()
        if not path_string: return ""
        
        cache_key = ("path_str", path_string)
        if cache_key in _val_cache: return _val_cache[cache_key]
        
        try:
            path_obj = Path(path_string).expanduser()
            if _Validators._is_safe_path(path_obj):
                res = str(path_obj.absolute())
                _val_cache[cache_key] = res
                return res
            _val_cache[cache_key] = None
            return None
        except (OSError, RuntimeError, ValueError, TypeError, PermissionError, AttributeError):
            _val_cache[cache_key] = None
            return None

    @staticmethod
    def str(key: str, val: Any) -> str | None:
        """Valida strings asegurando no caracteres de control ni rutas inseguras."""
        if val is None or not isinstance(val, (str, Path)): return None
        text = str(val).strip()
        if any(c < ' ' for c in text) or ".." in text: return None
        
        if key == "ultima_carpeta": return _Validators.path(text)
        if not text: return "" if key == "asistente_clave_api" else None
        
        cache_key = (key, text)
        if cache_key in _val_cache: return _val_cache[cache_key]
        
        if key == "tema": res = text.lower() if text.lower() in VALID_THEMES else None
        elif key == "acento": res = text.lower() if text.lower() in VALID_ACCENTS else None
        else: res = text if len(text) <= 512 else None
        
        _val_cache[cache_key] = res
        return res

_VALIDATOR_MAP: Final[dict[str, Callable[[str, Any], Any]]] = {
    "tema": _Validators.str,
    "acento": _Validators.str,
    "abrir_en": _Validators.str,
    "ultima_carpeta": _Validators.str,
    "asistente_clave_api": _Validators.str,
    "asistente_modelo": _Validators.str,
    "mostrar_barras": _Validators.bool,
    "animaciones": _Validators.bool,
    "confirmar_siempre": _Validators.bool,
    "recordar_ultima_carpeta": _Validators.bool,
    "analisis_en_paralelo": _Validators.bool,
    "asistente_activado": _Validators.bool,
    "asistente_enviar_metricas": _Validators.bool,
    "duplicados_tamano_minimo_kb": _Validators.int,
    "top_archivos": _Validators.int,
    "top_procesos": _Validators.int
}

def settings_path(path_or_base: PathLike | None = None) -> Path:
    """Resuelve la ubicación del archivo de configuración, validando la ruta base."""
    if path_or_base is None: return SETTINGS_DIR / SETTINGS_FILE
    key = str(path_or_base)
    if key not in _path_cache:
        try:
            base = Path(key).expanduser().resolve(strict=False)
            _path_cache[key] = (base / SETTINGS_FILE) if _Validators._is_safe_path(base) else (SETTINGS_DIR / SETTINGS_FILE)
        except (OSError, RuntimeError, PermissionError):
            return SETTINGS_DIR / SETTINGS_FILE
    return _path_cache[key]

def validate(values: Any) -> AppSettings:
    """Aplica validadores a un diccionario arbitrario, retornando una config garantizada."""
    if not isinstance(values, dict): return DEFAULTS.copy()
    config = DEFAULTS.copy()
    for key, validator in _VALIDATOR_MAP.items():
        if key in values:
            val = validator(key, values.get(key))
            if val is not None: config[key] = val
    return config

def load(path_or_base: PathLike | None = None) -> AppSettings:
    """Carga configuraciones desde disco. Implementa caché basado en mtime."""
    global _cached_settings, _current_path, _last_mtime
    ruta = settings_path(path_or_base)
    try:
        if not ruta.exists(): return DEFAULTS.copy()
        stats = ruta.stat()
        if _cached_settings is not None and _current_path == ruta and _last_mtime == stats.st_mtime:
            return _cached_settings.copy()
        if 0 < stats.st_size <= MAX_SETTINGS_SIZE:
            with open(ruta, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _cached_settings = validate(data)
                _current_path, _last_mtime = ruta, stats.st_mtime
                return _cached_settings.copy()
    except (OSError, PermissionError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        pass
    return DEFAULTS.copy()

def save(values: Any, path_or_base: PathLike | None = None) -> Path | None:
    """Guarda configuraciones de forma atómica. Falla si la ruta es protegida."""
    global _cached_settings, _current_path, _last_mtime
    if not isinstance(values, dict): return None
    ruta = settings_path(path_or_base)
    if is_protected_path(str(ruta)) or not is_safe_to_modify(str(ruta)):
        return None
    cleaned_settings = validate(values)
    
    # Pre-serialización para verificar tamaño y prevenir DoS por escritura
    json_data = json.dumps(cleaned_settings, indent=2, ensure_ascii=False)
    if len(json_data.encode("utf-8")) > MAX_SETTINGS_SIZE:
        return None

    if cleaned_settings.get("asistente_activado") and not (cleaned_settings.get("asistente_clave_api") or os.environ.get(API_KEY_ENV_VAR)):
        cleaned_settings["asistente_activado"] = False
    if _cached_settings == cleaned_settings and _current_path == ruta: return ruta
    try:
        if not ruta.parent.exists(): ruta.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(ruta.parent, os.W_OK): return None
        temp = ruta.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as f:
            f.write(json_data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, ruta)
        _cached_settings, _current_path = cleaned_settings, ruta
        _last_mtime = ruta.stat().st_mtime
        return ruta
    except (OSError, IOError, PermissionError, RuntimeError):
        if 'temp' in locals():
            try: os.remove(temp)
            except OSError: pass
        return None

def update(changes: dict[str, Any], path_or_base: PathLike | None = None) -> AppSettings:
    """Actualiza solo claves específicas en el archivo de configuración."""
    current = load(path_or_base)
    needs_save = False
    for k, v in changes.items():
        if k in _VALIDATOR_MAP and current.get(k) != v:
            current[k] = v
            needs_save = True
    if needs_save: save(current, path_or_base)
    return current

def reset(path_or_base: PathLike | None = None) -> AppSettings:
    """Restaura la configuración a valores de fábrica."""
    save(DEFAULTS, path_or_base)
    return DEFAULTS.copy()

def get(key: str, path_or_base: PathLike | None = None) -> Any:
    """Accesor rápido para un valor individual."""
    return load(path_or_base).get(key, DEFAULTS.get(key))

def assistant_api_key(path_or_base: PathLike | None = None) -> str:
    """Prioriza variables de entorno sobre el archivo de configuración para la API Key."""
    env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return env_key if env_key else load(path_or_base).get("asistente_clave_api", "").strip()

def assistant_enabled(path_or_base: PathLike | None = None) -> bool:
    """Verifica si el asistente puede operar (configurado y con API Key presente)."""
    settings = load(path_or_base)
    return bool(settings.get("asistente_activado") and (os.environ.get(API_KEY_ENV_VAR) or settings.get("asistente_clave_api")))

def describe(path_or_base: PathLike | None = None) -> list[str]:
    """Genera un reporte legible de la configuración para mostrar en la interfaz."""
    current = load(path_or_base)
    key = assistant_api_key(path_or_base)
    origin = f"variable de entorno {API_KEY_ENV_VAR}" if os.environ.get(API_KEY_ENV_VAR) else ("archivo de configuración" if key else "no configurada")
    return [
        "Configuración actual", "", f"  Archivo: {settings_path(path_or_base)}", "",
        "  Apariencia", f"    Tema: {current['tema']}", f"    Acento: {current['acento']}",
        f"    Barras visuales: {'sí' if current['mostrar_barras'] else 'no'}", "",
        "  Comportamiento", f"    Confirmar siempre: {'sí' if current['confirmar_siempre'] else 'no'}",
        f"    Pestaña inicial: {current['abrir_en']}", f"    Recordar carpeta: {'sí' if current['recordar_ultima_carpeta'] else 'no'}", "",
        "  Rendimiento", f"    Duplicados desde: {current['duplicados_tamano_minimo_kb']} KB",
        f"    Top de archivos: {current['top_archivos']}", f"    Análisis en paralelo: {'sí' if current['analisis_en_paralelo'] else 'no'}", "",
        "  Asistente IA", f"    Activado: {'sí' if current['asistente_activado'] else 'no'}",
        f"    Clave: {origin}", f"    Modelo: {current['asistente_modelo']}", ""
    ]
