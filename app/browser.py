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
from pathlib import Path, PurePath
from typing import Iterable, Sequence, Dict, List, Optional, Callable, Set
from safety import is_protected_path

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

# Rutas de caché relativas a LOCALAPPDATA (Windows). Solo datos
# regenerables: si se borran, el navegador los vuelve a crear.
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

# Nombres que este módulo nunca reporta ni toca, aunque estén dentro del
# perfil: son datos del usuario, no caché.
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

@dataclass
class BrowserCache:
    """Representación de una carpeta de caché detectada y su peso en disco."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB. Precisión de 2 decimales para reportes."""
        return round(self.size_bytes / (1024 * 1024), 2)


def base_directories() -> List[Path]:
    """
    Obtiene los directorios raíz donde se alojan perfiles (LOCALAPPDATA).
    Retorna una lista vacía si el SO no es Windows o la variable es inexistente.
    """
    if os.name != "nt":
        return []
    
    local: Optional[str] = os.environ.get("LOCALAPPDATA")
    if not local or not isinstance(local, str):
        return []
    
    try:
        path_local = Path(local).resolve()
        if path_local.is_absolute() and path_local.is_dir():
            return [path_local]
        return []
    except (OSError, RuntimeError, ValueError):
        return []


def _is_safe_path(target_path: Optional[Path], base_path: Optional[Path]) -> bool:
    """
    Verificación de seguridad para prevenir Path Traversal y ataques de enlace.
    Valida que 'target_path' pertenezca físicamente a 'base_path'.
    """
    if not isinstance(target_path, Path) or not isinstance(base_path, Path):
        return False
        
    try:
        real_base = base_path.resolve(strict=True)
        real_target = target_path.resolve(strict=True)
        
        # Validar contra lista negra de seguridad antes de cualquier otra comprobación
        if is_protected_path(real_target) or is_protected_path(real_base):
            return False

        # Prevenir Path Traversal: asegurar que real_target sea subdirectorio de real_base
        if os.path.commonpath([real_base, real_target]) != str(real_base):
            return False

        # Detectar caracteres no imprimibles o RTL en el path (evita ocultamiento visual)
        if any(ord(char) < 32 or ord(char) in (0x200E, 0x200F, 0x202A, 0x202E) for char in str(target_path)):
            return False

        is_junction = getattr(os.path, 'isjunction', lambda _: False)
        if real_target.is_symlink() or is_junction(str(real_target)):
            return False

        return True
    except (OSError, ValueError, RuntimeError, PermissionError):
        return False


def _is_excluded_file(name: str) -> bool:
    """Filtro de nombres de archivo protegidos (sesiones, cookies, etc)."""
    if not isinstance(name, str):
        return True
    return name.lower() in NEVER_TOUCH


def _is_system_hidden(entry_path: str, kernel32: ctypes.WinDLL | None) -> bool:
    """Usa la API de Windows para verificar si un archivo tiene atributos de sistema u oculto."""
    if not kernel32 or not isinstance(entry_path, str):
        return False
    try:
        attrs = kernel32.GetFileAttributesW(entry_path)
        # 0x04: SYSTEM, 0x02: HIDDEN, 0xFFFFFFFF (-1): error
        return attrs != -1 and bool(attrs & 0x04 or attrs & 0x02)
    except (OSError, AttributeError, TypeError):
        return False


def _should_skip_entry(entry: os.DirEntry, kernel32: ctypes.WinDLL | None, is_junction_fn: Callable[[str], bool]) -> bool:
    """Valida si una entrada del sistema de archivos debe omitirse durante el escaneo."""
    if _is_system_hidden(entry.path, kernel32):
        return True
    if entry.is_symlink() or is_junction_fn(entry.path):
        return True
    if entry.is_file() and _is_excluded_file(entry.name):
        return True
    return False


def _sum_directory_recursive(root_dir: str, is_junction_fn: Callable[[str], bool], visited: Optional[Set[str]] = None, cache: Optional[Dict[str, int]] = None, depth: int = 0) -> int:
    """
    Realiza un recorrido DFS para calcular el peso total (bytes) de una carpeta.
    Implementa control de ciclos mediante 'visited' y caché de resultados.
    """
    if depth > 10 or not root_dir or not os.path.exists(root_dir):
        return 0
    
    if is_protected_path(Path(root_dir)):
        return 0
        
    if visited is None:
        visited = set()
    if cache is None:
        cache = {}
    
    try:
        real_path = os.path.realpath(root_dir)
        if real_path in visited:
            return 0
        if real_path in cache:
            return cache[real_path]
        visited.add(real_path)
    except (OSError, PermissionError):
        return 0
        
    total_size: int = 0
    kernel32 = ctypes.windll.kernel32 if os.name == 'nt' else None
    
    try:
        with os.scandir(root_dir) as it:
            for entry in it:
                try:
                    if _should_skip_entry(entry, kernel32, is_junction_fn):
                        continue
                    
                    if entry.is_dir():
                        total_size += _sum_directory_recursive(entry.path, is_junction_fn, visited, cache, depth + 1)
                    elif entry.is_file():
                        total_size += entry.stat().st_size
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        pass
    
    cache[real_path] = total_size
    return total_size


def directory_size(path: str | os.PathLike | None) -> int:
    """
    Calcula el peso total en bytes de una carpeta tras validar que sea segura.
    Retorna 0 en caso de error, acceso denegado o ruta protegida.
    """
    if path is None:
        return 0
    
    try:
        p_path = Path(path)
        if not p_path.exists():
            return 0
        root_path = p_path.resolve(strict=True)
        if not root_path.is_absolute() or not root_path.is_dir() or is_protected_path(root_path):
            return 0
        
        is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)
        return _sum_directory_recursive(str(root_path), is_junction)
    except (OSError, PermissionError, RuntimeError, ValueError):
        return 0


def _is_valid_cache_path(candidate: Optional[Path], base_path: Path) -> bool:
    """Verifica si la ruta candidata existe, es segura y no contiene archivos críticos."""
    if not isinstance(candidate, Path) or not isinstance(base_path, Path):
        return False
    try:
        return (
            candidate.exists() and 
            candidate.is_dir() and 
            _is_safe_path(candidate, base_path) and
            not _is_excluded_file(candidate.name)
        )
    except (OSError, PermissionError, RuntimeError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[Dict[str, str]] = None
) -> List[BrowserCache]:
    """Detecta cachés instaladas combinando rutas base y subrutas conocidas."""
    raw_bases = bases if bases is not None else base_directories()
    cache_paths = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)
    perf_cache: Dict[str, int] = {}

    found: List[BrowserCache] = []
    if not isinstance(raw_bases, (list, tuple)) or not isinstance(cache_paths, dict):
        return found
        
    for base in raw_bases:
        if not isinstance(base, Path): continue
        try:
            real_base = base.resolve(strict=True)
        except (OSError, PermissionError):
            continue
            
        for browser_name, relative_path_str in cache_paths.items():
            if not isinstance(relative_path_str, str): continue
            try:
                candidate = real_base.joinpath(*relative_path_str.split("\\"))
                if _is_valid_cache_path(candidate, real_base):
                    size: int = _sum_directory_recursive(str(candidate.resolve()), is_junction, cache=perf_cache)
                    if size > 0:
                        found.append(BrowserCache(
                            browser=browser_name,
                            path=candidate.resolve(),
                            size_bytes=size,
                        ))
            except (OSError, ValueError, TypeError, AttributeError, PermissionError):
                continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Calcula el peso total en bytes acumulado de una colección de caché."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera un informe textual del estado de las cachés encontradas."""
    current_caches: List[BrowserCache] = caches if caches is not None else detect_profiles()
    
    if not current_caches:
        return ["No se detectaron cachés de navegador en este sistema."]
        
    total_mb: float = round(total_cache_bytes(current_caches) / (1024 * 1024), 2)
    lines: List[str] = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
