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

_SYSTEM_ROOTS: Final[frozenset[Path]] = frozenset(
    Path(os.environ[v]).resolve() for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)


def _has_invalid_chars(path_str: str) -> bool:
    """Detecta caracteres nulos o secuencias de dispositivos reservados."""
    norm = os.path.normpath(path_str)
    return bool("\0" in path_str or re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str) or
                norm.startswith(r"\\?") or norm.startswith(r"\\."))


def _is_reserved_device_name(name: str) -> bool:
    """Valida si el nombre base del archivo coincide con dispositivos reservados del sistema."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))


@lru_cache(maxsize=1024)
def _is_system_or_hidden(path: Path) -> bool:
    """Verifica atributos de sistema u oculto mediante la API Win32."""
    if os.name != 'nt':
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & (0x02 | 0x04))
    except (OSError, AttributeError, TypeError):
        return False


@lru_cache(maxsize=1024)
def _is_reparse_point(path: Path) -> bool:
    """Determina si la ruta es un punto de reparse (Junction/Symlink)."""
    try:
        stats = path.lstat()
        return bool(getattr(stats, "st_file_attributes", 0) & 0x400) or path.is_symlink()
    except (OSError, PermissionError):
        return True 


def _is_file_in_use(path: Path) -> bool:
    """Intenta abrir el archivo en modo exclusivo para testear bloqueos."""
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
    """Valida la integridad del archivo antes de cualquier operación de modificación."""
    if not p.exists():
        raise UnsafePathError(f"El archivo {p.name} ya no existe.")

    checks = [
        (not os.access(p, os.W_OK), "inaccesible (sin permisos de escritura)"),
        (_is_reparse_point(p), "punto de reparse detectado"),
        (_is_readonly(p), "atributo de solo lectura"),
        (_is_file_in_use(p), "archivo en uso por otro proceso"),
        (_is_system_or_hidden(p), "atributo de sistema u oculto"),
        (p.is_file() and p.stat().st_nlink > 1, "múltiples enlaces (hard link)")
    ]
    
    for is_unsafe, reason in checks:
        if is_unsafe:
            raise UnsafePathError(f"Operación bloqueada para {p.name}: {reason}.")


@lru_cache(maxsize=1024)
def _is_readonly(path: Path) -> bool:
    """Verifica el bit S_IWRITE en el stat del sistema de archivos."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=2048)
def normalize(path: PathLike) -> Path:
    """Normaliza, expande y resuelve una ruta a formato absoluto, validando longitud."""
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: tipo {type(path)} no soportado.")
    
    str_path = str(path).strip()
    if not str_path or len(str_path) > 260:
        raise ValueError("La ruta está vacía o excede el límite de longitud.")
        
    try:
        return Path(str_path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta normalizada coincide con la raíz (anchor) de su unidad."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return True


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """Determina si la ruta reside en una ubicación protegida mediante heurísticas."""
    if not path:
        return True
    
    try:
        p = normalize(path)
        if not PROTECTED_DIR_NAMES.isdisjoint(part.lower() for part in p.parts):
            return True
            
        if not _SYSTEM_ROOTS.isdisjoint(p.parents) or p in _SYSTEM_ROOTS:
            return True
        
        return p == Path(p.anchor) or (p.exists() and _is_reparse_point(p))
    except (PermissionError, OSError, ValueError, TypeError, RuntimeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica si la ruta 'child' está contenida lógicamente dentro de 'parent'."""
    try:
        c, p = normalize(child), normalize(parent)
        if allow_equal and c == p:
            return True
        return p in c.parents
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=512)
def is_sensitive_file(path: PathLike) -> bool:
    """Verifica si la extensión del archivo figura en la lista de SENSITIVE_EXTENSIONS."""
    try:
        return Path(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: PathLike | None = None) -> Path:
    """Validador principal de seguridad para operaciones de escritura."""
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")

    if not isinstance(path, (str, Path)):
        raise UnsafePathError(f"Tipo de entrada no soportado: {type(path)}")

    p = normalize(path)
    path_str = str(path)
    
    if any(part in ("..", "...") for part in path_str.replace("/", os.sep).split(os.sep)):
        raise UnsafePathError("Operación bloqueada: posible intento de path traversal.")

    if base_dir and not is_within_directory(p, base_dir, allow_equal=True):
        raise UnsafePathError("Operación bloqueada: intento de acceso fuera del directorio base.")
    
    if is_within_directory(p, Path.cwd(), allow_equal=True):
        raise UnsafePathError("Operación bloqueada: el archivo pertenece al directorio de ejecución.")

    if _has_invalid_chars(path_str) or _is_reserved_device_name(p.name):
        raise UnsafePathError("Ruta inválida o formato de dispositivo bloqueado.")
    
    if path_str.startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas UNC/red no permitidas.")
    
    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida.")
        
    if p.exists():
        _check_file_integrity(p)
        try:
            if p.is_symlink() and not is_within_directory(p.resolve(), p.parent, allow_equal=True):
                raise UnsafePathError("Operación bloqueada: symlink inseguro detectado.")
        except (OSError, RuntimeError):
            raise UnsafePathError("Operación bloqueada: error al resolver symlink.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError("Operación bloqueada: extensión sensible.")
    
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Interfaz booleana para realizar chequeos de seguridad sin interrumpir el flujo."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una colección de rutas, retornando solo aquellas que cumplen los criterios de seguridad."""
    valid: list[Path] = []
    for p in paths:
        try:
            if is_safe_to_modify(p, allow_sensitive=allow_sensitive):
                valid.append(normalize(p))
        except (TypeError, ValueError, OSError):
            continue
    return valid


def describe_protection(path: PathLike) -> str:
    """Provee una descripción humana del motivo por el cual una ruta fue marcada como insegura."""
    try:
        p = normalize(path)
        raw_str = str(path)
    except (TypeError, ValueError):
        return "Ruta mal formada."
    if raw_str.startswith(("\\\\", "//")):
        return f"'{raw_str}' es una ruta de red."
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
