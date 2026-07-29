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
from pathlib import Path
from typing import Any, Final, TypeAlias

from safety import is_safe_to_modify, ensure_safe_to_modify

PathLike: TypeAlias = str | Path

__all__ = [
    "DEFAULTS",
    "SETTINGS_DIR",
    "SETTINGS_FILE",
    "API_KEY_ENV_VAR",
    "VALID_THEMES",
    "VALID_ACCENTS",
    "settings_path",
    "load",
    "save",
    "update",
    "reset",
    "validate",
    "get",
    "assistant_api_key",
    "assistant_enabled",
    "describe",
]

SETTINGS_DIR: Final = Path("~/LimpiezaTotalOmega").expanduser()
SETTINGS_FILE: Final = "config.json"
MAX_SETTINGS_SIZE: Final = 1024 * 64  # Límite de 64KB para evitar ataques de desbordamiento

# Variable de entorno preferida para la clave del asistente.
API_KEY_ENV_VAR: Final = "OMEGA_GEMINI_KEY"

VALID_THEMES: Final = ("oscuro", "claro", "sistema")
VALID_ACCENTS: Final = ("menta", "violeta", "magenta", "cian", "ambar")

# Caché interno para evitar lectura repetitiva de disco y re-validación
_cached_settings: dict[str, Any] | None = None
_last_path_str: str | None = None
_last_mtime: float = 0.0

# Valores de fábrica. Cada clave define además el tipo esperado: si el archivo
# trae otra cosa, se descarta ese valor y se usa este.
DEFAULTS: Final[dict[str, Any]] = {
    # Apariencia
    "tema": "oscuro",
    "acento": "menta",
    "mostrar_barras": True,
    "animaciones": True,
    # Comportamiento
    "confirmar_siempre": True,
    "abrir_en": "Salud",
    "recordar_ultima_carpeta": True,
    "ultima_carpeta": "",
    # Límites de análisis (rendimiento)
    "duplicados_tamano_minimo_kb": 64,
    "top_archivos": 15,
    "top_procesos": 15,
    "analisis_en_paralelo": True,
    # Asistente IA
    "asistente_activado": False,
    "asistente_clave_api": "",
    "asistente_enviar_metricas": True,
    "asistente_modelo": "gemini-3.1-flash-lite",
}

# Límites de los valores numéricos permitidos para prevenir configuraciones extremas.
# Estructura: clave -> (minimo_inclusivo, maximo_inclusivo).
_NUMERIC_LIMITS: Final = {
    "duplicados_tamano_minimo_kb": (0, 1024 * 1024),
    "top_archivos": (1, 500),
    "top_procesos": (1, 500),
}

# Pre-cálculo para optimizar validación al verificar claves presentes en DEFAULTS
_DEFAULTS_KEYS: Final = set(DEFAULTS.keys())


def _coerce_bool(raw_value: Any) -> bool | None:
    """
    Normaliza entradas no booleanas (strings tipo 'true'/'1'/'si') a booleano real.
    
    Returns:
        bool: El valor normalizado.
        None: Si la entrada no representa un booleano válido.
    """
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in ("1", "true", "si", "sí", "yes")
    return None


def _coerce_int(raw_value: Any, setting_key: str) -> int | None:
    """
    Intenta convertir a entero, aplicando límites definidos en _NUMERIC_LIMITS.
    
    Args:
        raw_value: El valor crudo extraído del archivo JSON.
        setting_key: La clave de configuración para buscar límites asociados.
    
    Returns:
        int: El valor dentro del rango permitido (o truncado al límite).
        None: Si la conversión falla.
    """
    if not isinstance(raw_value, (int, str)):
        return None
    try:
        parsed_val = int(raw_value)
        min_limit, max_limit = _NUMERIC_LIMITS.get(setting_key, (0, 10**9))
        return max(min_limit, min(max_limit, parsed_val))
    except (TypeError, ValueError):
        return None


def _validate_str(clave: str, valor: Any) -> str | None:
    """
    Valida strings de configuración. Para 'ultima_carpeta', realiza chequeo
    de seguridad contra el sistema de archivos mediante `is_safe_to_modify`.
    """
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    if not texto:
        return "" if clave == "ultima_carpeta" else None
        
    if clave == "tema" and texto.lower() not in VALID_THEMES:
        return None
    if clave == "acento" and texto.lower() not in VALID_ACCENTS:
        return None
    if clave == "ultima_carpeta":
        try:
            ruta_candidata = Path(texto).expanduser().resolve()
            if not is_safe_to_modify(str(ruta_candidata)):
                return None
            return str(ruta_candidata)
        except (OSError, RuntimeError, ValueError):
            return None
    return texto.lower() if clave in ("tema", "acento") else texto


def _apply_validation_by_type(clave: str, valor: Any, defecto: Any) -> Any:
    """
    Selecciona la estrategia de validación basada en el tipo del valor por defecto.
    Asegura que el dato resultante mantenga la consistencia tipológica del esquema.
    """
    if valor is None:
        return None
    
    dispatch = {
        bool: lambda v: _coerce_bool(v),
        int: lambda v: _coerce_int(v, clave),
        str: lambda v: _validate_str(clave, v)
    }
    
    func = dispatch.get(type(defecto))
    return func(valor) if func else None


def settings_path(path_or_base: PathLike | None = None) -> Path:
    """Resuelve la ruta absoluta del archivo de configuración, asegurando seguridad."""
    if path_or_base is not None:
        base = Path(path_or_base).expanduser().resolve()
    else:
        base = SETTINGS_DIR
    
    # Aseguramos que la carpeta base sea segura antes de trabajar en ella
    ensure_safe_to_modify(str(base))
    return base / SETTINGS_FILE


def validate(values: Any) -> dict[str, Any]:
    """
    Valida un diccionario externo contra el esquema de DEFAULTS.
    Descarta cualquier clave desconocida y corrige valores inválidos a sus defaults.
    """
    limpio = dict(DEFAULTS)
    if not isinstance(values, dict):
        return limpio

    for clave in _DEFAULTS_KEYS:
        if clave in values:
            coerced = _apply_validation_by_type(clave, values[clave], DEFAULTS[clave])
            if coerced is not None:
                limpio[clave] = coerced

    return limpio


def load(path_or_base: PathLike | None = None) -> dict[str, Any]:
    """Carga configuración con caché por mtime y chequeos de seguridad."""
    global _cached_settings, _last_path_str, _last_mtime
    
    try:
        ruta = settings_path(path_or_base)
        if not ruta.exists():
            return dict(DEFAULTS)
        
        stat = ruta.stat()
        if stat.st_size > MAX_SETTINGS_SIZE:
            return dict(DEFAULTS)
            
        ruta_str = str(ruta)
        if _cached_settings is not None and ruta_str == _last_path_str and stat.st_mtime == _last_mtime:
            return _cached_settings

        raw_data = ruta.read_text(encoding="utf-8")
        data = json.loads(raw_data)
        
        if not isinstance(data, dict):
            return dict(DEFAULTS)

        _cached_settings = validate(data)
        _last_path_str = ruta_str
        _last_mtime = stat.st_mtime
        return _cached_settings
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # Si falló la lectura, retornamos defaults para no romper la app
        return dict(DEFAULTS)


def save(values: Any, path_or_base: PathLike | None = None) -> Path | None:
    """Persistencia atómica: escribe en archivo temporal y luego reemplaza."""
    global _cached_settings, _last_path_str, _last_mtime
    
    try:
        ruta = settings_path(path_or_base)
        limpio = validate(values)
        json_data = json.dumps(limpio, indent=2, ensure_ascii=False)
        
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ensure_safe_to_modify(str(ruta.parent))
        
        # Uso de with para asegurar cierre de descriptor y evitar fugas si falla el fsync
        with tempfile.NamedTemporaryFile("w", dir=ruta.parent, delete=False, encoding="utf-8") as tf:
            tf.write(json_data)
            tf.flush()
            os.fsync(tf.fileno())
            temp_name = tf.name
        
        try:
            os.replace(temp_name, ruta)
        except OSError:
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise

        _cached_settings = limpio
        _last_path_str = str(ruta)
        _last_mtime = ruta.stat().st_mtime
        return ruta
    except (OSError, RuntimeError, PermissionError):
        return None


def update(changes: dict[str, Any], path_or_base: PathLike | None = None) -> dict[str, Any]:
    """Actualiza solo las claves provistas y persiste el estado completo."""
    actual = load(path_or_base)
    actual.update(changes)
    save(actual, path_or_base)
    return actual


def reset(path_or_base: PathLike | None = None) -> dict[str, Any]:
    """Resetea el archivo de configuración a los valores de fábrica."""
    save(dict(DEFAULTS), path_or_base)
    return dict(DEFAULTS)


def get(key: str, path_or_base: PathLike | None = None) -> Any:
    """Getter de alto nivel: obtiene valor configurado o el default si no existe."""
    return load(path_or_base).get(key, DEFAULTS.get(key))


def assistant_api_key(path_or_base: PathLike | None = None) -> str:
    """Obtiene API key con prioridad absoluta en variable de entorno."""
    desde_entorno = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return desde_entorno or load(path_or_base).get("asistente_clave_api", "").strip()


def assistant_enabled(path_or_base: PathLike | None = None) -> bool:
    """Verifica si el asistente está activado y posee una API key válida."""
    config = load(path_or_base)
    return bool(config.get("asistente_activado")) and bool(assistant_api_key(path_or_base))


def describe(path_or_base: PathLike | None = None) -> list[str]:
    """Genera una representación textual legible de la configuración actual."""
    actual = load(path_or_base)
    clave = assistant_api_key(path_or_base)
    origen = f"variable de entorno {API_KEY_ENV_VAR}" if os.environ.get(API_KEY_ENV_VAR) else ("archivo de configuración" if clave else "no configurada")

    return [
        "Configuración actual", "", f"  Archivo: {settings_path(path_or_base)}", "",
        "  Apariencia", f"    Tema: {actual['tema']}", f"    Acento: {actual['acento']}",
        f"    Barras visuales: {'sí' if actual['mostrar_barras'] else 'no'}", "",
        "  Comportamiento", f"    Confirmar siempre: {'sí' if actual['confirmar_siempre'] else 'no'}",
        f"    Pestaña inicial: {actual['abrir_en']}", f"    Recordar carpeta: {'sí' if actual['recordar_ultima_carpeta'] else 'no'}", "",
        "  Rendimiento", f"    Duplicados desde: {actual['duplicados_tamano_minimo_kb']} KB",
        f"    Top de archivos: {actual['top_archivos']}", f"    Análisis en paralelo: {'sí' if actual['analisis_en_paralelo'] else 'no'}", "",
        "  Asistente IA", f"    Activado: {'sí' if actual['asistente_activado'] else 'no'}",
        f"    Clave: {origen}", f"    Modelo: {actual['asistente_modelo']}", ""
    ]
