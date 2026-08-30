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
from typing import Iterable, Sequence, Dict, List, Optional, Callable, Set, Union
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

# Mapa de navegadores soportados a sus rutas relativas dentro de LOCALAPPDATA.
BROWSER_CACHE_PATHS: Dict[str, str] = {
    "Google Chrome": r"Google\Chrome\User Data\Default\Cache",
    "Microsoft Edge": r"Microsoft\Edge\User Data\Default\Cache",
    "Brave": r"BraveSoftware\Brave-Browser\User Data\Default\Cache",
    "Opera": r"Opera Software\Opera Stable\Cache",
    "Vivaldi": r"Vivaldi\User Data\Default\Cache",
    "Chrome (código)": r"Google\Chrome\User Data\Default\Code Cache",
    "Edge (código)": r"Microsoft\Edge\User Data\Default\Code Cache",
    "Chrome (GPU)": r"Microsoft\Edge\User Data\Default\GPUCache",
}

# Conjunto de nombres de archivos/carpetas que NUNCA deben ser procesados
# por contener datos persistentes críticos para el usuario.
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
# Atributos: FILE_ATTRIBUTE_HIDDEN (0x01) | FILE_ATTRIBUTE_SYSTEM (0x02) | FILE_ATTRIBUTE_REPARSE_POINT (0x400)
SYSTEM_HIDDEN_FLAGS: int = 0x01 | 0x02 | 0x400

# Tipo alias para claridad en funciones de callback de sistema
JunctionChecker = Callable[[str], bool]

@dataclass
class BrowserCache:
    """Representación de una carpeta de caché detectada y su peso en disco."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB con precisión de 2 decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)


def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """
    Carga dinámicamente kernel32.dll para llamadas a la API de Win32.
    Retorna None si no es Windows o si la carga falla.
    """
    if os.name != 'nt':
        return None
    try:
        if not hasattr(ctypes, 'windll'):
            return None
        return ctypes.windll.kernel32
    except (AttributeError, OSError, ValueError, RuntimeError):
        return None


def base_directories() -> List[Path]:
    """
    Determina los directorios base de perfil de usuario (LOCALAPPDATA).
    Verifica la existencia y los permisos mediante `safety.py` antes de retornar.
    """
    local_env = os.environ.get("LOCALAPPDATA")
    if not local_env:
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
    Valida si 'real_target' está contenido estrictamente dentro de 'real_base'
    usando resolución de rutas para prevenir ataques de directory traversal.
    """
    if not isinstance(real_target, Path) or not isinstance(real_base, Path):
        return False
    try:
        target = str(real_target.resolve())
        base = str(real_base.resolve())
        return os.path.commonpath([target, base]) == base
    except (OSError, ValueError, RuntimeError, PermissionError):
        return False


def _is_excluded_file(name: str) -> bool:
    """Valida si un nombre de archivo está en la lista de bloqueo permanente."""
    if not isinstance(name, str) or not name:
        return True
    return name.lower() in NEVER_TOUCH


def __is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """
    Verifica atributos de sistema u ocultos usando la API de Win32.
    El chequeo es preventivo para evitar escanear zonas de protección del SO.
    """
    if kernel32 is None or not isinstance(entry_path, str) or not entry_path:
        return False
    try:
        if not os.path.isabs(entry_path):
            return False
        attrs: int = kernel32.GetFileAttributesW(entry_path)
        if attrs == 0xFFFFFFFF:
            return False 
        return bool(attrs & SYSTEM_HIDDEN_FLAGS)
    except (OSError, AttributeError, TypeError, ValueError, MemoryError, ctypes.ArgumentError):
        return False


def _should_skip_entry(entry: os.DirEntry, kernel32: Optional[ctypes.WinDLL], is_junction_fn: JunctionChecker) -> bool:
    """
    Filtra entradas del sistema (ocultas, junctions, symlinks) que no deben
    ser recorridas para evitar loops infinitos o modificación de archivos críticos.
    """
    if entry is None or not hasattr(entry, 'name') or not hasattr(entry, 'path'):
        return True
    
    name = entry.name
    if not name or _is_excluded_file(name):
        return True
        
    try:
        if entry.is_symlink() or is_junction_fn(entry.path) or os.path.ismount(entry.path):
            return True
        if __is_system_hidden(entry.path, kernel32):
            return True
    except (OSError, PermissionError, FileNotFoundError):
        return True
    return False


def _is_safe_to_traverse(path_obj: Path, base_check_path: Optional[Path]) -> bool:
    """Verifica si el acceso a la ruta es seguro según las reglas del proyecto."""
    if not isinstance(path_obj, Path):
        return False
    try:
        if not path_obj.exists() or not is_safe_to_modify(path_obj) or is_protected_path(path_obj):
            return False
        if base_check_path and not _is_path_inside_base(path_obj, base_check_path):
            return False
        return True
    except (OSError, RuntimeError):
        return False


def _sum_directory_recursive(
    root_dir: str, 
    is_junction_fn: JunctionChecker, 
    kernel32: Optional[ctypes.WinDLL],
    memo: Dict[str, int],
    base_check_path: Optional[Path] = None,
    depth: int = 0
) -> int:
    """
    Calcula recursivamente el peso de una carpeta. Utiliza `memo` para evitar
    re-procesamiento y `MAX_SCAN_DEPTH` para prevenir desbordamiento de pila.
    """
    if depth > MAX_SCAN_DEPTH or not isinstance(root_dir, str) or not root_dir:
        return 0

    try:
        root_path = Path(root_dir).resolve(strict=True)
        if not _is_safe_to_traverse(root_path, base_check_path):
            return 0
    except (OSError, RuntimeError):
        return 0
        
    root_abs = str(root_path)
    if root_abs in memo:
        return memo[root_abs]

    if root_path.is_symlink() or is_junction_fn(root_abs) or os.path.ismount(root_abs):
        return 0

    total: int = 0
    try:
        with os.scandir(root_abs) as it:
            for entry in it:
                if _should_skip_entry(entry, kernel32, is_junction_fn):
                    continue
                
                try:
                    if entry.is_dir(follow_symlinks=False):
                        sub_path = Path(entry.path)
                        if _is_safe_to_traverse(sub_path, base_check_path):
                            total += _sum_directory_recursive(
                                entry.path, is_junction_fn, kernel32, memo, base_check_path, depth + 1
                            )
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except (OSError, PermissionError):
                    continue
    except (PermissionError, OSError):
        return 0
    
    memo[root_abs] = total
    return total


def directory_size(path: Union[str, Path, None]) -> int:
    """
    Calcula el peso total de una carpeta tras validar seguridad con `is_safe_to_modify`.
    """
    if path is None:
        return 0
    try:
        p_obj = Path(path)
        if not p_obj.exists():
            return 0
        
        p_res = p_obj.resolve(strict=True)
        if not p_res.is_dir() or os.path.ismount(str(p_res)) or is_protected_path(p_res) or not is_safe_to_modify(p_res):
            return 0
        
        is_junction: JunctionChecker = getattr(os.path, 'isjunction', lambda _: False)
        return _sum_directory_recursive(str(p_res), is_junction, _get_kernel32(), {})
    except (OSError, PermissionError, RuntimeError, ValueError):
        return 0


def _is_valid_cache_path(candidate: Path, base_path: Path, is_junction_fn: JunctionChecker) -> bool:
    """
    Valida que la ruta candidata a caché sea un directorio real, seguro de
    modificar, y que resida dentro del perfil de usuario permitido.
    """
    if not isinstance(candidate, Path) or not isinstance(base_path, Path):
        return False
    try:
        if not candidate.exists():
            return False
            
        real_candidate = candidate.resolve(strict=True)
        
        if (real_candidate.is_symlink() or is_junction_fn(str(real_candidate)) or os.path.ismount(str(real_candidate)) or
            not real_candidate.is_dir() or is_protected_path(real_candidate) or 
            not is_safe_to_modify(real_candidate) or
            not _is_path_inside_base(real_candidate, base_path) or 
            _is_excluded_file(real_candidate.name)):
            return False
        return True
    except (OSError, PermissionError, RuntimeError, ValueError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[Dict[str, str]] = None
) -> List[BrowserCache]:
    """
    Escanea las rutas definidas en `cache_paths`. Utiliza un diccionario de 
    memoización persistente para evitar re-cálculos redundantes entre navegadores.
    """
    raw_bases = bases if bases is not None else base_directories()
    browser_map = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    
    if not raw_bases or not isinstance(browser_map, dict):
        return []
    
    is_junction: JunctionChecker = getattr(os.path, 'isjunction', lambda _: False)
    k32 = _get_kernel32()
    
    perf_cache: Dict[str, int] = {}
    found: List[BrowserCache] = []
    
    for base in raw_bases:
        if not isinstance(base, Path): 
            continue
        try:
            real_base = base.resolve(strict=True)
            for browser_name, rel_str in browser_map.items():
                if not isinstance(rel_str, str) or not rel_str: 
                    continue
                candidate = real_base.joinpath(*rel_str.split("\\"))
                
                if _is_valid_cache_path(candidate, real_base, is_junction):
                    c_path = candidate.resolve()
                    path_str = str(c_path)
                    
                    size = _sum_directory_recursive(path_str, is_junction, k32, perf_cache, real_base)
                    if size > 0:
                        found.append(BrowserCache(browser_name, c_path, size))
        except (OSError, PermissionError, TypeError): 
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Calcula el acumulado de bytes de todas las cachés identificadas."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera una representación de texto para el reporte de salud del sistema."""
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
