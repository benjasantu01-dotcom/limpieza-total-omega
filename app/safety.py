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
from functools import lru_cache

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
_SYSTEM_ROOTS_PARTS: Final[frozenset[str]] = frozenset({p.name.lower() for p in _SYSTEM_ROOTS})


def normalize(path: PathLike) -> Path:
    """
    Convierte una ruta a objeto Path absoluto y resuelto.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: se esperaba str o PathLike, recibió {type(path)}")
    
    str_path = str(path).strip()
    if not str_path:
        raise ValueError("La ruta proporcionada está vacía.")
        
    try:
        p = Path(str_path).expanduser()
        return p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def _contains_protected_name(path: Path) -> bool:
    """Verifica si alguno de los componentes de la ruta es un nombre reservado."""
    return not PROTECTED_DIR_NAMES.isdisjoint(part.lower() for part in path.parts)


def is_drive_root(path: PathLike) -> bool:
    r"""Verifica si la ruta corresponde a la raíz del sistema de archivos (ej. C:\)."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return False


@lru_cache(maxsize=256)
def is_protected_path(path: PathLike) -> bool:
    """
    Evalúa si una ruta es considerada peligrosa por ser del sistema o red.
    """
    if not path or not isinstance(path, (str, os.PathLike)):
        return True
    
    raw_path = str(path).strip()
    if not raw_path or raw_path.startswith(("\\\\", "//")):
        return True
        
    try:
        p = normalize(path)
        if p.is_symlink() or is_drive_root(p):
            return True
        
        parts_lower = {part.lower() for part in p.parts}
        return not PROTECTED_DIR_NAMES.isdisjoint(parts_lower) or not _SYSTEM_ROOTS_PARTS.isdisjoint(parts_lower)
    except (PermissionError, OSError, ValueError, TypeError):
        return True 


def is_within_directory(
    child: PathLike,
    parent: PathLike,
    allow_equal: bool = False,
) -> bool:
    """
    Verifica si 'child' reside físicamente dentro de 'parent' evitando symlinks.
    """
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        if not c.is_absolute() or not p.is_absolute():
            return False
            
        for path_to_check in [c, p]:
            for parent_dir in path_to_check.parents:
                if parent_dir.exists():
                    stat = os.lstat(parent_dir)
                    if hasattr(stat, "st_reparse_tag") and stat.st_reparse_tag != 0:
                        return False
            
        if c == p:
            return allow_equal
        c.relative_to(p)
        return True
    except (ValueError, TypeError, OSError):
        return False


def is_sensitive_file(path: PathLike) -> bool:
    """Verifica si la extensión del archivo es crítica para el sistema."""
    if path is None:
        return True
    try:
        return normalize(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Valida la seguridad para operaciones de escritura/borrado.
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")
        
    p = normalize(path)
    if not p.parts:
        raise UnsafePathError("Ruta inválida: no contiene componentes detectables.")

    if str(p).startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas UNC o de red no permitidas.")
    
    if p.exists() and p.is_symlink():
        raise UnsafePathError("Operación bloqueada: symlink detectado.")
    
    try:
        if p.exists():
            stat = p.lstat()
            if hasattr(stat, "st_reparse_tag") and stat.st_reparse_tag != 0:
                raise UnsafePathError("Operación bloqueada: punto de reparse detectado.")
    except (OSError, PermissionError):
        pass

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida o raíz.")
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Operación bloqueada: extensión sensible detectada ({p.suffix}).")
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Versión booleana para chequeos preventivos sin levantar excepciones."""
    try:
        return isinstance(ensure_safe_to_modify(path, allow_sensitive=allow_sensitive), Path)
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una lista de rutas, retornando solo las seguras."""
    if paths is None:
        return []
    return [normalize(c) for c in paths if c and is_safe_to_modify(c, allow_sensitive=allow_sensitive)]


def describe_protection(path: PathLike) -> str:
    """Retorna una cadena explicativa sobre por qué una ruta no es segura."""
    if not path:
        return "La ruta está vacía."
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
