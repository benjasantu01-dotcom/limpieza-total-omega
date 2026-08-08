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
import sys
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
    Path(os.environ[v]).resolve() for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
]

_RESERVED_NAMES: Final[frozenset[str]] = frozenset({
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", 
    "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"
})


def _has_invalid_chars(path_str: str) -> bool:
    """
    Detecta caracteres nulos o secuencias de dispositivos reservados.
    Evita que rutas maliciosas (como 'NUL' o caracteres de control) 
    interfieran con las llamadas de bajo nivel del sistema operativo.
    """
    if not isinstance(path_str, str):
        return True
    norm = os.path.normpath(path_str)
    return bool("\0" in path_str or re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str) or
                norm.startswith(r"\\?") or norm.startswith(r"\\."))


def _is_reserved_device_name(name: str) -> bool:
    """Valida si el nombre base del archivo coincide con dispositivos reservados del sistema."""
    base = name.split('.')[0]
    return base.lower() in _RESERVED_NAMES


def _is_system_or_hidden(path: Path) -> bool:
    """
    Verifica atributos de sistema u oculto mediante la API Win32.
    """
    if os.name != 'nt':
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & (0x02 | 0x04))
    except (OSError, AttributeError, TypeError):
        return False


def _is_reparse_point(path: Path) -> bool:
    """
    Determina si la ruta es un punto de reparse (Junction/Symlink).
    """
    try:
        stats = path.lstat()
        return bool(getattr(stats, "st_file_attributes", 0) & 0x400) or path.is_symlink()
    except (OSError, PermissionError):
        return True 


def _is_file_in_use(path: Path) -> bool:
    """
    Intenta abrir el archivo en modo exclusivo para testear bloqueos.
    """
    if not path.exists() or not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_EXCL)
        os.close(fd)
        return False
    except (PermissionError, BlockingIOError):
        return True
    except OSError:
        return False


def _check_file_integrity(p: Path) -> None:
    """
    Validación exhaustiva de integridad antes de escritura.
    """
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
    """Verifica el bit S_IWRITE en el stat del sistema de archivos."""
    if not path.exists():
        return True
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=2048)
def normalize(path: PathLike) -> Path:
    """
    Transforma y canoniza rutas, validando límites de longitud de sistema.
    """
    if path is None or not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: tipo {type(path) if path is not None else 'None'} no soportado.")
    
    str_path = str(path).strip()
    if not str_path or len(str_path) > 260:
        raise ValueError("La ruta está vacía o excede el límite de longitud.")
        
    try:
        return Path(str_path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta apunta exactamente al anchor (raíz) de la unidad."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return True


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """
    Heurística de seguridad optimizada con manejo de errores de acceso.
    """
    if not path:
        return True
    
    try:
        p = normalize(path)
        if not PROTECTED_DIR_NAMES.isdisjoint(part.lower() for part in p.parts):
            return True
            
        for sys_root in _SYSTEM_ROOTS:
            if sys_root in p.parents or p == sys_root:
                return True
        
        return p == Path(p.anchor) or (p.exists() and _is_reparse_point(p))
    except (PermissionError, OSError, ValueError, TypeError, RuntimeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica contención lógica de subdirectorios."""
    try:
        c, p = normalize(child), normalize(parent)
        return p in c.parents or (allow_equal and c == p)
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=512)
def is_sensitive_file(path: PathLike) -> bool:
    """Indica si la extensión es crítica."""
    try:
        return Path(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: PathLike | None = None) -> Path:
    """
    Validador estricto para operaciones de escritura.
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")

    p = normalize(path)
    
    if ".." in str(path) or ".." in p.parts:
        raise UnsafePathError("Operación bloqueada: posible ataque de path traversal.")

    if base_dir and not is_within_directory(p, base_dir, allow_equal=True):
        raise UnsafePathError("Operación bloqueada: intento de acceso fuera del directorio base.")
    
    if is_within_directory(p, Path.cwd(), allow_equal=True):
        raise UnsafePathError("Operación bloqueada: el archivo pertenece al directorio de ejecución.")

    str_val = str(p)
    if _has_invalid_chars(str_val) or _is_reserved_device_name(p.stem):
        raise UnsafePathError("Ruta inválida o formato bloqueado.")
    if str_val.startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas de red no permitidas.")
    
    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida.")
        
    if p.exists():
        _check_file_integrity(p)
        if p.is_symlink() and not is_within_directory(p.resolve(), p.parent, allow_equal=True):
             raise UnsafePathError("Operación bloqueada: symlink inseguro detectado.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError("Operación bloqueada: extensión sensible.")
    
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Wrapper booleano para realizar chequeos seguros."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una lista de rutas, preservando únicamente las que pasan el check."""
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
    """Genera una explicación amigable sobre el motivo del bloqueo."""
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
