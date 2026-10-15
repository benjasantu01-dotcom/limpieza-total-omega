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
    Union, TypeAlias, NamedTuple, Set, TypeGuard, Any
)
import functools

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
VisitedDirs: TypeAlias = Set[str]
SafetyCache: TypeAlias = Dict[str, bool]

PATH_FORBIDDEN_CHARS: Set[str] = {'*', '?', '<', '>', '|', '"'}

@dataclass
class ScanContext:
    """
    Estado mutable compartido durante un proceso de escaneo. 
    Contiene caches para evitar validaciones redundantes de seguridad 
    y registros de nodos visitados para prevenir bucles.
    """
    sandbox_base_norm: str
    kernel32: Optional[ctypes.WinDLL]
    is_junction: JunctionChecker
    visited_files: Set[tuple[int, int]]
    visited_dirs: VisitedDirs
    safety_cache: SafetyCache

def safe_path_operation(default: Any) -> Callable:
    """
    Decorador que envuelve funciones de E/S capturando errores de acceso al sistema de archivos.
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
    bytes_found: int
    success: bool

class FileAttributes(NamedTuple):
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

def _is_valid_rel_path(p: str) -> bool:
    """Valida que una ruta relativa no contenga secuencias de escape o caracteres prohibidos."""
    if not p or not isinstance(p, str): return False
    if any(ord(c) < 32 for c in p): return False
    if os.path.isabs(p) or p.startswith(('\\', '/')): return False
    if '..' in p or any(c in p for c in PATH_FORBIDDEN_CHARS): return False
    return True

for _k, _v in BROWSER_CACHE_PATHS.items():
    if not _is_valid_rel_path(_v):
        raise ValueError(f"Ruta de configuración insegura detectada: {_v}")

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
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / BYTES_TO_MB, 2)

@safe_path_operation(None)
def _get_kernel32() -> Optional[ctypes.WinDLL]:
    if os.name != 'nt': return None
    dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    return dll if hasattr(dll, 'GetFileAttributesW') else None

def _is_unc_path(path_str: Optional[str]) -> TypeGuard[str]:
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def _is_within_sandbox(target: Path, base: Path) -> bool:
    try:
        t_res = target.resolve()
        b_res = base.resolve()
        return b_res in t_res.parents or t_res == b_res
    except (OSError, RuntimeError):
        return False

def base_directories() -> List[Path]:
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env or _is_unc_path(local_env):
        return []
    p = Path(local_env)
    if p.exists() and p.is_dir():
        try:
            path_local = p.resolve(strict=True)
            if is_safe_to_modify(path_local) and not is_protected_path(path_local):
                return [path_local]
        except (OSError, RuntimeError):
            return []
    return []

def _is_excluded_file(name: Optional[str]) -> TypeGuard[str]:
    return isinstance(name, str) and name.lower() in NEVER_TOUCH

@safe_path_operation(False)
def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    if kernel32 is None: return False
    attrs = kernel32.GetFileAttributesW(entry_path)
    return attrs != 0xFFFFFFFF and bool(attrs & SYSTEM_HIDDEN_FLAGS)

def _is_path_safe(path_str: str, ctx: ScanContext) -> bool:
    if path_str not in ctx.safety_cache:
        p = Path(path_str)
        ctx.safety_cache[path_str] = p.exists() and is_safe_to_modify(p) and not is_protected_path(p)
    return ctx.safety_cache[path_str]

def _should_skip_entry(entry: os.DirEntry, ctx: ScanContext) -> bool:
    if _is_excluded_file(entry.name): return True
    path_str = entry.path
    if len(path_str) >= MAX_PATH_LEN or any(c in path_str for c in PATH_FORBIDDEN_CHARS): return True
    if not _is_path_safe(path_str, ctx): return True
    if entry.is_symlink() or ctx.is_junction(path_str) or _is_system_hidden(path_str, ctx.kernel32): return True
    return False

def _process_file_node(entry: os.DirEntry, ctx: ScanContext) -> int:
    """Extrae el tamaño de un archivo individual evitando procesar duplicados por inodo."""
    try:
        st = entry.stat(follow_symlinks=False)
        file_id = (st.st_dev, st.st_ino)
        if file_id in ctx.visited_files: return 0
        ctx.visited_files.add(file_id)
        return int(st.st_size)
    except (OSError, PermissionError): return 0

def _sum_directory_recursive(root_path: str, ctx: ScanContext, depth: int = 0) -> ScanResult:
    """
    Recorre el sistema de archivos de forma recursiva aplicando las reglas de seguridad
    de 'ctx'. Retorna el acumulado de bytes encontrados y éxito de operación.
    """
    if depth > MAX_SCAN_DEPTH or len(root_path) >= MAX_PATH_LEN: return ScanResult(0, True)
    
    # Validación defensiva de sandbox por nodo
    if not _is_within_sandbox(Path(root_path), Path(ctx.sandbox_base_norm)):
        return ScanResult(0, False)

    try:
        root_stat = os.stat(root_path)
        node_id = (root_stat.st_dev, root_stat.st_ino)
        if node_id in ctx.visited_dirs: return ScanResult(0, True)
        ctx.visited_dirs.add(node_id)
    except (OSError, PermissionError): return ScanResult(0, False)

    total_bytes = 0
    try:
        with os.scandir(root_path) as it:
            for entry in it:
                if _should_skip_entry(entry, ctx): continue
                if entry.is_dir(follow_symlinks=False):
                    res = _sum_directory_recursive(entry.path, ctx, depth + 1)
                    total_bytes += res.bytes_found
                else:
                    total_bytes += _process_file_node(entry, ctx)
        return ScanResult(total_bytes, True)
    except (OSError, PermissionError): return ScanResult(total_bytes, False)

@safe_path_operation(0)
def directory_size(path: Optional[OSPath]) -> int:
    if not isinstance(path, (str, Path)) or not path: return 0
    path_obj = Path(path).resolve(strict=True)
    ctx = ScanContext(str(path_obj), _get_kernel32(), _IS_JUNCTION_FN, set(), set(), {})
    return _sum_directory_recursive(str(path_obj), ctx, 0).bytes_found

@safe_path_operation(False)
def _is_valid_cache_path(candidate: Path, base_abs_str: str) -> bool:
    if not candidate.exists() or not candidate.is_dir() or not os.access(candidate, os.R_OK): return False
    real = candidate.resolve(strict=True)
    if not _is_within_sandbox(real, Path(base_abs_str)): return False
    if not is_safe_to_modify(real) or is_protected_path(real): return False
    return not (real.is_symlink() or _IS_JUNCTION_FN(str(real)) or _is_excluded_file(real.name))

@safe_path_operation(Path())
def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    if not _is_valid_rel_path(rel_str): return Path()
    target = real_base.joinpath(*rel_str.split("\\")).resolve(strict=True)
    if target.exists() and target.is_dir() and _is_within_sandbox(target, real_base):
        return target
    return Path()

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    raw_bases = list(bases) if bases is not None else base_directories()
    if not raw_bases: return []
    
    browser_map = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    found: List[BrowserCache] = []
    ctx = ScanContext("", _get_kernel32(), _IS_JUNCTION_FN, set(), set(), {})
    
    for base in raw_bases:
        try:
            real_base = base.resolve(strict=True)
            ctx.sandbox_base_norm = str(real_base)
            for browser_name, rel_str in browser_map.items():
                candidate = _resolve_browser_path(real_base, rel_str)
                if candidate != Path() and _is_valid_cache_path(candidate, ctx.sandbox_base_norm):
                    ctx.visited_files.clear()
                    # Reseteamos visited_dirs solo entre navegadores para evitar ciclos cross-browser
                    # pero mantenemos seguridad cacheada globalmente para ganar performance
                    scan_res = _sum_directory_recursive(str(candidate), ctx, 0)
                    if scan_res.bytes_found > 0:
                        found.append(BrowserCache(str(browser_name), candidate, scan_res.bytes_found))
        except (OSError, RuntimeError): continue
    
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    return sum(c.size_bytes for c in caches) if caches else 0

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    current_caches = caches if isinstance(caches, list) else detect_profiles()
    if not current_caches: return ["No se detectaron cachés de navegador en este sistema."]
    total_mb = round(total_cache_bytes(current_caches) / BYTES_TO_MB, 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
