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
import time
from enum import Enum, auto
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, TypeGuard
from functools import lru_cache

PathLike: TypeAlias = Union[str, os.PathLike]
ViolationPredicate: TypeAlias = Callable[[Path, os.stat_result], bool]

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
    """Categorías de riesgo detectadas durante la validación de integridad."""
    INACCESSIBLE = "inaccesible"
    REPARSE_POINT = "punto de reparse"
    READ_ONLY = "solo lectura"
    IN_USE = "en uso"
    SYSTEM_HIDDEN = "sistema/oculto"
    HARD_LINK = "hard link detectado"
    SYMLINK = "enlace simbólico detectado"
    ADS = "ADS (flujos alternativos)"
    EMPTY_FILE = "archivo vacío"
    EXCESSIVE_DEPTH = "profundidad excesiva"
    MOUNT_POINT = "punto de montaje detectado"


# Directorios críticos del sistema que nunca deben ser modificados
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

# Extensiones ejecutables o de configuración consideradas riesgosas
SENSITIVE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

_SYSTEM_ROOT_PATHS: Final[tuple[str, ...]] = tuple(
    os.path.normcase(os.environ[v]) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)

_INTEGRITY_CACHE: dict[str, tuple[float, bool]] = {}
CACHE_TTL: Final[float] = 2.0


class _IntegrityCheck(NamedTuple):
    """
    Define una regla de seguridad para archivos.
    'reason' indica el tipo de riesgo.
    'predicate' es una función que evalúa el estado del archivo; retorna True 
    si el archivo debe considerarse inseguro (bloqueado).
    """
    reason: ProtectionReason
    predicate: ViolationPredicate


class _CheckResult(NamedTuple):
    """Resultado del chequeo de integridad para fines de reporte."""
    is_safe: bool
    reason: ProtectionReason | None = None


def is_running_as_admin() -> bool:
    """Verifica si el proceso actual tiene privilegios elevados (Administrador)."""
    if os.name != 'nt':
        try:
            return os.geteuid() == 0
        except (AttributeError, OSError):
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _has_invalid_chars(path_str: str | None) -> bool:
    """Valida que la ruta no contenga caracteres de control o marcas RTL."""
    if not isinstance(path_str, str) or not path_str: 
        return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str))


def _is_reserved_device_name(name: str) -> bool:
    """Comprueba si el nombre es un dispositivo reservado (ej. NUL, CON)."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))


def _has_alternate_data_stream(path: Path) -> bool:
    """
    Detecta flujos de datos alternativos (ADS). 
    En Windows, un ADS se denota por la presencia de un carácter ':' adicional 
    al del drive letter (ej. `archivo.txt:stream`).
    """
    return ":" in path.name and len(path.name.split(":")) > 2


@lru_cache(maxsize=2048)
def _is_system_or_hidden(path: Path) -> bool:
    """Verifica atributos 'Sistema' u 'Oculto' mediante WinAPI (GetFileAttributesW)."""
    if os.name != 'nt' or not isinstance(path, Path):
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1: return False
        return bool(attrs & (0x02 | 0x04))
    except (OSError, AttributeError, TypeError, ValueError):
        return False 


@lru_cache(maxsize=2048)
def _is_reparse_point(path: Path) -> bool:
    """Identifica nodos de reparse (junctions, symlinks). Vital para evitar recursión infinita."""
    if os.name != 'nt':
        return path.is_symlink()
    if not isinstance(path, Path):
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1: return False
        return bool(attrs & 0x400)
    except (OSError, AttributeError, TypeError, ValueError):
        return False


@lru_cache(maxsize=1024)
def _is_file_in_use(path_str: str) -> bool:
    """
    Verifica si el archivo está en uso exclusivo usando acceso de escritura.
    Permite lectura compartida (FILE_SHARE_READ), fallando solo si es imposible escribir.
    """
    path = Path(path_str)
    if not path.exists():
        return False
    if os.name == 'nt':
        try:
            # GENERIC_WRITE (0x40000000), OPEN_EXISTING (3), FILE_SHARE_READ (0x00000001)
            handle = ctypes.windll.kernel32.CreateFileW(
                str(path), 0x40000000, 0x00000001, None, 3, 0x00000080, None
            )
            if handle == -1: return True
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        except (AttributeError, OSError):
            return True
    return not os.access(path, os.W_OK)


def _is_sensitive_extension(path: Path) -> bool:
    """Valida si la extensión del archivo está marcada como sensible."""
    return path.suffix.lower() in SENSITIVE_EXTENSIONS


# Pipeline de validaciones: cada predicado es una condición necesaria para la seguridad
_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _IntegrityCheck(ProtectionReason.REPARSE_POINT, lambda p, _: _is_reparse_point(p)),
    _IntegrityCheck(ProtectionReason.READ_ONLY, lambda _, st: not bool(st.st_mode & stat.S_IWRITE)),
    _IntegrityCheck(ProtectionReason.IN_USE, lambda p, _: _is_file_in_use(str(p))),
    _IntegrityCheck(ProtectionReason.SYSTEM_HIDDEN, lambda p, _: _is_system_or_hidden(p)),
    _IntegrityCheck(ProtectionReason.HARD_LINK, lambda p, st: p.is_file() and st.st_nlink > 1),
    _IntegrityCheck(ProtectionReason.ADS, lambda p, _: _has_alternate_data_stream(p)),
    _IntegrityCheck(ProtectionReason.EMPTY_FILE, lambda p, st: p.is_file() and st.st_size == 0),
    _IntegrityCheck(ProtectionReason.MOUNT_POINT, lambda p, _: os.path.ismount(p)),
]


def _check_file_integrity(path: Path) -> None:
    """
    Ejecuta el pipeline de validaciones sobre un archivo existente con caché temporal.
    Lanza UnsafePathError si alguna regla de integridad es violada.
    """
    path_key = str(path)
    now = time.monotonic()
    
    if path_key in _INTEGRITY_CACHE:
        timestamp, is_safe = _INTEGRITY_CACHE[path_key]
        if now - timestamp < CACHE_TTL:
            if not is_safe: raise UnsafePathError("Operación denegada (cache hit).")
            return

    if len(path.parts) > 64:
        raise UnsafePathError(f"Profundidad de ruta inusual: {ProtectionReason.EXCESSIVE_DEPTH.value}")

    try:
        file_stat: os.stat_result = path.lstat()
    except (PermissionError, OSError) as e:
        raise UnsafePathError(f"Error de acceso a metadatos: {ProtectionReason.INACCESSIBLE.value} ({e.strerror})")

    for rule in _VALIDATORS:
        try:
            if rule.predicate(path, file_stat):
                _INTEGRITY_CACHE[path_key] = (now, False)
                raise UnsafePathError(f"Operación denegada: {rule.reason.value}")
        except (OSError, PermissionError):
            # Si el predicado falla, asumimos precaución e ignoramos la validación específica
            continue
            
    _INTEGRITY_CACHE[path_key] = (now, True)


@lru_cache(maxsize=2048)
def _is_readonly(path: Path) -> bool:
    """Valida el bit de solo lectura en los metadatos."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Estandariza rutas para asegurar consistencia y validar longitud."""
    if path is None: raise ValueError("Ruta nula recibida.")
    try:
        path_str = str(path).strip()
    except (TypeError, ValueError): raise ValueError("Entrada no convertible a string.")

    if not path_str: raise ValueError("Entrada de ruta vacía.")
    if len(path_str) >= 260: raise ValueError("Longitud de ruta excede el límite permitido.")
        
    try:
        p = Path(path_str).expanduser()
        # Impedimos resolución que atraviese enlaces simbólicos fuera del scope esperado
        return p.resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as e:
        raise ValueError(f"Error irrecuperable al normalizar {path_str}: {e}")


def is_drive_root(path: PathLike) -> bool:
    """Retorna True si la ruta es la raíz de una unidad (ej. C:\)."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError): return True


@lru_cache(maxsize=2048)
def is_protected_path(path: PathLike) -> bool:
    """Valida contra la lista de directorios prohibidos del sistema."""
    if not path: return True

    try:
        p = normalize(path)
        normalized_str = os.path.normcase(str(p))
        
        return (
            any(normalized_str.startswith(root) for root in _SYSTEM_ROOT_PATHS) or
            any(part.lower() in PROTECTED_DIR_NAMES for part in p.parts) or
            p == Path(p.anchor)
        )
    except (ValueError, TypeError, OSError, RuntimeError): 
        return True


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica si child es un subdirectorio o archivo contenido en parent usando rutas reales."""
    if child is None or parent is None: return False
    try:
        c_path = Path(os.path.realpath(str(normalize(child))))
        p_path = Path(os.path.realpath(str(normalize(parent))))
        if not c_path.is_absolute() or not p_path.is_absolute(): return False
        if not allow_equal and c_path == p_path: return False
        return os.path.commonpath([str(c_path), str(p_path)]) == str(p_path)
    except (ValueError, TypeError, OSError, RuntimeError): return False


@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Retorna True si la extensión está en la lista negra."""
    if not path: return True
    try:
        return _is_sensitive_extension(Path(str(path)))
    except (TypeError, ValueError, OSError): return True 


def _validate_basic_path_safety(path: Path, path_str: str) -> None:
    """Verifica condiciones de seguridad estructurales elementales."""
    if _has_invalid_chars(path_str) or _is_reserved_device_name(path.name):
        raise UnsafePathError("Nombre de ruta o dispositivo inválido.")
    if ".." in path.parts:
        raise UnsafePathError("Intento de path traversal detectado.")
    if path_str.startswith(("\\\\", "//")):
        raise UnsafePathError("Rutas de red (UNC) no permitidas.")
    if path.anchor and not os.path.exists(path.anchor):
        raise UnsafePathError("Unidad o punto de montaje no disponible.")
    if len(str(path)) >= 260:
        raise UnsafePathError("La ruta resultante excede la longitud máxima permitida.")


def _validate_boundary_conditions(path: Path, base_dir: PathLike | None) -> None:
    """Verifica si la operación respeta límites geográficos definidos."""
    if base_dir and not is_within_directory(path, base_dir, allow_equal=True):
        raise UnsafePathError("Operación fuera del directorio base permitido.")
    if is_within_directory(path, Path.cwd(), allow_equal=True):
        raise UnsafePathError("Operación denegada en el directorio de ejecución.")
    if is_drive_root(path) or is_protected_path(path):
        raise UnsafePathError("Ruta de sistema protegida.")
    if _is_reparse_point(path):
        raise UnsafePathError("Seguridad denegada: nodo de reparse detectado.")


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: PathLike | None = None) -> Path:
    """
    Valida si una ruta puede ser escrita de forma segura.

    Args:
        path: Ruta a evaluar.
        allow_sensitive: Si es True, permite archivos de configuración sensibles.
        base_dir: Directorio base opcional que delimita la operación.

    Raises:
        UnsafePathError: Si la ruta infringe políticas de seguridad o integridad.

    Returns:
        Path: La ruta normalizada si es segura para modificar.
    """
    if path is None: raise UnsafePathError("Ruta nula recibida para validación.")

    p = normalize(path)
    _validate_basic_path_safety(p, str(p))
    _validate_boundary_conditions(p, base_dir)
    
    if p.exists():
        if not (p.is_file() or p.is_dir()):
            raise UnsafePathError("Tipo de archivo no soportado.")
        _check_file_integrity(p)
    else:
        parent = p.parent
        if parent.exists():
            if not os.access(parent, os.W_OK):
                raise UnsafePathError("Escritura bloqueada: directorio padre restringido.")
        elif is_protected_path(parent):
            raise UnsafePathError("Escritura bloqueada: directorio padre protegido.")
    
    if not allow_sensitive and _is_sensitive_extension(p):
        raise UnsafePathError("Extensión de archivo sensible.")
            
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> TypeGuard[PathLike]:
    """Retorna True si la ruta pasa las validaciones de seguridad."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError): return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una lista de rutas, manteniendo solo aquellas seguras."""
    results = []
    for p in paths:
        if p is not None and is_safe_to_modify(p, allow_sensitive=allow_sensitive):
            try: results.append(normalize(p))
            except (ValueError, TypeError, OSError): continue
    return results


def describe_protection(path: PathLike) -> str:
    """Retorna un mensaje legible explicando por qué una ruta fue bloqueada."""
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        raw_str = str(path)
    except (TypeError, ValueError): return "Ruta mal formada."

    if raw_str.startswith(("\\\\", "//")): return f"'{raw_str}' es ruta de red."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(p): return f"'{p}' protegida por sistema."
    if p.exists():
        if len(p.parts) > 64: return f"'{p}' profundidad excesiva."
        if not os.access(p, os.W_OK): return f"'{p}' sin permisos de escritura."
        if os.path.islink(p): return f"'{p}' es un enlace simbólico."
        if os.path.ismount(p): return f"'{p}' es un punto de montaje."
        if _is_readonly(p): return f"'{p}' es solo lectura."
        if _is_file_in_use(str(p)): return f"'{p}' en uso por otro proceso."
        if _is_system_or_hidden(p): return f"'{p}' atributo oculto/sistema."
        if _has_alternate_data_stream(p): return f"'{p}' contiene ADS."
        if p.is_file() and p.stat().st_size == 0: return f"'{p}' es un archivo vacío."
    if _is_sensitive_extension(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
