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

Diseño testeable: `detect_profiles` recibe la carpeta base por parámetro,
así en CI se puede simular una instalación con carpetas temporales.
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

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
BROWSER_CACHE_PATHS = {
    "Google Chrome": r"Google\Chrome\User Data\Default\Cache",
    "Microsoft Edge": r"Microsoft\Edge\User Data\Default\Cache",
    "Brave": r"BraveSoftware\Brave-Browser\User Data\Default\Cache",
    "Opera": r"Opera Software\Opera Stable\Cache",
    "Vivaldi": r"Vivaldi\User Data\Default\Cache",
    "Chrome (código)": r"Google\Chrome\User Data\Default\Code Cache",
    "Edge (código)": r"Microsoft\Edge\User Data\Default\Code Cache",
    "Chrome (GPU)": r"Google\Chrome\User Data\Default\GPUCache",
}

# Nombres que este módulo nunca reporta ni toca, aunque estén dentro del
# perfil: son datos del usuario, no caché.
NEVER_TOUCH = frozenset({
    "login data", "cookies", "web data", "bookmarks", "history",
    "preferences", "local state", "extensions", "profile",
})

SAFETY_NOTE = (
    "Solo se listan carpetas de caché, que el navegador regenera solo. "
    "Nunca se tocan contraseñas, cookies, marcadores ni historial. "
    "Cerrá el navegador antes de limpiar su caché, o los archivos en uso "
    "no se van a poder mover."
)


@dataclass
class BrowserCache:
    """Carpeta de caché detectada de un navegador."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Convierte bytes a MB con dos decimales de precisión."""
        return round(self.size_bytes / (1024 * 1024), 2)


def base_directories() -> list[Path]:
    """Carpetas base donde buscar perfiles de navegador (solo Windows)."""
    if os.name != "nt":
        return []
    local = os.environ.get("LOCALAPPDATA")
    return [Path(local)] if local and Path(local).is_dir() else []


def directory_size(path: str | os.PathLike) -> int:
    """
    Calcula el tamaño total de una carpeta de forma iterativa.
    
    Ignora enlaces simbólicos para evitar bucles infinitos. Captura errores de
    permisos (PermissionError) silenciándolos, ya que es esperable al escanear
    perfiles de usuario.
    """
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            # Filtrar directorios para no seguir symlinks
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for f in files:
                try:
                    fp = os.path.join(root, f)
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        return 0
    return total


def detect_profiles(bases: Sequence[Path] | None = None, 
                    cache_paths: dict[str, str] | None = None) -> list[BrowserCache]:
    """
    Explora los directorios base en busca de cachés definidas en cache_paths.
    
    Valida que las rutas resultantes no escapen del directorio base (prevención
    de Path Traversal básico) y excluye elementos en NEVER_TOUCH.
    """
    if bases is None:
        bases = base_directories()
    if cache_paths is None:
        cache_paths = BROWSER_CACHE_PATHS

    found: list[BrowserCache] = []
    
    for base in bases:
        base_path = Path(base).resolve()
        if not base_path.is_dir():
            continue
            
        for browser, relative in cache_paths.items():
            # Construye ruta normalizando separadores según SO
            candidate = base_path.joinpath(*relative.replace("\\", "/").split("/")).resolve()
            
            # Seguridad: Verificar que el candidato siga bajo base_path
            if not str(candidate).startswith(str(base_path)):
                continue
            
            if candidate.name.lower() in NEVER_TOUCH:
                continue
                
            if candidate.is_dir():
                found.append(BrowserCache(
                    browser=browser,
                    path=candidate,
                    size_bytes=directory_size(candidate),
                ))
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Suma el tamaño de todas las cachés detectadas."""
    if caches is None:
        caches = detect_profiles()
    return sum(cache.size_bytes for cache in caches)


def summarize(caches: list[BrowserCache] | None = None) -> list[str]:
    """Genera un reporte legible de las cachés encontradas y su peso total."""
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
