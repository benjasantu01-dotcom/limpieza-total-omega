"""
browser.py — detección de cachés de navegadores.

SOLO LECTURA: detecta qué navegadores hay instalados, dónde guardan su
caché y cuánto ocupa. **No borra nada.** Limpiar caché de navegador es
seguro en general, pero borrar la carpeta equivocada del perfil puede
hacerte perder sesiones, contraseñas guardadas o marcadores, así que acá
solo se reportan las carpetas y su tamaño; la limpieza pasa por la carpeta
de revisión de `organizer.py`, con confirmación del usuario.

A propósito se listan solo carpetas de CACHÉ (datos regenerables) y nunca
las de credenciales o marcadores, ni siquiera para reportar su tamaño.
La exclusión se gestiona mediante la constante `NEVER_TOUCH`.

Diseño testeable: `detect_profiles` recibe la carpeta base por parámetro,
así en CI se puede simular una instalación con carpetas temporales.
"""

from __future__ import annotations
import os
import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Dict, List, Optional, Callable, Union, TypeAlias

from safety import is_protected_path, is_safe_to_modify

__all__ = [
    "BrowserCache",
    "BROWSER_CACHE_PATHS",
    "NEVER_TOUCH",
    "base_directories",
    "detect_profiles",
    "directory_size",
    "total_cache_bytes",
    "summarize",
    "SAFETY_NOTE",
]

# Tipos definidos para mejorar la legibilidad de la arquitectura del módulo
JunctionChecker: TypeAlias = Callable[[str], bool]
BrowserMap: TypeAlias = Dict[str, str]

def _is_junction_default(path: str) -> bool:
    """Fallback si el entorno no soporta la detección de junctions."""
    return False

# Acceso seguro a la funcionalidad de junctions si existe en el runtime
_IS_JUNCTION_FN: JunctionChecker = getattr(os.path, 'isjunction', _is_junction_default)

# Mapeo de nombres descriptivos a rutas relativas dentro de LOCALAPPDATA.
BROWSER_CACHE_PATHS: BrowserMap = {
    "Google Chrome": r"Google\Chrome\User Data\Default\Cache",
    "Microsoft Edge": r"Microsoft\Edge\User Data\Default\Cache",
    "Brave": r"BraveSoftware\Brave-Browser\User Data\Default\Cache",
    "Opera": r"Opera Software\Opera Stable\Cache",
    "Vivaldi": r"Vivaldi\User Data\Default\Cache",
    "Chrome (código)": r"Google\Chrome\User Data\Default\Code Cache",
    "Edge (código)": r"Microsoft\Edge\User Data\Default\Code Cache",
    "Chrome (GPU)": r"Microsoft\Edge\User Data\Default\GPUCache",
}

NEVER_TOUCH: frozenset[str] = frozenset({
    "login data", "cookies", "web data", "bookmarks", "history",
    "preferences", "local state", "extensions", "profile",
})

SAFETY_NOTE: str = (
    "Solo se listan carpetas de caché, que el navegador regenera solo. "
    "Nunca se tocan contraseñas, cookies, marcadores ni historial. "
    "Cerrá el navegador antes de limpiar su caché, o los archivos en uso "
    "no se van a poder mover."
)

MAX_SCAN_DEPTH: int = 15
MAX_PATH_LEN: int = 260
SYSTEM_HIDDEN_FLAGS: int = 0x01 | 0x02 | 0x400
ERROR_SHARING_VIOLATION: int = 32

@dataclass
class BrowserCache:
    """
    Representación de una carpeta de caché detectada y su peso en disco.
    
    Attributes:
        browser: Nombre comercial del navegador.
        path: Ruta absoluta al directorio de caché.
        size_bytes: Tamaño total en bytes detectado.
    """
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB con precisión de 2 decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)


def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """
    Carga kernel32.dll para acceder a atributos de archivo de bajo nivel.
    Retorna None si no es Windows o si la carga falla.
    """
    if os.name != 'nt':
        return None
    try:
        # Se fuerza el uso de la API W (Wide) para manejar Unicode correctamente.
        # use_last_error=True permite capturar errores de sistema sin excepciones.
        dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        if hasattr(dll, 'GetFileAttributesW'):
            return dll
        return None
    except (OSError, RuntimeError, AttributeError):
        return None


def base_directories() -> List[Path]:
    """
    Determina la ruta raíz de perfiles de usuario (%LOCALAPPDATA%).
    Valida la existencia y seguridad de la ruta antes de retornarla.
    """
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env:
        return []
    
    try:
        path_local = Path(local_env).resolve(strict=True)
        if path_local.is_dir() and is_safe_to_modify(path_local) and not is_protected_path(path_local):
            return [path_local]
        return []
    except (OSError, RuntimeError, ValueError):
        return []


def _is_path_inside_base(real_target: Path, real_base: Path) -> bool:
    """
    Verifica mediante resolución absoluta que la ruta objetivo esté bajo la base.
    
    Es fundamental resolver ambas rutas antes de comparar para neutralizar 
    ataques de 'Directory Traversal' (ej. uso de '..') y asegurar que el 
    alcance se limita estrictamente a los directorios permitidos.
    """
    if real_target is None or real_base is None:
        return False
    try:
        target_str: str = str(real_target.resolve(strict=True))
        base_str: str = str(real_base.resolve(strict=True))
        if len(target_str) >= MAX_PATH_LEN or any(c in target_str for c in '\0\r\n'):
            return False
        return target_str.startswith(base_str + os.sep) or target_str == base_str
    except (OSError, RuntimeError, ValueError):
        return False


def _is_excluded_file(name: Optional[str]) -> bool:
    """Valida si el nombre de archivo está en la lista de elementos protegidos."""
    return name is not None and name.lower() in NEVER_TOUCH


def __is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Usa Win32 GetFileAttributesW para detectar atributos Oculto/Sistema."""
    if kernel32 is None or not isinstance(entry_path, str) or not entry_path:
        return False
    try:
        attrs: int = kernel32.GetFileAttributesW(entry_path)
        if attrs == 0xFFFFFFFF:
            return False 
        return bool(attrs & SYSTEM_HIDDEN_FLAGS)
    except (AttributeError, TypeError, ctypes.ArgumentError, OSError):
        return False


def _should_skip_entry(entry: os.DirEntry, kernel32: Optional[ctypes.WinDLL], is_junction_fn: JunctionChecker) -> bool:
    """
    Filtro de seguridad para ignorar entradas que sean symlinks, junctions, 
    montajes de red o archivos de sistema críticos según las políticas internas.
    """
    if entry is None:
        return True
    
    try:
        name = entry.name
        if not name or _is_excluded_file(name):
            return True
            
        path = entry.path
        if not path or len(path) >= MAX_PATH_LEN or any(c in path for c in '\0\r\n'):
            return True
        
        # Validar tipo de entrada antes de operar para evitar errores de acceso
        if entry.is_symlink() or is_junction_fn(path):
            return True
            
        if __is_system_hidden(path, kernel32):
            return True
            
    except (OSError, PermissionError, UnicodeEncodeError):
        return True
    return False


def _is_safe_to_traverse(path_obj: Path, base_check_path: Optional[Path]) -> bool:
    """
    Valida que la ruta exista, sea segura de modificar según `safety.py` 
    y mantenga la integridad jerárquica frente a la carpeta base definida.
    """
    if path_obj is None:
        return False
    try:
        if not path_obj.exists() or not path_obj.is_dir():
            return False
        p_res = path_obj.resolve(strict=True)
        if not is_safe_to_modify(p_res) or is_protected_path(p_res):
            return False
        if base_check_path and not _is_path_inside_base(p_res, base_check_path):
            return False
        return True
    except (OSError, RuntimeError, PermissionError, ValueError):
        return False


def _sum_directory_recursive(
    root_abs: str, 
    is_junction_fn: JunctionChecker, 
    kernel32: Optional[ctypes.WinDLL],
    memo: Dict[str, int],
    base_check_path: Optional[Path] = None,
    depth: int = 0
) -> int:
    """
    Cálculo de tamaño mediante DFS con control de profundidad y manejo de bloqueos.
    """
    if not root_abs or depth > MAX_SCAN_DEPTH:
        return 0
    
    if root_abs in memo:
        return memo[root_abs]
    
    # Validar seguridad de la ruta actual antes de procesar
    if not is_safe_to_modify(Path(root_abs)) or is_protected_path(Path(root_abs)):
        return 0
    
    total: int = 0
    try:
        with os.scandir(root_abs) as it:
            for entry in it:
                if _should_skip_entry(entry, kernel32, is_junction_fn):
                    continue
                
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += _sum_directory_recursive(
                            entry.path, is_junction_fn, kernel32, memo, base_check_path, depth + 1
                        )
                    elif entry.is_file(follow_symlinks=False):
                        s = entry.stat(follow_symlinks=False)
                        total += int(s.st_size)
                except (OSError, PermissionError) as e:
                    # Ignorar errores de acceso (ex. archivo bloqueado por el sistema/navegador)
                    if getattr(e, 'winerror', None) == ERROR_SHARING_VIOLATION:
                        continue
                    continue
    except (PermissionError, OSError):
        return 0
    
    memo[root_abs] = total
    return total


def directory_size(path: Union[str, Path, None]) -> int:
    """Interfaz pública para obtener el tamaño de una ruta tras validarla."""
    if path is None:
        return 0
    try:
        p: Path = Path(path)
        if not p.is_absolute() or not p.is_dir():
            return 0
        if not _is_safe_to_traverse(p, None):
            return 0
        return _sum_directory_recursive(str(p.resolve(strict=True)), _IS_JUNCTION_FN, _get_kernel32(), {})
    except (OSError, PermissionError, RuntimeError, ValueError):
        return 0


def _is_valid_cache_path(candidate: Path, base_path: Path, is_junction_fn: JunctionChecker) -> bool:
    """Verifica si una carpeta candidata es una ubicación de caché legítima y segura."""
    if candidate is None:
        return False
    try:
        if not candidate.exists() or not candidate.is_dir():
            return False
        
        real_candidate = candidate.resolve(strict=True)
        
        if not real_candidate.is_dir() or not _is_path_inside_base(real_candidate, base_path):
            return False
            
        if not is_safe_to_modify(real_candidate) or is_protected_path(real_candidate):
            return False
            
        if (real_candidate.is_symlink() or is_junction_fn(str(real_candidate)) or 
            _is_excluded_file(real_candidate.name)):
            return False
        return True
    except (OSError, PermissionError, RuntimeError, ValueError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[BrowserMap] = None
) -> List[BrowserCache]:
    """Escanea en busca de perfiles y calcula la ocupación de cada caché."""
    raw_bases: Sequence[Path] = bases if bases is not None else base_directories()
    browser_map: BrowserMap = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    
    k32: Optional[ctypes.WinDLL] = _get_kernel32()
    perf_cache: Dict[str, int] = {}
    found: List[BrowserCache] = []
    
    for base in raw_bases:
        if not isinstance(base, Path) or not base.is_dir():
            continue
        try:
            real_base = base.resolve(strict=True)
            for browser_name, rel_str in browser_map.items():
                if not rel_str:
                    continue
                candidate = real_base.joinpath(*rel_str.split("\\"))
                
                if not _is_valid_cache_path(candidate, real_base, _IS_JUNCTION_FN):
                    continue
                    
                c_path = candidate.resolve(strict=True)
                size = _sum_directory_recursive(str(c_path), _IS_JUNCTION_FN, k32, perf_cache, real_base)
                if size > 0:
                    found.append(BrowserCache(str(browser_name), c_path, size))
        except (OSError, PermissionError, TypeError, ValueError):
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Calcula el peso total acumulado en bytes."""
    if caches is None:
        return 0
    try:
        return sum(cache.size_bytes for cache in caches if isinstance(cache, BrowserCache))
    except (TypeError, AttributeError):
        return 0


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Formatea la información de caché para la UI."""
    current_caches = caches if caches is not None else detect_profiles()
    if not current_caches:
        return ["No se detectaron cachés de navegador en este sistema."]
        
    total_mb = round(total_cache_bytes(current_caches) / (1024 * 1024), 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
