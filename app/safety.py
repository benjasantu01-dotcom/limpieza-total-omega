"""
safety.py — capa de seguridad para la manipulación de archivos.

Este módulo provee funciones de validación para prevenir modificaciones
accidentales o maliciosas en directorios del sistema o archivos críticos.
Todo cambio destructivo debe pasar por `ensure_safe_to_modify`.
"""

from __future__ import annotations
import os
import stat
import re
import ctypes
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final
from functools import lru_cache

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
    """Lanzada cuando una operación intenta manipular rutas protegidas."""


# DIRECTORIOS_BLOQUEADOS: Nombres que, si aparecen en una ruta, indican riesgo de sistema.
PROTECTED_DIR_NAMES: Final[frozenset[str]] = frozenset({
    "windows", "winnt", "system32", "syswow64", "system", "boot",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "perflogs", "msocache", "$windows.~bt", "$windows.~ws",
    "windowsapps", "assembly", "winsxs", "drivers", "drivestore",
    ".ssh", ".gnupg", "microsoft\\crypto", "protect",
    "bin", "sbin", "usr", "etc", "var", "lib", "lib64", "proc", "sys",
    "dev", "root", "library", "applications",
})

# EXTENSIONES_SENSIBLES: Archivos fundamentales para la integridad o configuración.
SENSITIVE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

_SYSTEM_ROOTS: Final[list[Path]] = [
    Path(os.environ[v]) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
]

_RESERVED_NAMES: Final[frozenset[str]] = frozenset({
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", 
    "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"
})


def _has_invalid_chars(path_str: str) -> bool:
    """Valida si la cadena contiene caracteres prohibidos o prefijos de dispositivos Windows."""
    if not isinstance(path_str, str):
        return True
    norm = os.path.normpath(path_str)
    return bool("\0" in path_str or re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str) or
                norm.startswith(r"\\?") or norm.startswith(r"\\."))


def _is_reserved_device_name(name: str) -> bool:
    """Verifica si el nombre de archivo corresponde a un dispositivo reservado (e.g., CON, NUL)."""
    base = name.split('.')[0]
    return base.lower() in _RESERVED_NAMES


def _is_system_or_hidden(path: Path) -> bool:
    """Consulta atributos Win32 para detectar si el archivo tiene flags de sistema o es oculto."""
    if os.name != 'nt':
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & (0x02 | 0x04))
    except (OSError, AttributeError, TypeError):
        return False


def _is_reparse_point(path: Path) -> bool:
    """Determina si la ruta es un punto de reparse (Junction/Symlink)."""
    try:
        stats = path.lstat()
        return bool(getattr(stats, "st_file_attributes", 0) & 0x400) or path.is_symlink()
    except (OSError, PermissionError):
        return True 


def _is_file_in_use(path: Path) -> bool:
    """Intenta abrir el archivo en modo exclusivo para detectar bloqueos de otros procesos."""
    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_EXCL)
        os.close(fd)
        return False
    except (OSError, PermissionError):
        return True


def _check_file_integrity(p: Path) -> None:
    """Ejecuta una serie de validaciones sobre un archivo existente."""
    if any([
        not os.access(p, os.W_OK),
        _is_reparse_point(p),
        _is_readonly(p),
        _is_file_in_use(p),
        _is_system_or_hidden(p),
        (p.is_file() and p.stat().st_nlink > 1)
    ]):
        raise UnsafePathError("Operación bloqueada: archivo inaccesible, protegido o sistema.")


@lru_cache(maxsize=1024)
def _is_readonly(path: Path) -> bool:
    """Verifica si el atributo de solo lectura está activo en el sistema de archivos."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=2048)
def normalize(path: PathLike) -> Path:
    """Normaliza una ruta a su forma absoluta y resuelta."""
    if path is None or not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: tipo {type(path) if path is not None else 'None'} no soportado.")
    
    str_path = str(path).strip()
    if not str_path:
        raise ValueError("La ruta proporcionada está vacía.")
        
    try:
        return Path(str_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta provista representa la raíz de una unidad lógica."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return True


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """Determina si la ruta reside en un directorio crítico del sistema."""
    if not path:
        return True
    
    try:
        p = normalize(path)
        is_protected = any(part.lower() in PROTECTED_DIR_NAMES for part in p.parts)
        if not is_protected:
            for sys_root in _SYSTEM_ROOTS:
                if os.path.commonpath([str(p), str(sys_root)]) == str(sys_root):
                    is_protected = True
                    break
        
        if not is_protected:
            is_protected = p == Path(p.anchor) or (p.exists() and _is_reparse_point(p))
            
        return is_protected
    except (PermissionError, OSError, ValueError, TypeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Valida si 'child' es un subdirectorio o archivo contenido dentro de 'parent'."""
    try:
        c, p = normalize(child), normalize(parent)
        return p in c.parents or (allow_equal and c == p)
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=512)
def is_sensitive_file(path: PathLike) -> bool:
    """Verifica si la extensión del archivo está marcada como sensible."""
    try:
        return Path(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """Validador estricto para operaciones de escritura."""
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")

    p = normalize(path)
    str_val = str(p)
    if _has_invalid_chars(str_val) or len(str_val) > 260 or _is_reserved_device_name(p.stem):
        raise UnsafePathError("Ruta inválida, demasiado larga o formato bloqueado.")
    if str_val.startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas de red no permitidas.")
    
    if p.exists():
        _check_file_integrity(p)

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError("Operación bloqueada: extensión sensible.")
    
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Versión booleana de `ensure_safe_to_modify` para chequeos de filtrado."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una colección de rutas retornando solo aquellas seguras para modificar."""
    valid: list[Path] = []
    for p in paths:
        try:
            norm_p = normalize(p)
            if is_safe_to_modify(norm_p, allow_sensitive=allow_sensitive):
                valid.append(norm_p)
        except (TypeError, ValueError, OSError):
            continue
    return valid


def describe_protection(path: PathLike) -> str:
    """Genera una explicación legible sobre por qué una ruta fue marcada como insegura."""
    try:
        p = normalize(path)
    except (TypeError, ValueError):
        return "Ruta mal formada."
    if str(p).startswith(("\\\\", "//")):
        return f"'{p}' es una ruta de red."
    if is_drive_root(p):
        return f"'{p}' es la raíz de una unidad."
    if is_protected_path(p):
        return f"'{p}' protegida por reglas de sistema."
    if p.exists():
        try:
            if not os.access(p, os.W_OK):
                return f"'{p}' sin permisos de escritura."
            if _is_readonly(p):
                return f"'{p}' atributos de solo lectura."
            if _is_file_in_use(p):
                return f"'{p}' está en uso por otro proceso."
            if _is_system_or_hidden(p):
                return f"'{p}' archivo de sistema o oculto."
        except (OSError, PermissionError):
            return f"'{p}' error al verificar permisos."
    if is_sensitive_file(p):
        return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
