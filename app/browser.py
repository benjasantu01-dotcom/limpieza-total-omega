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
        return round(self.size_bytes / (1024 * 1024), 2)


def base_directories() -> list[Path]:
    """Carpetas base donde buscar perfiles de navegador."""
    if os.name != "nt":
        return []
    local = os.environ.get("LOCALAPPDATA")
    return [Path(local)] if local and Path(local).is_dir() else []


def directory_size(path: str | os.PathLike) -> int:
    """Tamaño total de una carpeta, saltando lo que no se pueda leer."""
    total = 0
    base = Path(path)
    if not base.is_dir():
        return 0
    for root, _, files in os.walk(base):
        for name in files:
            try:
                candidate = Path(root) / name
                if candidate.is_symlink():
                    continue
                total += candidate.stat().st_size
            except (OSError, PermissionError):
                continue
    return total


def detect_profiles(bases=None, cache_paths=None) -> list[BrowserCache]:
    """Detecta cachés de navegador existentes y mide su tamaño.

    `bases` y `cache_paths` se pueden inyectar para testear con una
    estructura de carpetas simulada, sin navegadores instalados.
    """
    if bases is None:
        bases = base_directories()
    if cache_paths is None:
        cache_paths = BROWSER_CACHE_PATHS

    found: list[BrowserCache] = []
    for base in bases:
        for browser, relative in cache_paths.items():
            # Se normaliza el separador para que las rutas con "\" también
            # funcionen cuando los tests corren en Linux.
            candidate = Path(base).joinpath(*str(relative).replace("\\", "/").split("/"))
            if candidate.name.lower() in NEVER_TOUCH:
                continue  # nunca reportar carpetas de datos del usuario
            if candidate.is_dir():
                found.append(BrowserCache(
                    browser=browser,
                    path=candidate,
                    size_bytes=directory_size(candidate),
                ))
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches=None) -> int:
    """Suma el tamaño de todas las cachés detectadas."""
    if caches is None:
        caches = detect_profiles()
    return sum(cache.size_bytes for cache in caches)


def summarize(caches=None) -> list[str]:
    """Resumen legible de las cachés de navegador encontradas."""
    if caches is None:
        caches = detect_profiles()
    caches = list(caches)
    if not caches:
        return ["No se detectaron cachés de navegador en este sistema."]
    total_mb = round(total_cache_bytes(caches) / (1024 * 1024), 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(caches)} carpeta(s)", ""]
    for cache in caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
