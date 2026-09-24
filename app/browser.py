"""
browser.py — detección de cachés de navegadores.

SOLO LECTURA: detecta qué navegadores hay instalados, dónde guardan su
caché y cuánto ocupa. **No borra nada.** Limpiar caché de navegador es
seguro en general, pero borrar la carpeta equivocada del perfil puede
hacerte perder sesiones, contraseñas guardadas o marcadores, así que acá
solo se reportan las carpetas y su tamaño; la limpieza pasa por la carpeta
de revisión de `organizer.py`, con confirmación del usuario.

GARANTÍAS DE OPERACIÓN:
- El módulo nunca escala privilegios ni intenta modificar el disco.
- Las excepciones en el acceso a archivos (archivos bloqueados, permisos) 
  se capturan silenciosamente para evitar abortos en escaneos masivos.
- Solo se procesan rutas contenidas en el perfil de usuario (LOCALAPPDATA) 
  para prevenir la navegación fuera del scope permitido.
"""

from __future__ import annotations
import os
import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Dict, List, Optional, Callable, Union, TypeAlias, NamedTuple

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
OSPath: TypeAlias = Union[str, Path]

class FileAttributes(NamedTuple):
    """Flags de Win32 para filtrar archivos del sistema mediante bitmask."""
    READONLY: int = 0x01
    HIDDEN: int = 0x02
    SYSTEM: int = 0x04
    REPARSE_POINT: int = 0x400

# Máscara usada para ignorar archivos de sistema, ocultos o puntos de unión (junctions).
SYSTEM_HIDDEN_FLAGS: int = (
    FileAttributes().HIDDEN | 
    FileAttributes().SYSTEM | 
    FileAttributes().REPARSE_POINT
)

def _is_junction_default(path: str) -> bool:
    """Retorna siempre False; utilizado como fallback si os.path.isjunction no existe."""
    return False

# Acceso seguro a la funcionalidad de junctions si existe en el runtime (Python 3.12+)
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

@dataclass
class BrowserCache:
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


def _get_kernel32() -> Optional[ctypes.WinDLL]:
    if os.name != 'nt':
        return None
    try:
        dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        return dll if hasattr(dll, 'GetFileAttributesW') else None
    except Exception:
        return None

def _is_unc_path(path_str: Optional[str]) -> bool:
    if not isinstance(path_str, str) or not path_str:
        return False
    return path_str.startswith(r"\\") or path_str.startswith("//")

def base_directories() -> List[Path]:
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env or _is_unc_path(local_env):
        return []
    
    try:
        p = Path(local_env)
        if p.exists() and p.is_dir():
            path_local = p.resolve(strict=True)
            if is_safe_to_modify(path_local) and not is_protected_path(path_local):
                return [path_local]
    except (OSError, RuntimeError, PermissionError):
        pass
    return []


def _is_path_inside_base(target_abs: str, base_abs: str) -> bool:
    if not isinstance(target_abs, str) or not isinstance(base_abs, str) or not target_abs or not base_abs:
        return False
    if len(target_abs) >= MAX_PATH_LEN or len(base_abs) >= MAX_PATH_LEN or '\0' in target_abs:
        return False
    try:
        common = os.path.commonpath([os.path.normpath(target_abs), os.path.normpath(base_abs)])
        return os.path.normpath(common) == os.path.normpath(base_abs)
    except (OSError, ValueError):
        return False


def _is_excluded_file(name: Optional[str]) -> bool:
    return name is not None and name.lower() in NEVER_TOUCH


def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    if kernel32 is None: return False
    try:
        attrs = kernel32.GetFileAttributesW(entry_path)
        return False if attrs == 0xFFFFFFFF else bool(attrs & SYSTEM_HIDDEN_FLAGS)
    except Exception:
        return False


def _should_skip_entry(
    entry: os.DirEntry, 
    kernel32: Optional[ctypes.WinDLL], 
    is_junction_fn: JunctionChecker
) -> bool:
    """
    Determina si una entrada del sistema de archivos debe ser ignorada.
    
    Args:
        entry: Objeto DirEntry de os.scandir.
        kernel32: Instancia de Win32 DLL para chequeos de atributos o None en no-Windows.
        is_junction_fn: Función para detectar si la ruta es un punto de unión (junction).
    """
    if entry.name is None or _is_excluded_file(entry.name):
        return True
    
    try:
        path = entry.path
        if len(path) >= MAX_PATH_LEN or _is_unc_path(path):
            return True
        if entry.is_symlink() or is_junction_fn(path) or _is_system_hidden(path, kernel32):
            return True
    except (OSError, AttributeError):
        return True
    return False


def _get_entry_size(entry: os.DirEntry) -> int:
    try:
        return int(entry.stat(follow_symlinks=False).st_size)
    except (OSError, PermissionError):
        return 0


def _sum_directory_recursive(
    root_abs: str, 
    is_junction_fn: JunctionChecker, 
    kernel32: Optional[ctypes.WinDLL],
    memo: Dict[str, int],
    depth: int = 0
) -> int:
    """
    Calcula recursivamente el tamaño de un directorio evitando ciclos y rutas inseguras.
    
    Args:
        root_abs: Ruta absoluta del directorio a escanear.
        is_junction_fn: Callback para detectar puntos de unión (evita escaneo infinito).
        kernel32: Objeto WinDLL opcional para verificar flags de sistema/ocultos.
        memo: Diccionario de caché para evitar re-procesar rutas ya calculadas.
        depth: Profundidad actual para cumplimiento de MAX_SCAN_DEPTH.
    """
    if not root_abs or depth > MAX_SCAN_DEPTH:
        return 0
    if root_abs in memo:
        return memo[root_abs]

    total_bytes: int = 0
    try:
        with os.scandir(root_abs) as it:
            for entry in it:
                if _should_skip_entry(entry, kernel32, is_junction_fn):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    total_bytes += _sum_directory_recursive(entry.path, is_junction_fn, kernel32, memo, depth + 1)
                else:
                    total_bytes += _get_entry_size(entry)
        
        memo[root_abs] = total_bytes
        return total_bytes
    except (OSError, PermissionError, RuntimeError):
        return 0


def directory_size(path: Optional[OSPath]) -> int:
    if not path: return 0
    try:
        p = Path(path)
        if not p.exists() or not p.is_absolute() or not is_safe_to_modify(p) or is_protected_path(p):
            return 0
        return _sum_directory_recursive(str(p.resolve(strict=True)), _IS_JUNCTION_FN, _get_kernel32(), {})
    except Exception:
        return 0


def _is_valid_cache_path(candidate: Path, base_abs_str: str, is_junction_fn: JunctionChecker) -> bool:
    if not isinstance(candidate, Path): return False
    try:
        if not candidate.exists() or not candidate.is_dir(): return False
        real_candidate = str(candidate.resolve(strict=True))
        if not _is_path_inside_base(real_candidate, base_abs_str) or not is_safe_to_modify(candidate) or is_protected_path(candidate):
            return False
        return not (candidate.is_symlink() or is_junction_fn(str(candidate)) or _is_excluded_file(candidate.name))
    except Exception:
        return False


def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    if not isinstance(real_base, Path) or not isinstance(rel_str, str):
        return Path()
    try:
        target = real_base.joinpath(*rel_str.split("\\"))
        if not target.exists():
            return Path()
        
        target_abs = str(target.resolve())
        if not target_abs.startswith(str(real_base)):
            return Path()
        if not is_safe_to_modify(target) or is_protected_path(target):
            return Path()
            
        return target if len(target_abs) < MAX_PATH_LEN else Path()
    except Exception:
        return Path()


def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    raw_bases = bases if bases is not None else base_directories()
    browser_map = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    k32 = _get_kernel32()
    global_memo: Dict[str, int] = {}
    found: List[BrowserCache] = []
    
    for base in raw_bases:
        if not isinstance(base, Path): continue
        try:
            real_base_path = base.resolve(strict=True)
            base_abs_str = str(real_base_path)
            for browser_name, rel_str in browser_map.items():
                candidate = _resolve_browser_path(real_base_path, rel_str)
                if not candidate or not _is_valid_cache_path(candidate, base_abs_str, _IS_JUNCTION_FN):
                    continue
                
                real_candidate = str(candidate.resolve(strict=True))
                size = _sum_directory_recursive(real_candidate, _IS_JUNCTION_FN, k32, global_memo)
                if size > 0:
                    found.append(BrowserCache(str(browser_name), Path(real_candidate), size))
        except Exception:
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    return sum(c.size_bytes for c in caches) if caches else 0


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
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
