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

PATH_FORBIDDEN_CHARS: Set[str] = {'*', '?', '<', '>', '|', '"'}

@dataclass
class ScanContext:
    """
    Estado inmutable compartido durante el recorrido del árbol de directorios.
    Contiene la base de confianza para evitar escapes de directorio y
    memoria de archivos visitados para evitar ciclos en enlaces simbólicos.
    """
    sandbox_base_norm: str
    kernel32: Optional[ctypes.WinDLL]
    is_junction: JunctionChecker
    visited_files: Set[tuple[int, int]]
    visited_dirs: VisitedDirs
    never_touch_lower: frozenset[str]
    max_depth: int = 20

class DirectoryScanner:
    """
    Motor de recorrido recursivo seguro. 
    Implementa validación de sandbox por ruta normalizada y detección
    de puntos de reparse para evitar fugas de contexto o bucles infinitos.
    """
    def __init__(self, context: ScanContext):
        self.ctx = context

    def get_size(self, root_path: str, depth: int = 0) -> ScanResult:
        """
        Calcula el tamaño acumulado de un directorio, ignorando elementos protegidos.
        El éxito total depende de que no ocurran excepciones críticas de acceso.
        """
        if depth > self.ctx.max_depth: 
            return ScanResult(0, True)
        
        root_norm = os.path.normcase(root_path)
        if not root_norm.startswith(self.ctx.sandbox_base_norm) or root_norm in self.ctx.visited_dirs:
            return ScanResult(0, True)

        self.ctx.visited_dirs.add(root_norm)
        total_bytes: int = 0
        
        try:
            with os.scandir(root_path) as iterator:
                for entry in iterator:
                    try:
                        entry_name_lower = entry.name.lower()
                        if entry_name_lower in self.ctx.never_touch_lower: continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            if not self.ctx.is_junction(entry.path):
                                subdir = self.get_size(entry.path, depth + 1)
                                total_bytes += subdir.bytes_found
                        elif entry.is_file(follow_symlinks=False):
                            if not _is_system_hidden(entry.path, self.ctx.kernel32):
                                # Verificación de acceso antes de stat para evitar bloqueos
                                try:
                                    fd = os.open(entry.path, os.O_RDONLY)
                                    os.close(fd)
                                except (OSError, PermissionError):
                                    continue
                                    
                                st = entry.stat(follow_symlinks=False)
                                file_id = (st.st_dev, st.st_ino)
                                if file_id not in self.ctx.visited_files:
                                    self.ctx.visited_files.add(file_id)
                                    total_bytes += st.st_size
                    except (OSError, PermissionError):
                        continue
            return ScanResult(total_bytes, True)
        except (OSError, PermissionError): 
            return ScanResult(0, False)

def safe_path_operation(default: Any) -> Callable[[Callable], Callable]:
    """Decorador para envolver operaciones de disco en bloques try-except."""
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
    """Resultado del cálculo de tamaño recursivo."""
    bytes_found: int
    success: bool

class FileAttributes:
    """Constantes de flags para la API Win32 GetFileAttributes."""
    READONLY: int = 0x01
    HIDDEN: int = 0x02
    SYSTEM: int = 0x04
    REPARSE_POINT: int = 0x400

SYSTEM_HIDDEN_FLAGS: int = (
    FileAttributes.HIDDEN | 
    FileAttributes.SYSTEM | 
    FileAttributes.REPARSE_POINT
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

def _is_valid_rel_path(path_candidate: Any) -> TypeGuard[str]:
    """Valida que una ruta relativa no contenga caracteres peligrosos o escape de sandbox."""
    if not isinstance(path_candidate, str) or not path_candidate.strip(): return False
    if os.path.isabs(path_candidate) or path_candidate.startswith(('\\', '/')): return False
    if ".." in os.path.normpath(path_candidate): return False
    if any(c in path_candidate for c in PATH_FORBIDDEN_CHARS): return False
    if any(ord(c) < 32 for c in path_candidate): return False
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
    """Identifica rutas de red UNC que deben evitarse por seguridad y latencia."""
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def base_directories() -> List[Path]:
    """Retorna las rutas base permitidas para escaneo basadas en el perfil de usuario."""
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env.strip() or _is_unc_path(local_env):
        return []
    
    try:
        p = Path(local_env).resolve()
        if p.exists() and p.is_dir() and os.access(p, os.R_OK):
            if is_safe_to_modify(p) and not is_protected_path(p):
                return [p]
    except (OSError, RuntimeError):
        pass
    return []

@safe_path_operation(False)
def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Verifica si un archivo posee atributos de sistema o hidden mediante WinAPI."""
    if kernel32 is None: return False
    attrs = kernel32.GetFileAttributesW(entry_path)
    return attrs != 0xFFFFFFFF and bool(attrs & SYSTEM_HIDDEN_FLAGS)

@safe_path_operation(0)
def directory_size(path: Optional[OSPath]) -> int:
    """Calcula el peso en bytes de un directorio, aplicando filtros de seguridad."""
    if not isinstance(path, (str, Path)) or not path: return 0
    path_obj = Path(path)
    if not path_obj.exists() or not path_obj.is_dir(): return 0
    if not is_safe_to_modify(path_obj) or is_protected_path(path_obj): return 0
    
    res_str = str(path_obj.resolve())
    ctx = ScanContext(os.path.normcase(res_str), _get_kernel32(), _IS_JUNCTION_FN, set(), set(), NEVER_TOUCH)
    return DirectoryScanner(ctx).get_size(res_str).bytes_found

@safe_path_operation(False)
def _is_valid_cache_path(candidate: Path, base_norm: str, kernel: Optional[ctypes.WinDLL]) -> bool:
    """Valida exhaustivamente una ruta de caché antes de intentar cualquier medición."""
    if not candidate.exists() or not candidate.is_dir(): return False
    cand_abs = os.path.abspath(candidate)
    cand_norm = os.path.normcase(cand_abs)
    
    if not is_safe_to_modify(candidate) or not cand_norm.startswith(base_norm): return False
    if _is_system_hidden(cand_abs, kernel): return False
    
    return not (candidate.is_symlink() or _IS_JUNCTION_FN(cand_abs) or is_protected_path(candidate) or candidate.name.lower() in NEVER_TOUCH)

def _resolve_browser_path(base_resolved: Path, rel_str: str) -> Path:
    """
    Construye y valida una ruta absoluta de caché.
    Asegura que la ruta resuelta permanezca dentro del directorio base
    (evitando ataques de path traversal) y que no sea un enlace simbólico
    o punto de reparse que apunte fuera del sandbox.
    """
    if not _is_valid_rel_path(rel_str): return Path()
    try:
        target = base_resolved.joinpath(*rel_str.split("\\"))
        if not target.exists() or not target.is_dir(): return Path()
        
        target_abs = os.path.abspath(target)
        if not os.path.normcase(target_abs).startswith(os.path.normcase(str(base_resolved))): return Path()
        if _IS_JUNCTION_FN(target_abs) or target.is_symlink(): return Path()
        
        return target if is_safe_to_modify(target) else Path()
    except (OSError, RuntimeError):
        return Path()

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """
    Realiza el escaneo de perfiles de navegador en las bases configuradas.
    Retorna una lista ordenada de objetos BrowserCache con el tamaño hallado.
    """
    raw_bases = list(bases) if (bases is not None and isinstance(bases, (list, tuple))) else base_directories()
    browser_map = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    
    if not raw_bases or not isinstance(browser_map, dict): return []
        
    found: List[BrowserCache] = []
    kernel = _get_kernel32()
    
    for base in raw_bases:
        if not isinstance(base, Path): continue
        try:
            base_resolved = base.resolve()
            if not base_resolved.is_dir(): continue
            base_norm = os.path.normcase(str(base_resolved))
        except (OSError, RuntimeError): continue
        
        scanner = DirectoryScanner(ScanContext(base_norm, kernel, _IS_JUNCTION_FN, set(), set(), NEVER_TOUCH))
        
        for browser_key, relative_path in browser_map.items():
            if not isinstance(relative_path, str): continue
            candidate = _resolve_browser_path(base_resolved, relative_path)
            if candidate != Path() and _is_valid_cache_path(candidate, base_norm, kernel):
                scan_res = scanner.get_size(str(candidate))
                if scan_res.success and scan_res.bytes_found > 0:
                    found.append(BrowserCache(browser_key, candidate, scan_res.bytes_found))
    
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Calcula el total en bytes de una colección de cachés."""
    return sum(c.size_bytes for c in caches if isinstance(c, BrowserCache)) if caches else 0

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera un reporte legible en formato de lista para la UI."""
    current_caches = caches if isinstance(caches, list) else detect_profiles()
    if not current_caches: return ["No se detectaron cachés de navegador en este sistema."]
    
    total_bytes = total_cache_bytes(current_caches)
    total_mb = round(total_bytes / BYTES_TO_MB, 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
