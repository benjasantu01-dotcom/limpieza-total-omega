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

# Carpetas y archivos excluidos explícitamente para garantizar la integridad
# de los datos del usuario (sesiones, contraseñas, configuraciones).
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
    """Representación de una carpeta de caché detectada y su peso en disco."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB con precisión de 2 decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)


def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """Carga kernel32.dll para validación de atributos Win32."""
    if os.name != 'nt':
        return None
    try:
        dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        if hasattr(dll, 'GetFileAttributesW'):
            return dll
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return None

def _is_unc_path(path_str: str) -> bool:
    """Detecta si la ruta es un recurso de red (UNC) para evitar bloqueos/riesgos."""
    return path_str.startswith(r"\\") or path_str.startswith("//")

def base_directories() -> List[Path]:
    """
    Localiza la ruta raíz de los datos de usuario (%LOCALAPPDATA%).
    
    Returns:
        Lista conteniendo la ruta de perfil local si es segura.
    """
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env or _is_unc_path(local_env):
        return []
    
    try:
        p = Path(local_env)
        if not p.exists():
            return []
        path_local = p.resolve(strict=True)
        if path_local.is_dir() and is_safe_to_modify(path_local) and not is_protected_path(path_local):
            return [path_local]
    except (OSError, RuntimeError, PermissionError):
        pass
    return []


def _is_path_inside_base(real_target: Path, real_base: Path) -> bool:
    """Verifica si el objetivo reside dentro de la jerarquía de base (sandbox)."""
    if not isinstance(real_target, Path) or not isinstance(real_base, Path):
        return False
    try:
        target_abs = str(real_target.resolve(strict=True))
        base_abs = str(real_base.resolve(strict=True))
        
        if len(target_abs) >= MAX_PATH_LEN or len(base_abs) >= MAX_PATH_LEN:
            return False
            
        return os.path.commonpath([target_abs, base_abs]) == base_abs
    except (OSError, ValueError, RuntimeError, PermissionError):
        return False


def _is_excluded_file(name: Optional[str]) -> bool:
    """Indica si un nombre de archivo está en la lista de bloqueo."""
    return name is not None and name.lower() in NEVER_TOUCH


def __is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Consulta atributos Win32 para detectar archivos ocultos."""
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
    """Filtra archivos protegidos, symlinks y junctions."""
    if entry.name is None:
        return True
    
    try:
        if _is_excluded_file(entry.name):
            return True
        
        path = entry.path
        if not path or len(path) >= MAX_PATH_LEN or any(c in path for c in '\0\r\n') or _is_unc_path(path):
            return True
        
        if entry.is_symlink() or is_junction_fn(path):
            return True
            
        if __is_system_hidden(path, kernel32):
            return True
            
    except (OSError, PermissionError, UnicodeEncodeError):
        return True
    return False


def _is_safe_to_traverse(path_obj: Path, base_check_path: Optional[Path]) -> bool:
    """Valida que el directorio sea seguro y esté en la base autorizada."""
    if not isinstance(path_obj, Path):
        return False
    try:
        p_res = path_obj.resolve(strict=True)
        if _is_unc_path(str(p_res)) or not p_res.is_dir() or not is_safe_to_modify(p_res) or is_protected_path(p_res):
            return False
        if base_check_path and not _is_path_inside_base(p_res, base_check_path):
            return False
        return True
    except (OSError, RuntimeError, PermissionError, ValueError):
        return False


def _is_valid_traversal_step(entry: os.DirEntry, root_base: str) -> bool:
    """Verifica condiciones de seguridad específicas para descender en un subdirectorio."""
    return (
        entry.is_dir(follow_symlinks=False) and 
        not entry.is_symlink() and 
        _is_path_inside_base(Path(entry.path), Path(root_base))
    )

def _process_entry(entry: os.DirEntry, root_base: str, is_junction_fn: JunctionChecker, kernel32: Optional[ctypes.WinDLL], memo: Dict[str, int], depth: int) -> int:
    """Procesa un elemento individual: si es directorio válido, desciende recursivamente; si es archivo, obtiene su tamaño."""
    if depth > MAX_SCAN_DEPTH:
        return 0
    try:
        if _is_valid_traversal_step(entry, root_base):
            if not is_junction_fn(entry.path):
                return _sum_directory_recursive(entry.path, is_junction_fn, kernel32, memo, root_base, depth + 1)
        elif entry.is_file(follow_symlinks=False):
            try:
                stat_res = entry.stat(follow_symlinks=False)
                return int(stat_res.st_size) if hasattr(stat_res, 'st_size') else 0
            except (OSError, PermissionError):
                return 0
    except (OSError, PermissionError):
        return 0
    return 0


def _sum_directory_recursive(
    root_abs: str, 
    is_junction_fn: JunctionChecker, 
    kernel32: Optional[ctypes.WinDLL],
    memo: Dict[str, int],
    root_base: str,
    depth: int = 0
) -> int:
    """Motor recursivo de cálculo de tamaño con memoización y validación de sandbox."""
    if not isinstance(root_abs, str) or not root_abs or depth > MAX_SCAN_DEPTH or _is_unc_path(root_abs):
        return 0
    
    root_path = Path(root_abs)
    if not root_path.is_absolute() or not root_path.exists():
        return 0
        
    if root_abs in memo:
        return memo[root_abs]

    # Verificar que el punto de inicio de este sub-escaneo sigue en zona segura
    if not _is_path_inside_base(root_path, Path(root_base)):
        return 0

    total: int = 0
    try:
        with os.scandir(root_abs) as it:
            for entry in it:
                if _should_skip_entry(entry, kernel32, is_junction_fn):
                    continue
                total += _process_entry(entry, root_base, is_junction_fn, kernel32, memo, depth)
    except (PermissionError, OSError):
        pass
    
    memo[root_abs] = total
    return total


def directory_size(path: Union[str, Path, None]) -> int:
    """Interfaz pública para obtener el tamaño seguro de un directorio."""
    if path is None:
        return 0
    p = Path(path)
    if not p.is_absolute() or not _is_safe_to_traverse(p, None):
        return 0
    try:
        resolved = str(p.resolve(strict=True))
        if _is_unc_path(resolved):
            return 0
        return _sum_directory_recursive(resolved, _IS_JUNCTION_FN, _get_kernel32(), {}, resolved)
    except (OSError, RuntimeError, PermissionError):
        return 0


def _is_valid_cache_path(candidate: Path, base_path: Path, is_junction_fn: JunctionChecker) -> bool:
    """Verifica si el directorio de caché cumple criterios de seguridad."""
    try:
        if not isinstance(candidate, Path) or not candidate.is_absolute() or not candidate.exists() or not candidate.is_dir():
            return False
        real_candidate = candidate.resolve(strict=True)
        if _is_unc_path(str(real_candidate)) or not _is_path_inside_base(real_candidate, base_path):
            return False
        if not is_safe_to_modify(real_candidate) or is_protected_path(real_candidate):
            return False
        return not (candidate.is_symlink() or is_junction_fn(str(candidate)) or _is_excluded_file(candidate.name))
    except (OSError, PermissionError, RuntimeError, ValueError):
        return False


def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    """Combina base y ruta relativa validando integridad básica."""
    if not isinstance(rel_str, str) or any(c in rel_str for c in '\0\r\n'):
        return real_base
    try:
        parts = rel_str.split("\\")
        target = real_base.joinpath(*parts)
        if len(str(target)) >= MAX_PATH_LEN:
            return real_base
        return target
    except (TypeError, ValueError, OSError):
        return real_base


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[BrowserMap] = None
) -> List[BrowserCache]:
    """Escanea perfiles conocidos y devuelve una lista de objetos BrowserCache."""
    raw_bases = bases if bases is not None else base_directories()
    browser_map = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    
    k32 = _get_kernel32()
    # Cache global de directorios ya procesados para evitar re-escaneo
    perf_cache: Dict[str, int] = {}
    found: List[BrowserCache] = []
    scanned_paths: set[str] = set()
    
    if not isinstance(raw_bases, (list, tuple)):
        return []

    for base in raw_bases:
        if not isinstance(base, Path) or not base.is_dir():
            continue
        try:
            real_base = base.resolve(strict=True)
            for browser_name, rel_str in browser_map.items():
                if not isinstance(rel_str, str):
                    continue
                
                candidate = _resolve_browser_path(real_base, rel_str)
                if not _is_valid_cache_path(candidate, real_base, _IS_JUNCTION_FN):
                    continue
                
                real_candidate = str(candidate.resolve(strict=True))
                if real_candidate in scanned_paths:
                    continue
                
                # Se reutiliza perf_cache para evitar re-calcular nodos comunes
                size = _sum_directory_recursive(real_candidate, _IS_JUNCTION_FN, k32, perf_cache, str(real_base))
                if size > 0:
                    scanned_paths.add(real_candidate)
                    found.append(BrowserCache(str(browser_name), Path(real_candidate), size))
        except (OSError, PermissionError, TypeError, ValueError, RuntimeError):
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Calcula el tamaño agregado en bytes de una lista de BrowserCache."""
    return sum(c.size_bytes for c in caches) if caches else 0


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Prepara reporte textual del escaneo para la interfaz de usuario."""
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
