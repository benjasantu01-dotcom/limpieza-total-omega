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
from typing import (
    Iterable, Sequence, Dict, List, Optional, Callable, 
    Union, TypeAlias, NamedTuple, Set, TypeGuard, Any, functools
)

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

# Aliases para mejorar la legibilidad de las firmas de funciones
JunctionChecker: TypeAlias = Callable[[str], bool]
BrowserMap: TypeAlias = Dict[str, str]
OSPath: TypeAlias = Union[str, Path]
# Mapeo de rutas normalizadas (normcase) a su tamaño calculado
VisitedDirs: TypeAlias = Dict[str, int]

# Caracteres que no deben formar parte de una ruta procesada por seguridad
PATH_FORBIDDEN_CHARS: Set[str] = {'*', '?', '<', '>', '|', '"'}

@dataclass(frozen=True)
class ScanContext:
    """
    Estado compartido durante el escaneo recursivo de directorios.
    
    Attributes:
        base_norm: Ruta base normalizada para verificar confinamiento (sandbox).
        kernel32: Instancia opcional de la API de Windows para consultas de sistema.
        is_junction: Callback para detectar puntos de reparse (Junctions).
        visited_files: Conjunto de tuplas (dev, ino) para evitar conteo duplicado.
        visited_dirs: Diccionario de caché para rutas visitadas.
    """
    base_norm: str
    kernel32: Optional[ctypes.WinDLL]
    is_junction: JunctionChecker
    visited_files: Set[tuple[int, int]]
    visited_dirs: VisitedDirs

def safe_path_operation(default: Any) -> Callable:
    """
    Decorador para envolver operaciones de E/S propensas a errores de SO.
    
    Args:
        default: Valor de retorno si ocurre una excepción de sistema.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except (OSError, PermissionError, RuntimeError, AttributeError, ValueError, TypeError):
                return default
        return wrapper
    return decorator

class ScanResult(NamedTuple):
    """Resultado inmutable de un escaneo de directorio."""
    bytes_found: int
    success: bool

class FileAttributes(NamedTuple):
    """Máscaras de bits de la Win32 API para identificación de tipos de archivo."""
    READONLY: int = 0x01
    HIDDEN: int = 0x02
    SYSTEM: int = 0x04
    REPARSE_POINT: int = 0x400

SYSTEM_HIDDEN_FLAGS: int = (
    FileAttributes().HIDDEN | 
    FileAttributes().SYSTEM | 
    FileAttributes().REPARSE_POINT
)

def _is_junction_default(path: str) -> bool:
    return False

_IS_JUNCTION_FN: JunctionChecker = getattr(os.path, 'isjunction', _is_junction_default)

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
    "Nunca se tocan contraseñas, cookies, marcadores ni historial."
)

MAX_SCAN_DEPTH: int = 15
MAX_PATH_LEN: int = 260
BYTES_TO_MB: float = 1024 * 1024

@dataclass
class BrowserCache:
    """Modelo de datos para una caché detectada."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / BYTES_TO_MB, 2)

@safe_path_operation(None)
def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """Obtiene acceso a la DLL de sistema solo si el host es Windows."""
    if os.name != 'nt':
        return None
    dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    return dll if hasattr(dll, 'GetFileAttributesW') else None

def _is_unc_path(path_str: Optional[str]) -> TypeGuard[str]:
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def _ensure_within_base(target_norm: str, base_norm: str) -> bool:
    return target_norm.startswith(base_norm)

def base_directories() -> List[Path]:
    """Obtiene la raíz de configuración del usuario actual, validada contra seguridad."""
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env or _is_unc_path(local_env):
        return []
    
    p = Path(local_env)
    if p.exists() and p.is_dir():
        try:
            path_local: Path = p.resolve(strict=True)
            if is_safe_to_modify(path_local) and not is_protected_path(path_local):
                return [path_local]
        except (OSError, RuntimeError):
            return []
    return []

def _is_excluded_file(name: Optional[str]) -> TypeGuard[str]:
    return name is not None and name.lower() in NEVER_TOUCH

@safe_path_operation(False)
def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Valida atributos de sistema de archivos vía Win32 API."""
    if kernel32 is None: return False
    attrs: int = kernel32.GetFileAttributesW(entry_path)
    return attrs != 0xFFFFFFFF and bool(attrs & SYSTEM_HIDDEN_FLAGS)

def _should_skip_entry(entry: os.DirEntry, ctx: ScanContext) -> bool:
    """Filtra entradas según políticas de seguridad y aislamiento."""
    if _is_excluded_file(entry.name):
        return True
    
    path_str = entry.path
    if _is_unc_path(path_str) or len(path_str) >= MAX_PATH_LEN or any(c in path_str for c in PATH_FORBIDDEN_CHARS):
        return True
    
    if not _ensure_within_base(os.path.normcase(path_str), ctx.base_norm):
        return True
    
    if is_protected_path(Path(path_str)) or not is_safe_to_modify(Path(path_str)):
        return True

    if entry.is_symlink() or ctx.is_junction(path_str) or _is_system_hidden(path_str, ctx.kernel32):
        return True
        
    return False

def _process_file_node(entry: os.DirEntry, visited_files: Set[tuple[int, int]]) -> int:
    try:
        if not is_safe_to_modify(Path(entry.path)) or is_protected_path(Path(entry.path)):
            return 0
        st = entry.stat(follow_symlinks=False)
        if not hasattr(st, 'st_dev') or not hasattr(st, 'st_ino'):
            return 0
        file_id = (st.st_dev, st.st_ino)
        if file_id in visited_files:
            return 0
        visited_files.add(file_id)
        return st.st_size
    except (OSError, PermissionError):
        return 0

def _sum_directory_recursive(root_path: str, ctx: ScanContext, depth: int = 0) -> ScanResult:
    """Realiza un recorrido DFS limitado con detección de ciclos y nodos visitados."""
    if depth > MAX_SCAN_DEPTH:
        return ScanResult(0, True)
    
    if not is_safe_to_modify(Path(root_path)) or is_protected_path(Path(root_path)):
        return ScanResult(0, True)
    
    path_norm = os.path.normcase(root_path)
    if path_norm in ctx.visited_dirs:
        return ScanResult(ctx.visited_dirs[path_norm], True)

    total_bytes: int = 0
    try:
        with os.scandir(root_path) as it:
            for entry in it:
                try:
                    if _should_skip_entry(entry, ctx):
                        continue
                        
                    if entry.is_dir(follow_symlinks=False):
                        res = _sum_directory_recursive(entry.path, ctx, depth + 1)
                        total_bytes += res.bytes_found
                    else:
                        total_bytes += _process_file_node(entry, ctx.visited_files)
                except (OSError, PermissionError):
                    continue
        
        ctx.visited_dirs[path_norm] = total_bytes
        return ScanResult(total_bytes, True)
    except (OSError, PermissionError):
        return ScanResult(0, False)

@safe_path_operation(0)
def directory_size(path: Optional[OSPath]) -> int:
    """Calcula el tamaño total de un directorio, respetando restricciones de seguridad."""
    if not isinstance(path, (str, Path)): return 0
    path_obj: Path = Path(path)
    if not path_obj.exists() or not path_obj.is_dir(): return 0
    
    resolved_p: Path = path_obj.resolve(strict=True)
    if not is_safe_to_modify(resolved_p) or is_protected_path(resolved_p):
        return 0
    
    path_str = str(resolved_p)
    ctx = ScanContext(os.path.normcase(path_str), _get_kernel32(), _IS_JUNCTION_FN, set(), {})
    return _sum_directory_recursive(path_str, ctx, 0).bytes_found

@safe_path_operation(False)
def _is_valid_cache_path(candidate: Path, base_abs_str: str) -> bool:
    """Verifica si la ruta candidata pertenece al perfil de usuario y es segura."""
    if not isinstance(candidate, Path) or not candidate.is_dir(): 
        return False
    try:
        real: Path = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
        
    if not _ensure_within_base(os.path.normcase(str(real)), os.path.normcase(base_abs_str)):
        return False
    if not is_safe_to_modify(real) or is_protected_path(real):
        return False
    return not (real.is_symlink() or _IS_JUNCTION_FN(str(real)) or _is_excluded_file(real.name))

@safe_path_operation(Path())
def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    """Construye una ruta absoluta validando que no escape del perfil base."""
    if not isinstance(real_base, Path) or not isinstance(rel_str, str) or not rel_str: 
        return Path()
    
    if any(char in rel_str for char in PATH_FORBIDDEN_CHARS): 
        return Path()
    
    parts = [p for p in rel_str.split("\\") if p and p != "." and p != ".."]
    if not parts:
        return Path()
        
    target: Path = real_base.joinpath(*parts)
    if not target.is_dir(): 
        return Path()
    
    try:
        target_res = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return Path()
        
    if _ensure_within_base(os.path.normcase(str(target_res)), os.path.normcase(str(real_base))) and \
       is_safe_to_modify(target_res) and not is_protected_path(target_res):
        return target_res
    return Path()

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """Descubre perfiles de navegador y mide el uso de disco de sus cachés."""
    raw_bases = list(bases) if bases is not None else base_directories()
    if not raw_bases: return []

    browser_map = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    found: List[BrowserCache] = []
    
    for base in raw_bases:
        if not isinstance(base, Path) or not base.exists(): continue
        try:
            real_base: Path = base.resolve(strict=True)
            real_base_str: str = str(real_base)
            ctx = ScanContext(os.path.normcase(real_base_str), _get_kernel32(), _IS_JUNCTION_FN, set(), {})
            
            for browser_name, rel_str in browser_map.items():
                candidate = _resolve_browser_path(real_base, rel_str)
                if candidate != Path() and _is_valid_cache_path(candidate, real_base_str):
                    scan_res = _sum_directory_recursive(str(candidate), ctx, 0)
                    if scan_res.bytes_found > 0:
                        found.append(BrowserCache(str(browser_name), candidate, scan_res.bytes_found))
        except (OSError, RuntimeError):
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    return sum(c.size_bytes for c in caches) if caches else 0

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera un reporte legible sobre el uso de disco de las cachés."""
    current_caches = caches if isinstance(caches, list) else detect_profiles()
    if not current_caches:
        return ["No se detectaron cachés de navegador en este sistema."]
        
    total_mb: float = round(total_cache_bytes(current_caches) / BYTES_TO_MB, 2)
    lines: List[str] = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
