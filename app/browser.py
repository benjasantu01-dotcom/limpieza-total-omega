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

JunctionChecker: TypeAlias = Callable[[str], bool]
BrowserMap: TypeAlias = Dict[str, str]
OSPath: TypeAlias = Union[str, Path]
VisitedDirs: TypeAlias = Set[tuple[int, int]]

PATH_FORBIDDEN_CHARS: Set[str] = {'*', '?', '<', '>', '|', '"'}

class FileAttributes:
    """Constantes de atributos de archivos Win32 para validación de seguridad."""
    READONLY: int = 0x01
    HIDDEN: int = 0x02
    SYSTEM: int = 0x04
    REPARSE_POINT: int = 0x400

SYSTEM_HIDDEN_FLAGS: int = (
    FileAttributes.HIDDEN | 
    FileAttributes.SYSTEM | 
    FileAttributes.REPARSE_POINT
)

@dataclass
class ScanContext:
    """
    Estado inmutable compartido durante la recursión del escaneo de disco.
    Define las restricciones de navegación para el DirectoryScanner.
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
    Motor de recorrido recursivo para el escaneo de directorios de caché.
    Implementa validaciones de seguridad para evitar escapes del sandbox y
    prevenir la entrada en puntos de reparse (junctions).
    """
    def __init__(self, context: ScanContext) -> None:
        self.ctx = context

    def _is_restricted_name(self, name: str) -> bool:
        """Verifica nombres de archivo reservados o bloqueados."""
        return any(ord(c) < 32 for c in name) or name.lower() in self.ctx.never_touch_lower

    def _should_stop_traversal(self, entry: os.DirEntry, depth: int) -> bool:
        """Determina si una entrada debe ignorarse por límites de seguridad o profundidad."""
        if not entry.name or self._is_restricted_name(entry.name):
            return True
        if depth > self.ctx.max_depth:
            return True
        return False

    def _is_path_allowed(self, path_str: str) -> bool:
        """
        Valida que la ruta sea segura según safety.py y que su ubicación
        real esté contenida dentro de la base autorizada (sandbox).
        """
        if not path_str or _is_unc_path(path_str): 
            return False
        
        try:
            path_obj = Path(path_str)
            if not path_obj.exists() or not is_safe_to_modify(path_obj) or is_protected_path(path_obj):
                return False
            
            real_path = os.path.realpath(path_str)
            return os.path.commonpath([real_path, self.ctx.sandbox_base_norm]) == self.ctx.sandbox_base_norm
        except (OSError, RuntimeError, ValueError):
            return False

    def _handle_directory(self, entry: os.DirEntry, depth: int) -> int:
        """Gestiona la recursión en directorios válidos."""
        if not self.ctx.is_junction(entry.path) and self._is_path_allowed(entry.path):
            return self.get_size(entry.path, depth + 1).bytes_found
        return 0

    def _handle_file(self, entry: os.DirEntry) -> int:
        """Valida y calcula el tamaño de archivos individuales."""
        if not _is_system_hidden(entry.path, self.ctx.kernel32):
            try:
                st_file = entry.stat(follow_symlinks=False)
                file_id = (st_file.st_dev, st_file.st_ino)
                if file_id not in self.ctx.visited_files:
                    self.ctx.visited_files.add(file_id)
                    return st_file.st_size
            except (OSError, PermissionError):
                pass
        return 0

    def _process_entry(self, entry: os.DirEntry, depth: int, total_bytes: int) -> int:
        """Distribuye el procesamiento según el tipo de entrada."""
        if self._should_stop_traversal(entry, depth):
            return total_bytes
            
        try:
            if entry.is_dir(follow_symlinks=False):
                return total_bytes + self._handle_directory(entry, depth)
            elif entry.is_file(follow_symlinks=False):
                return total_bytes + self._handle_file(entry)
        except (OSError, PermissionError):
            pass
        return total_bytes

    def get_size(self, root_path: str, depth: int = 0) -> ScanResult:
        """Calcula el tamaño de un directorio evitando ciclos y reparse points."""
        if not isinstance(root_path, str) or not root_path or not self._is_path_allowed(root_path):
            return ScanResult(0, False)
            
        if depth > self.ctx.max_depth:
            return ScanResult(0, True)

        try:
            st = os.lstat(root_path)
            # Extracción de atributos de reparse si está disponible
            attrs = getattr(st, 'st_file_attributes', 0)
            if self.ctx.is_junction(root_path) or (attrs & FileAttributes.REPARSE_POINT):
                return ScanResult(0, True)
            dir_id = (st.st_dev, st.st_ino)
        except (OSError, PermissionError, FileNotFoundError):
            return ScanResult(0, False)

        if dir_id in self.ctx.visited_dirs:
            return ScanResult(0, True)

        self.ctx.visited_dirs.add(dir_id)
        total_bytes: int = 0
        
        try:
            with os.scandir(root_path) as iterator:
                for entry in iterator:
                    total_bytes = self._process_entry(entry, depth, total_bytes)
            return ScanResult(total_bytes, True)
        except (OSError, PermissionError): 
            return ScanResult(0, False)

def safe_path_operation(default: Any) -> Callable[[Callable], Callable]:
    """Decorador para capturar errores de sistema en operaciones de lectura."""
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
    """Resultado estructurado de la operación de escaneo de tamaño."""
    bytes_found: int
    success: bool

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
    """Valida la integridad de las rutas relativas predefinidas en BROWSER_CACHE_PATHS."""
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
    """Entidad que representa un hallazgo de caché de navegador."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / BYTES_TO_MB, 2)

@safe_path_operation(None)
def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """Acceso seguro a funciones del API de Win32 (kernel32)."""
    if os.name != 'nt': return None
    dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    return dll if hasattr(dll, 'GetFileAttributesW') else None

def _is_unc_path(path_str: Optional[str]) -> TypeGuard[str]:
    """Verifica si una ruta es UNC (Universal Naming Convention)."""
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def base_directories() -> List[Path]:
    """Detecta las rutas base de perfiles del sistema bajo el usuario actual."""
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env.strip() or _is_unc_path(local_env):
        return []
    
    try:
        p = Path(local_env)
        if p.exists() and p.is_dir() and os.access(p, os.R_OK):
            if is_safe_to_modify(p) and not is_protected_path(p):
                return [p]
    except (OSError, RuntimeError):
        pass
    return []

@safe_path_operation(False)
def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Utiliza atributos de Win32 para detectar archivos marcados como ocultos o sistema."""
    if kernel32 is None: return False
    attrs = kernel32.GetFileAttributesW(entry_path)
    return attrs != 0xFFFFFFFF and bool(attrs & SYSTEM_HIDDEN_FLAGS)

@safe_path_operation(0)
def directory_size(path: Optional[OSPath]) -> int:
    """Calcula el tamaño de un directorio de forma aislada y segura."""
    if path is None: return 0
    p = Path(path)
    if not p.exists() or not p.is_dir() or not os.access(p, os.R_OK): return 0
    
    try:
        abs_p = p.resolve(strict=True)
        root_norm = str(abs_p)
    except (OSError, RuntimeError):
        return 0

    ctx = ScanContext(root_norm, _get_kernel32(), _IS_JUNCTION_FN, set(), set(), NEVER_TOUCH, MAX_SCAN_DEPTH)
    return DirectoryScanner(ctx).get_size(root_norm).bytes_found

def _is_target_valid(target: Optional[Path], base_norm: str, kernel: Optional[ctypes.WinDLL]) -> bool:
    """Verifica si un directorio de caché objetivo es apto para el análisis."""
    if not isinstance(target, Path) or not target.exists() or not target.is_dir(): 
        return False
    try:
        if not os.access(target, os.R_OK) or not is_safe_to_modify(target) or is_protected_path(target): 
            return False
        
        if _IS_JUNCTION_FN(str(target)) or target.is_symlink() or os.path.islink(target):
            return False
            
        if _is_system_hidden(str(target), kernel): return False
        
        parts = [p.lower() for p in target.parts]
        if any(part in NEVER_TOUCH for part in parts):
            return False
        
        if any(c in target.name for c in PATH_FORBIDDEN_CHARS) or any(ord(c) < 32 for c in target.name):
            return False
        
        target_norm = os.path.realpath(str(target))
        return os.path.commonpath([target_norm, base_norm]) == base_norm
    except (OSError, PermissionError, RuntimeError, ValueError):
        return False

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """Escanea el sistema en busca de cachés de navegadores conocidos."""
    raw_bases = list(bases) if isinstance(bases, (list, tuple)) else base_directories()
    browser_map = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    
    if not raw_bases or not isinstance(browser_map, dict): return []
        
    found: List[BrowserCache] = []
    kernel = _get_kernel32()
    
    for base in raw_bases:
        if not isinstance(base, Path): continue
        
        try:
            if not base.exists(): continue
            base_norm = os.path.realpath(str(base))
            
            ctx = ScanContext(base_norm, kernel, _IS_JUNCTION_FN, set(), set(), NEVER_TOUCH, MAX_SCAN_DEPTH)
            scanner = DirectoryScanner(ctx)
            
            for browser_key, relative_path in browser_map.items():
                if not _is_valid_rel_path(relative_path): continue
                
                target = base.joinpath(*relative_path.split("\\"))
                if not _is_target_valid(target, base_norm, kernel): continue
                
                scan_res = scanner.get_size(str(target))
                if scan_res.success and scan_res.bytes_found > 0:
                    found.append(BrowserCache(str(browser_key), target, scan_res.bytes_found))
        except (OSError, RuntimeError, PermissionError):
            continue
    
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Suma total de bytes recuperables de una lista de hallazgos de caché."""
    return sum(c.size_bytes for c in caches if isinstance(c, BrowserCache)) if caches else 0

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera una descripción legible para el usuario de los hallazgos de caché."""
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
