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
JunctionChecker: TypeAlias = Callable[[str], bool]  # Valida si una ruta es un punto de reparse
BrowserMap: TypeAlias = Dict[str, str]              # Mapeo de nombre de navegador a subruta
OSPath: TypeAlias = Union[str, Path]                # Acepta tanto strings como objetos Path
VisitedDirs: TypeAlias = Set[str]                   # Tracking para evitar bucles recursivos
SafetyCache: TypeAlias = Dict[str, bool]            # Memoización de validaciones de seguridad

PATH_FORBIDDEN_CHARS: Set[str] = {'*', '?', '<', '>', '|', '"'}

@dataclass
class ScanContext:
    """
    Estado persistente durante una operación de escaneo.
    
    Attributes:
        sandbox_base_norm: Ruta base normalizada del perfil de usuario (sandbox).
        kernel32: Handle a la API de Windows para inspección de atributos.
        is_junction: Función delegada para detectar puntos de reparse.
        visited_files: Conjunto de (dev, inode) para evitar conteo doble de hardlinks.
        visited_dirs: Conjunto de rutas visitadas para prevenir ciclos de directorios.
        safety_cache: Diccionario para evitar re-validaciones de seguridad en el mismo árbol.
    """
    sandbox_base_norm: str
    kernel32: Optional[ctypes.WinDLL]
    is_junction: JunctionChecker
    visited_files: Set[tuple[int, int]]
    visited_dirs: VisitedDirs
    safety_cache: SafetyCache

def safe_path_operation(default: Any) -> Callable:
    """
    Decorador protector: envuelve operaciones de sistema que podrían lanzar 
    excepciones por permisos o bloqueos, retornando un valor seguro.
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
    """Representa el total de bytes encontrados y si la operación fue exitosa."""
    bytes_found: int
    success: bool

class FileAttributes(NamedTuple):
    """Constantes de máscara de bits de la API de Windows para tipos de archivos especiales."""
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
    """
    Valida que una ruta relativa no intente escapar del sandbox mediante '..' 
    o caracteres prohibidos por el sistema de archivos.
    """
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
    """Modelo que representa un hallazgo de caché, conteniendo su ubicación y peso."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el peso del caché convertido a MB."""
        return round(self.size_bytes / BYTES_TO_MB, 2)

@safe_path_operation(None)
def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """Obtiene una referencia al handle de kernel32.dll para validar atributos de archivo."""
    if os.name != 'nt': return None
    dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    return dll if hasattr(dll, 'GetFileAttributesW') else None

def _is_unc_path(path_str: Optional[str]) -> TypeGuard[str]:
    """Detecta si una ruta es una ruta de red (UNC), bloqueando su escaneo por seguridad."""
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def _is_within_sandbox(target_norm: str, base_norm: str) -> bool:
    """Verifica la contención de una ruta dentro de la carpeta base del usuario."""
    return target_norm.startswith(base_norm)

def base_directories() -> List[Path]:
    """Retorna la lista de rutas base (LOCALAPPDATA) validadas para el escaneo."""
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

def _is_excluded_file(name: str) -> bool:
    """Compara el nombre de archivo con la lista blanca de seguridad NEVER_TOUCH."""
    return name.lower() in NEVER_TOUCH

@safe_path_operation(False)
def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Usa la API nativa de Windows para identificar archivos del sistema u ocultos."""
    if kernel32 is None: return False
    attrs = kernel32.GetFileAttributesW(entry_path)
    return attrs != 0xFFFFFFFF and bool(attrs & SYSTEM_HIDDEN_FLAGS)

def _is_path_safe(path_str: str, ctx: ScanContext) -> bool:
    """Valida la seguridad de la ruta mediante caché de memoria para optimizar el escaneo."""
    if path_str not in ctx.safety_cache:
        p = Path(path_str)
        ctx.safety_cache[path_str] = p.exists() and is_safe_to_modify(p) and not is_protected_path(p)
    return ctx.safety_cache[path_str]

def _should_skip_entry(entry: os.DirEntry, ctx: ScanContext) -> bool:
    """
    Aplica filtros de seguridad sobre una entrada de directorio.
    Verifica exclusiones explícitas, longitud de ruta, validaciones de seguridad
    definidas en safety.py, y atributos de sistema/reparse points.
    """
    if _is_excluded_file(entry.name): return True
    path_str = entry.path
    if len(path_str) >= MAX_PATH_LEN or any(c in path_str for c in PATH_FORBIDDEN_CHARS): return True
    if not _is_path_safe(path_str, ctx): return True
    if entry.is_symlink() or ctx.is_junction(path_str) or _is_system_hidden(path_str, ctx.kernel32): return True
    return False

def _sum_directory_recursive(root_path: str, ctx: ScanContext, depth: int = 0) -> ScanResult:
    """
    Escaneo recursivo de directorios para calcular el peso total.
    
    Args:
        root_path: Ruta a escanear.
        ctx: Objeto ScanContext con el estado compartido del escaneo.
        depth: Profundidad actual para evitar recursión excesiva.
        
    Returns:
        ScanResult con la suma en bytes y éxito de la operación.
    """
    if depth > MAX_SCAN_DEPTH or len(root_path) >= MAX_PATH_LEN: 
        return ScanResult(0, True)
    
    if not _is_within_sandbox(os.path.normpath(root_path), ctx.sandbox_base_norm):
        return ScanResult(0, False)

    total_bytes = 0
    try:
        with os.scandir(root_path) as it:
            for entry in it:
                if _should_skip_entry(entry, ctx): continue
                
                if entry.is_dir(follow_symlinks=False):
                    res = _sum_directory_recursive(entry.path, ctx, depth + 1)
                    total_bytes += res.bytes_found
                else:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        if st.st_size is not None and st.st_size >= 0:
                            file_id = (st.st_dev, st.st_ino)
                            if file_id not in ctx.visited_files:
                                ctx.visited_files.add(file_id)
                                total_bytes += int(st.st_size)
                    except (OSError, PermissionError): continue
        return ScanResult(total_bytes, True)
    except (OSError, PermissionError): return ScanResult(total_bytes, False)

@safe_path_operation(0)
def directory_size(path: Optional[OSPath]) -> int:
    """Calcula el peso total de una ruta, aplicando filtros de seguridad y contexto."""
    if not path or not isinstance(path, (str, Path)): 
        return 0
    
    path_obj = Path(path)
    if not path_obj.exists():
        return 0
        
    resolved_path = path_obj.resolve(strict=True)
    ctx = ScanContext(os.path.normpath(str(resolved_path)), _get_kernel32(), _IS_JUNCTION_FN, set(), set(), {})
    return _sum_directory_recursive(str(resolved_path), ctx, 0).bytes_found

@safe_path_operation(False)
def _is_valid_cache_path(candidate: Path, base_abs_str: str) -> bool:
    """Valida que una ruta sea un directorio de caché real y dentro del alcance permitido."""
    if not candidate.exists() or not candidate.is_dir() or not os.access(candidate, os.R_OK): return False
    real = str(candidate.resolve(strict=True))
    if not _is_within_sandbox(os.path.normpath(real), base_abs_str): return False
    if not is_safe_to_modify(Path(real)) or is_protected_path(Path(real)): return False
    return not (candidate.is_symlink() or _IS_JUNCTION_FN(real) or _is_excluded_file(candidate.name))

@safe_path_operation(Path())
def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    """Combina la base del usuario con la subruta relativa del navegador."""
    if not _is_valid_rel_path(rel_str): return Path()
    target = real_base.joinpath(*rel_str.split("\\"))
    if not target.exists() or not target.is_dir(): return Path()
    target_real = target.resolve(strict=True)
    if len(str(target_real)) >= MAX_PATH_LEN: return Path()
    if _is_within_sandbox(os.path.normpath(str(target_real)), str(real_base)):
        return target_real
    return Path()

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """Identifica todas las carpetas de caché de navegador detectables en las rutas base."""
    raw_bases = list(bases) if bases is not None else base_directories()
    if not raw_bases: return []
    
    browser_map = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    found: List[BrowserCache] = []
    ctx = ScanContext("", _get_kernel32(), _IS_JUNCTION_FN, set(), set(), {})
    
    for base in raw_bases:
        try:
            real_base = base.resolve(strict=True)
            ctx.sandbox_base_norm = os.path.normpath(str(real_base))
            for browser_name, rel_str in browser_map.items():
                candidate = _resolve_browser_path(real_base, rel_str)
                if candidate != Path() and _is_valid_cache_path(candidate, ctx.sandbox_base_norm):
                    ctx.visited_files.clear()
                    scan_res = _sum_directory_recursive(str(candidate), ctx, 0)
                    if scan_res.bytes_found > 0:
                        found.append(BrowserCache(str(browser_name), candidate, scan_res.bytes_found))
        except (OSError, RuntimeError): continue
    
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Calcula el total de bytes de una lista de hallazgos de caché."""
    return sum(c.size_bytes for c in caches) if caches else 0

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Formatea la información de hallazgos para visualización en la UI."""
    current_caches = caches if isinstance(caches, list) else detect_profiles()
    if not current_caches: return ["No se detectaron cachés de navegador en este sistema."]
    total_mb = round(total_cache_bytes(current_caches) / BYTES_TO_MB, 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
