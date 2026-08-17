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
from enum import Enum, auto
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable
from functools import lru_cache

PathLike: TypeAlias = Union[str, os.PathLike]
ViolationPredicate: TypeAlias = Callable[[], bool]

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
    "is_running_as_admin",
]


class UnsafePathError(Exception):
    """Lanzada cuando una operación intenta manipular rutas protegidas."""


class ProtectionReason(Enum):
    """Categorías de riesgo para fallos de integridad."""
    INACCESSIBLE = "inaccesible"
    REPARSE_POINT = "punto de reparse"
    READ_ONLY = "solo lectura"
    IN_USE = "en uso"
    SYSTEM_HIDDEN = "sistema/oculto"
    HARD_LINK = "hard link detectado"
    ADS = "ADS (flujos alternativos)"
    EMPTY_FILE = "archivo vacío"


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

_SYSTEM_ROOT_PARTS: Final[frozenset[tuple[str, ...]]] = frozenset(
    Path(os.environ[v]).parts for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)


class _IntegrityCheck(NamedTuple):
    """Representa un criterio de validación para un archivo específico."""
    reason: ProtectionReason
    predicate: ViolationPredicate


def is_running_as_admin() -> bool:
    """Verifica si el proceso actual tiene privilegios elevados (Administrador)."""
    if os.name != 'nt':
        return os.getuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _is_permission_denied(e: Exception) -> bool:
    """Valida si un error de sistema es un error estándar de acceso denegado (POSIX/Win)."""
    return isinstance(e, (PermissionError, OSError)) and getattr(e, 'errno', 0) in (13, 5)


def _has_invalid_chars(path_str: str) -> bool:
    """Detecta caracteres ilegales en rutas (null bytes, control chars, RTL marks)."""
    return bool("\0" in path_str or re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str))


def _is_reserved_device_name(name: str) -> bool:
    """Comprueba si el nombre del archivo colisiona con dispositivos reservados del kernel (ej. NUL)."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))


def _has_alternate_data_stream(path: Path) -> bool:
    """Identifica flujos NTFS (ADS) detectando ':' adicional en el nombre del archivo."""
    return ":" in path.name and len(path.name.split(":")) > 2


@lru_cache(maxsize=2048)
def _is_system_or_hidden(path: Path) -> bool:
    """Consulta atributos de archivo win32 para verificar flags de sistema u oculto."""
    if os.name != 'nt' or not path.exists():
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & (0x02 | 0x04))
    except (OSError, AttributeError, TypeError, PermissionError):
        return False


@lru_cache(maxsize=2048)
def _is_reparse_point(path: Path) -> bool:
    """Detecta puntos de reparse (junctions/symlinks) mediante atributos win32 para evitar recursión."""
    if os.name != 'nt':
        return path.is_symlink()
    if not path.exists():
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & 0x400)
    except (OSError, AttributeError, TypeError, PermissionError):
        return False


def _is_file_in_use(path: Path) -> bool:
    """Verifica exclusividad de acceso intentando abrir el archivo con modo solo lectura."""
    if not path.exists() or not path.is_file():
        return False
    try:
        handle = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        os.close(handle)
        return False
    except OSError as e:
        return getattr(e, 'winerror', 0) == 32 or getattr(e, 'errno', 0) == 13


def _check_file_integrity(p: Path) -> None:
    """
    Realiza una batería de verificaciones de integridad antes de modificar archivos.
    Lanza UnsafePathError ante el primer criterio de riesgo detectado.
    """
    if not p.exists():
        raise UnsafePathError(f"El archivo {p.name} ya no existe.")
    if len(p.parts) > 32:
        raise UnsafePathError("Ruta demasiado profunda.")

    try:
        # Usamos lstat para evitar seguir enlaces/reparse points durante la inspección
        st = p.lstat()
    except OSError:
        raise UnsafePathError(f"No se pudo acceder a metadatos de {p.name}")

    checks = [
        (ProtectionReason.INACCESSIBLE, lambda: not os.access(p, os.W_OK)),
        (ProtectionReason.REPARSE_POINT, lambda: _is_reparse_point(p)),
        (ProtectionReason.READ_ONLY, lambda: not bool(st.st_mode & stat.S_IWRITE)),
        (ProtectionReason.IN_USE, lambda: _is_file_in_use(p)),
        (ProtectionReason.SYSTEM_HIDDEN, lambda: _is_system_or_hidden(p)),
        (ProtectionReason.HARD_LINK, lambda: p.is_file() and st.st_nlink > 1),
        (ProtectionReason.ADS, lambda: _has_alternate_data_stream(p)),
        (ProtectionReason.EMPTY_FILE, lambda: p.is_file() and st.st_size == 0)
    ]

    for reason, predicate in checks:
        if predicate():
            raise UnsafePathError(f"Operación denegada en {p.name}: {reason.value}.")


@lru_cache(maxsize=2048)
def _is_readonly(path: Path) -> bool:
    """Consulta el estado del bit de solo lectura en el sistema de archivos."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Convierte una ruta a su forma absoluta canónica, validando longitud y existencia."""
    if path is None:
        raise ValueError("Ruta nula recibida.")
    
    path_str = str(path).strip()
    if not path_str:
        raise ValueError("Entrada de ruta vacía.")
    
    if len(path_str) > 260:
        raise ValueError("Longitud de ruta excedida.")
        
    try:
        return Path(path_str).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as e:
        if _is_permission_denied(e):
            raise ValueError("Acceso denegado durante la normalización.")
        raise ValueError(f"Error al normalizar: {e}")


def is_drive_root(path: PathLike) -> bool:
    """Determina si la ruta apunta a la raíz de un dispositivo de almacenamiento."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return True


@lru_cache(maxsize=2048)
def is_protected_path(path: PathLike) -> bool:
    """Evalúa si una ruta pertenece a ubicaciones críticas protegidas por sistema."""
    if not path:
        return True
    
    try:
        p = normalize(path)
        p_parts = p.parts
        if any(p_parts[:len(root)] == root for root in _SYSTEM_ROOT_PARTS):
            return True
        if any(part.lower() in PROTECTED_DIR_NAMES for part in p_parts):
            return True
        if p.exists() and _is_reparse_point(p):
            return True
        return p == Path(p.anchor)
    except (PermissionError, OSError, ValueError, TypeError, RuntimeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica si la ruta 'child' está contenida bajo el directorio 'parent'."""
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        if allow_equal and c == p:
            return True
        return p in c.parents
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Filtra archivos por extensiones críticas que afectan la configuración del SO."""
    try:
        return Path(str(path)).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def _validate_basic_path_safety(p: Path, path_str: str) -> None:
    """Realiza validaciones de seguridad estructural previas a la modificación."""
    if _has_invalid_chars(path_str) or _is_reserved_device_name(p.name):
        raise UnsafePathError("Nombre de ruta o dispositivo inválido.")
    
    if any(part in ("..", "...") for part in path_str.replace("/", os.sep).split(os.sep)):
        raise UnsafePathError("Intento de path traversal detectado.")

    if path_str.startswith(("\\\\", "//")):
        raise UnsafePathError("Rutas de red (UNC) no permitidas.")

    if p.anchor and not os.path.exists(p.anchor):
        raise UnsafePathError("Unidad o punto de montaje no disponible.")


def _validate_boundary_conditions(p: Path, base_dir: PathLike | None) -> None:
    """Verifica que la ruta se mantenga dentro de los límites operativos permitidos."""
    if base_dir and not is_within_directory(p, base_dir, allow_equal=True):
        raise UnsafePathError("Operación fuera del directorio base permitido.")
    
    if is_within_directory(p, Path.cwd(), allow_equal=True):
        raise UnsafePathError("Operación denegada en el directorio de ejecución.")

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Ruta de sistema protegida.")


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: PathLike | None = None) -> Path:
    """Valida la integridad y seguridad de la ruta antes de realizar cambios persistentes."""
    if path is None:
        raise UnsafePathError("Ruta nula recibida para validación.")

    path_str = str(path)
    if _has_invalid_chars(path_str):
        raise UnsafePathError("Caracteres ilegales detectados en la ruta.")

    try:
        p = normalize(path)
    except (ValueError, TypeError) as e:
        raise UnsafePathError(f"Ruta inválida o mal formada: {e}")

    _validate_basic_path_safety(p, path_str)
    _validate_boundary_conditions(p, base_dir)
    
    if p.exists():
        _check_file_integrity(p)
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError("Extensión de archivo sensible.")
    
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Wrapper booleano: devuelve True solo si la ruta es apta para modificación."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una lista de rutas, retornando solo aquellas que son seguras."""
    valid: list[Path] = []
    if paths is None: return valid
    for p in paths:
        if p is not None and is_safe_to_modify(p, allow_sensitive=allow_sensitive):
            try:
                valid.append(normalize(p))
            except (TypeError, ValueError, OSError):
                continue
    return valid


def describe_protection(path: PathLike) -> str:
    """Provee una explicación legible para el usuario de por qué se denegó una ruta."""
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        raw_str = str(path)
    except (TypeError, ValueError):
        return "Ruta mal formada."

    if raw_str.startswith(("\\\\", "//")): return f"'{raw_str}' es ruta de red."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(p): return f"'{p}' protegida por sistema."
    if p.exists():
        if not os.access(p, os.W_OK): return f"'{p}' sin permisos de escritura."
        if _is_readonly(p): return f"'{p}' es solo lectura."
        if _is_file_in_use(p): return f"'{p}' en uso por otro proceso."
        if _is_system_or_hidden(p): return f"'{p}' atributo oculto/sistema."
        if _has_alternate_data_stream(p): return f"'{p}' contiene ADS."
        if p.is_file() and p.stat().st_size == 0: return f"'{p}' es un archivo vacío."
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
