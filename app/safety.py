"""
safety.py — la capa que impide que la app rompa el sistema.

POR QUÉ EXISTE (leer antes de tocar nada acá)
---------------------------------------------
El código de `app/` lo reescribe una IA de forma autónoma, sin supervisión,
durante días. Confiar en que "la IA va a tener cuidado" no es una defensa.
Este módulo convierte el cuidado en una regla que se puede verificar:

  - Ninguna operación destructiva puede tocar rutas de sistema.
  - Ninguna operación destructiva puede tocar la raíz de una unidad.
  - Mover o restaurar archivos no puede escapar de la carpeta destino
    (protección contra rutas maliciosas tipo "../../Windows").
  - Todo borrado es explícito del usuario; nunca automático.
"""

from __future__ import annotations
import os
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Optional

# Alias para facilitar la lectura de firmas de funciones que aceptan rutas
PathLike: TypeAlias = Union[str, os.PathLike]

__all__ = [
    "UnsafePathError",
    "PROTECTED_DIR_NAMES",
    "SENSITIVE_EXTENSIONS",
    "normalize",
    "is_drive_root",
    "is_protected_path",
    "is_within_directory",
    "ensure_safe_to_modify",
    "filter_safe_paths",
    "is_sensitive_file",
    "describe_protection",
]


class UnsafePathError(Exception):
    """Excepción lanzada cuando una operación intenta manipular rutas protegidas."""


# Carpetas que nunca se recorren ni se modifican, en ningún sistema.
PROTECTED_DIR_NAMES: frozenset[str] = frozenset({
    # Windows
    "windows", "winnt", "system32", "syswow64", "system", "boot",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "perflogs", "msocache", "$windows.~bt", "$windows.~ws",
    "windowsapps", "assembly", "winsxs", "drivers", "drivestore",
    # Datos de credenciales / claves del usuario
    ".ssh", ".gnupg", "microsoft\\crypto", "protect",
    # Unix / macOS (por si se corre fuera de Windows)
    "bin", "sbin", "usr", "etc", "var", "lib", "lib64", "proc", "sys",
    "dev", "boot", "root", "library", "applications",
})

# Extensiones que no se tocan aunque estén en una carpeta permitida:
SENSITIVE_EXTENSIONS: frozenset[str] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

# Cache de rutas críticas para evitar llamadas repetidas al entorno
_SYSTEM_ROOTS: tuple[Path, ...] = tuple(
    Path(os.environ[v]).resolve() 
    for v in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
)


def normalize(path: PathLike) -> Path:
    """
    Normaliza una ruta a absoluta y resuelta para evitar manipulaciones de '..'.

    Args:
        path: Ruta a normalizar.

    Returns:
        Un objeto Path absoluto y resuelto.

    Raises:
        ValueError: Si la ruta está vacía.
        TypeError: Si el tipo de entrada es inválido.
    """
    if path is None or (isinstance(path, str) and not path.strip()):
        raise ValueError("La ruta proporcionada está vacía.")
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida en normalize: se esperaba str o PathLike, recibió {type(path)}")
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta corresponde al punto de montaje de una unidad."""
    try:
        p = normalize(path)
        return p.parent == p or str(p) == p.anchor
    except Exception:
        return False


def is_protected_path(path: PathLike) -> bool:
    """
    Determina si una ruta es peligrosa por residir en un directorio de sistema.
    Valida junctions y subdirectorios de SYSTEM_ROOTS.
    """
    try:
        raw_p = Path(path)
        if raw_p.is_symlink() or (hasattr(raw_p, 'is_junction') and raw_p.is_junction()):
            return True
        p = normalize(path)
    except Exception:
        return True 
        
    if is_drive_root(p):
        return True
    
    # Verificación de partes de ruta contra lista de nombres bloqueados
    p_parts_lower = {part.lower() for part in p.parts}
    if not PROTECTED_DIR_NAMES.isdisjoint(p_parts_lower):
        return True
        
    # Verificación por anidamiento mediante comparación de anchors y padres directos
    for root in _SYSTEM_ROOTS:
        try:
            if p == root or root in p.parents:
                return True
        except (ValueError, RuntimeError):
            continue
    return False


def is_within_directory(
    child: PathLike,
    parent: PathLike,
    allow_equal: bool = False,
) -> bool:
    """Valida si 'child' está contenido dentro de 'parent'."""
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        if c == p:
            return allow_equal
        c.relative_to(p)
        return True
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


def is_sensitive_file(path: PathLike) -> bool:
    """Verifica si el archivo tiene una extensión en SENSITIVE_EXTENSIONS."""
    try:
        return normalize(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, Exception):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Valida si una ruta puede ser modificada (borrada o movida).
    
    Args:
        path: Ruta a validar.
        allow_sensitive: Si es True, permite archivos con extensiones críticas.
        
    Raises:
        UnsafePathError: Si la ruta viola las reglas de seguridad.
    """
    if path is None:
        raise UnsafePathError("No se puede validar una ruta None.")
        
    try:
        p = normalize(path)
    except (TypeError, ValueError) as e:
        raise UnsafePathError(f"Ruta mal formada: {path}") from e
    except Exception as e:
        raise UnsafePathError(f"Ruta inaccesible: {path}") from e

    if p.exists() and (p.is_block_device() or p.is_char_device()):
        raise UnsafePathError(f"Operación bloqueada: '{p}' es un dispositivo especial.")

    if is_drive_root(p):
        raise UnsafePathError(f"Operación bloqueada: '{p}' es la raíz de una unidad.")
    if is_protected_path(p):
        raise UnsafePathError(f"Operación bloqueada: '{p}' está en una ruta de sistema protegida.")
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(
            f"Operación bloqueada: '{p.name}' tiene una extensión sensible ({p.suffix})."
        )
    return p


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una lista de rutas, manteniendo solo las seguras."""
    safe: list[Path] = []
    if paths is None:
        return safe
    for candidate in paths:
        try:
            safe.append(ensure_safe_to_modify(candidate, allow_sensitive=allow_sensitive))
        except (UnsafePathError, TypeError, ValueError):
            continue
    return safe


def describe_protection(path: PathLike) -> str:
    """Genera un diagnóstico textual de por qué una ruta está protegida."""
    try:
        p = normalize(path)
    except (TypeError, ValueError, Exception):
        return "Ruta mal formada: no se puede analizar."
    if is_drive_root(p):
        return f"'{p}' es la raíz de una unidad: nunca se modifica."
    if is_protected_path(p):
        protegida = next(
            (part for part in p.parts if part.lower() in PROTECTED_DIR_NAMES),
            "ruta de sistema",
        )
        return f"'{p}' está protegida por contener '{protegida}'."
    if is_sensitive_file(p):
        return f"'{p.name}' tiene extensión sensible ({p.suffix}): no se borra automáticamente."
    return f"'{p}' se puede modificar con confirmación del usuario."
