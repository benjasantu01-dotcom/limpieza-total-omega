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
from typing import Union, Iterable, TypeAlias, Final

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
    "is_safe_to_modify",
    "filter_safe_paths",
    "is_sensitive_file",
    "describe_protection",
]


class UnsafePathError(Exception):
    """Excepción lanzada cuando una operación intenta manipular rutas protegidas."""


# Carpetas que nunca se recorren ni se modifican, en ningún sistema.
PROTECTED_DIR_NAMES: Final[frozenset[str]] = frozenset({
    # Windows
    "windows", "winnt", "system32", "syswow64", "system", "boot",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "perflogs", "msocache", "$windows.~bt", "$windows.~ws",
    "windowsapps", "assembly", "winsxs", "drivers", "drivestore",
    # Datos de credenciales / claves del usuario
    ".ssh", ".gnupg", "microsoft\\crypto", "protect",
    # Unix / macOS
    "bin", "sbin", "usr", "etc", "var", "lib", "lib64", "proc", "sys",
    "dev", "root", "library", "applications",
})

# Extensiones que no se tocan aunque estén en una carpeta permitida:
SENSITIVE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

# Cache de rutas críticas para evitar llamadas repetidas al entorno
_SYSTEM_ROOTS: Final[tuple[Path, ...]] = tuple(
    Path(os.environ[v]).resolve() 
    for v in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
)


def normalize(path: PathLike) -> Path:
    """
    Normaliza una ruta a absoluta y resuelta para evitar manipulaciones de '..'.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: se esperaba str o PathLike, recibió {type(path)}")
    
    str_path = str(path).strip()
    if not str_path:
        raise ValueError("La ruta proporcionada está vacía.")
        
    try:
        return Path(str_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def _contains_protected_name(path: Path) -> bool:
    """Verifica si alguna parte de la ruta coincide con un directorio protegido."""
    return not PROTECTED_DIR_NAMES.isdisjoint({part.lower() for part in path.parts})


def is_drive_root(path: PathLike) -> bool:
    r"""Verifica si la ruta apunta a la raíz de un volumen (ej. C:\ o /)."""
    try:
        p = Path(path).resolve()
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return False


def is_protected_path(path: PathLike) -> bool:
    """
    Determina si una ruta es peligrosa por residir en un directorio de sistema.
    """
    try:
        raw_p = Path(path)
        if raw_p.is_symlink():
            return True
            
        p = normalize(path)
    except (PermissionError, OSError, ValueError, TypeError):
        return True 
        
    if is_drive_root(p):
        return True
    
    if _contains_protected_name(p):
        return True
        
    for root in _SYSTEM_ROOTS:
        try:
            if p == root or root in p.parents:
                return True
        except (ValueError, RuntimeError, PermissionError):
            continue
    return False


def is_within_directory(
    child: PathLike,
    parent: PathLike,
    allow_equal: bool = False,
) -> bool:
    """
    Valida si 'child' es descendiente de 'parent'.
    """
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        if any(part.is_symlink() for part in c.parents):
            return False
            
        if c == p:
            return allow_equal
        c.relative_to(p)
        return True
    except (ValueError, TypeError, OSError):
        return False


def is_sensitive_file(path: PathLike) -> bool:
    """
    Verifica si la extensión del archivo es crítica según SENSITIVE_EXTENSIONS.
    """
    try:
        return normalize(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Valida si una ruta puede ser modificada. Lanza UnsafePathError ante cualquier riesgo.
    """
    if path is None:
        raise UnsafePathError("La ruta proporcionada es None.")
        
    try:
        p = normalize(path)
    except (TypeError, ValueError) as e:
        raise UnsafePathError(f"Ruta mal formada: {path}") from e

    if str(p).startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas UNC no permitidas.")

    try:
        if p.is_symlink():
            raise UnsafePathError("Operación bloqueada: enlaces simbólicos no permitidos.")
        if p.exists() and (p.is_block_device() or p.is_char_device()):
            raise UnsafePathError("Operación bloqueada: dispositivo especial.")
    except OSError:
        pass

    if is_drive_root(p):
        raise UnsafePathError("Operación bloqueada: raíz de unidad.")
    if is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida.")
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Operación bloqueada: extensión sensible detectada ({p.suffix}).")
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """
    Versión booleana de `ensure_safe_to_modify`.
    """
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """
    Filtra una colección de rutas, devolviendo solo aquellas consideradas seguras.
    """
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
    """
    Retorna un string descriptivo sobre el estado de seguridad de una ruta.
    """
    try:
        p = normalize(path)
    except (TypeError, ValueError):
        return "Ruta mal formada: no se puede analizar."
    if str(p).startswith(("\\\\", "//")):
        return f"'{p}' es una ruta de red."
    if is_drive_root(p):
        return f"'{p}' es la raíz de una unidad."
    if is_protected_path(p):
        protegida = next(
            (part for part in p.parts if part.lower() in PROTECTED_DIR_NAMES),
            "ruta de sistema",
        )
        return f"'{p}' está protegida por contener '{protegida}'."
    if is_sensitive_file(p):
        return f"'{p.name}' tiene extensión sensible ({p.suffix})."
    return f"'{p}' se puede modificar con confirmación."
