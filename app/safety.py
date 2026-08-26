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
    """Categorías de riesgo para fallos de integridad."""
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

_PATH_CACHE: dict[str, bool] = {}


class _IntegrityCheck(NamedTuple):
    """Regla que asocia una razón de protección con una función de validación."""
    reason: ProtectionReason
    predicate: ViolationPredicate


class _CheckResult(NamedTuple):
    """Resultado estructurado de un chequeo de integridad."""
    is_safe: bool
    reason: ProtectionReason | None = None


def is_running_as_admin() -> bool:
    """Verifica si el proceso actual tiene privilegios elevados (Administrador)."""
    if os.name != 'nt':
        try:
            return os.getuid() == 0
        except AttributeError:
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _has_invalid_chars(path_str: str) -> bool:
    """Detecta caracteres ilegales en rutas (null bytes, control chars, RTL marks)."""
    if not isinstance(path_str, str) or not path_str: return True
    return bool("\0" in path_str or re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str))


def _is_reserved_device_name(name: str) -> bool:
    """Comprueba si el nombre del archivo colisiona con dispositivos reservados del kernel."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))


def _has_alternate_data_stream(path: Path) -> bool:
    """Identifica flujos NTFS (ADS) detectando ':' adicional en el nombre del archivo."""
    return ":" in path.name and len(path.name.split(":")) > 2


@lru_cache(maxsize=2048)
def _is_system_or_hidden(path: Path) -> bool:
    """Verifica atributos de sistema u oculto mediante APIs nativas de Windows."""
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
    """Detecta junctions, symlinks y puntos de reparse mediante APIs nativas de Windows."""
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
    """Verifica exclusividad de acceso al archivo intentando abrirlo en modo exclusivo."""
    path = Path(path_str)
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_EXCL)
        os.close(fd)
        return False
    except OSError:
        return True


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
    Realiza validaciones físicas sobre el archivo.
    Lanza UnsafePathError si alguna regla definida en _VALIDATORS se incumple
    o si la profundidad de la ruta supera los umbrales seguros.
    """
    if len(path.parts) > 64:
        raise UnsafePathError(f"Profundidad de ruta inusual: {ProtectionReason.EXCESSIVE_DEPTH.value}")

    try:
        file_stat: os.stat_result = path.stat()
    except (PermissionError, OSError) as e:
        raise UnsafePathError(f"Error de acceso a metadatos: {e}")

    if not os.access(path, os.W_OK):
        raise UnsafePathError(f"Operación denegada: {ProtectionReason.INACCESSIBLE.value}")

    for rule in _VALIDATORS:
        try:
            if rule.predicate(path, file_stat):
                raise UnsafePathError(f"Operación denegada: {rule.reason.value}")
        except (OSError, PermissionError):
            continue


@lru_cache(maxsize=2048)
def _is_readonly(path: Path) -> bool:
    """Valida el bit de solo lectura en el sistema de archivos."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Estandariza rutas: expande '~', resuelve componentes y verifica límites."""
    if path is None: raise ValueError("Ruta nula recibida.")
    try:
        path_str = str(path).strip()
    except (TypeError, ValueError): raise ValueError("Entrada no convertible a string.")

    if not path_str: raise ValueError("Entrada de ruta vacía.")
    if len(path_str) >= 260: raise ValueError("Longitud de ruta excede el límite permitido.")
        
    try:
        p = Path(path_str).expanduser()
        return p.resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as e:
        raise ValueError(f"Error irrecuperable al normalizar {path_str}: {e}")


def is_drive_root(path: PathLike) -> bool:
    """Retorna True si la ruta normalizada coincide con la raíz de un volumen."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError): return True


def is_protected_path(path: PathLike) -> bool:
    """Evalúa si una ruta reside en directorios críticos del sistema."""
    if not path: return True
    path_key = str(path)
    if path_key in _PATH_CACHE: return _PATH_CACHE[path_key]

    try:
        p = normalize(path)
        p_str = os.path.normcase(str(p))
    except (ValueError, TypeError, OSError, RuntimeError): return True

    is_protected = any(p_str.startswith(root) for root in _SYSTEM_ROOT_PATHS if root) or \
                   not PROTECTED_DIR_NAMES.isdisjoint(part.lower() for part in p.parts) or \
                   p == Path(p.anchor)
    
    _PATH_CACHE[path_key] = is_protected
    return is_protected


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Valida jerarquía: confirma si 'child' es descendiente de 'parent'."""
    if child is None or parent is None: return False
    try:
        c, p = normalize(child), normalize(parent)
        if allow_equal and c == p: return True
        return p in c.parents
    except (ValueError, TypeError, OSError, RuntimeError): return False


@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Verifica si la extensión del archivo coincide con tipos protegidos."""
    if not path: return True
    try:
        return Path(str(path)).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError): return True 


def _validate_basic_path_safety(path: Path, path_str: str) -> None:
    """Realiza chequeos estructurales básicos: travesía inválida, caracteres y rutas UNC."""
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
    """Confirma que la ruta reside dentro de los límites operativos permitidos."""
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
    Valida exhaustivamente si una ruta es apta para escritura.
    Lanza UnsafePathError si la ruta es insegura o pertenece al sistema.
    """
    if path is None: raise UnsafePathError("Ruta nula recibida para validación.")

    p = normalize(path)
    _validate_basic_path_safety(p, str(p))
    _validate_boundary_conditions(p, base_dir)
    
    if p.exists():
        if not p.is_file() and not p.is_dir():
            raise UnsafePathError("Tipo de archivo no soportado.")
        _check_file_integrity(p)
    else:
        parent = p.parent
        if parent.exists():
            if not os.access(parent, os.W_OK):
                raise UnsafePathError("Escritura bloqueada: directorio padre restringido.")
        elif is_protected_path(parent):
            raise UnsafePathError("Escritura bloqueada: directorio padre protegido.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError("Extensión de archivo sensible.")
            
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> TypeGuard[PathLike]:
    """Wrapper booleano: devuelve True solo si la ruta supera toda validación de seguridad."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError): return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una colección de rutas retornando únicamente las aptas para modificación."""
    results = []
    for p in paths:
        if p is not None and is_safe_to_modify(p, allow_sensitive=allow_sensitive):
            try: results.append(normalize(p))
            except (ValueError, TypeError, OSError): continue
    return results


def describe_protection(path: PathLike) -> str:
    """Genera una descripción legible de la razón exacta por la que una ruta no es segura."""
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
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
