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
from enum import Enum, auto, IntEnum
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

# Constantes de atributos de archivo Win32
FILE_ATTRIBUTE_HIDDEN: Final[int] = 0x02
FILE_ATTRIBUTE_SYSTEM: Final[int] = 0x04
FILE_ATTRIBUTE_OFFLINE: Final[int] = 0x1000
FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x400
MAX_PATH_LENGTH: Final[int] = 260
MAX_FILE_SIZE: Final[int] = 2 * 1024 * 1024 * 1024  # 2GB límite de seguridad

class SafetyValidationErrorCode(IntEnum):
    """Códigos de error para diagnósticos específicos en fallos de seguridad."""
    GENERIC = 0
    NULL_CHAR = 1
    INVALID_CHARS = 2
    RESERVED_NAME = 3
    UNC_PATH = 4
    PATH_TOO_LONG = 5
    OUT_OF_BOUNDS = 6
    ROOT_ACCESS = 7
    PROTECTED_SYSTEM_PATH = 8
    REPARSE_POINT_DETECTED = 9
    FILE_IN_USE = 10
    SENSITIVE_EXTENSION = 11

class UnsafePathError(Exception):
    """Lanzada cuando una operación intenta manipular rutas protegidas."""
    def __init__(self, message: str, code: SafetyValidationErrorCode = SafetyValidationErrorCode.GENERIC):
        super().__init__(message)
        self.code = code

class ProtectionReason(Enum):
    """Categorías de riesgo detectadas durante la validación de integridad."""
    INACCESSIBLE = "inaccesible"
    REPARSE_POINT = "punto de reparse"
    READ_ONLY = "solo lectura"
    IN_USE = "en uso"
    SYSTEM_HIDDEN = "sistema/oculto/offline"
    HARD_LINK = "hard link detectado"
    SYMLINK = "enlace simbólico detectado"
    ADS = "ADS (flujos alternativos)"
    EMPTY_FILE = "archivo vacío"
    EXCESSIVE_DEPTH = "profundidad excesiva"
    MOUNT_POINT = "punto de montaje detectado"
    EXCESSIVE_SIZE = "tamaño de archivo excedido"


class ValidationContext(Enum):
    """Define si la validación es puramente estructural o requiere acceso a disco."""
    STRUCTURAL = auto()
    INTEGRITY = auto()


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


class _IntegrityCheck(NamedTuple):
    """
    Define una regla de seguridad para archivos.
    
    Attributes:
        reason: El motivo técnico por el cual un archivo podría ser bloqueado.
        predicate: Función que recibe (Path, stat_result) y retorna True si es inseguro.
    """
    reason: ProtectionReason
    predicate: ViolationPredicate


class _CheckResult(NamedTuple):
    """Resultado del chequeo de integridad para fines de reporte."""
    is_safe: bool
    reason: ProtectionReason | None = None


def is_running_as_admin() -> bool:
    """Verifica si el proceso actual posee privilegios elevados."""
    if os.name != 'nt':
        try:
            return os.geteuid() == 0
        except (AttributeError, OSError):
            return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False


def _has_invalid_chars(path_str: str | None) -> bool:
    """Detecta caracteres prohibidos en rutas o potencialmente maliciosos."""
    if not isinstance(path_str, str) or not path_str: 
        return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str))


@lru_cache(maxsize=128)
def _is_reserved_device_name(name: str) -> bool:
    """Comprueba si el nombre colisiona con dispositivos reservados de Windows."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))


@lru_cache(maxsize=512)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta flujos de datos alternativos (ADS) usando separadores ':'."""
    return ":" in path_name and len(path_name.split(":")) > 2


@lru_cache(maxsize=2048)
def _is_system_or_hidden(path: Path) -> bool:
    """Valida si el archivo posee atributos de sistema, oculto u offline."""
    try:
        st = path.lstat()
        return bool(getattr(st, 'st_file_attributes', 0) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_OFFLINE))
    except (AttributeError, OSError):
        return False 


@lru_cache(maxsize=2048)
def _is_junction(path: Path) -> bool:
    """Identifica puntos de unión (Junctions) mediante la WinAPI kernel32."""
    if os.name != 'nt': return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF: return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError, TypeError):
        return False


@lru_cache(maxsize=2048)
def _is_reparse_point(path: Path) -> bool:
    """Determina si la ruta es un punto de reparse (Junction o Symlink)."""
    try:
        st = path.lstat()
        attrs = getattr(st, 'st_file_attributes', 0)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) or _is_junction(path)
    except (AttributeError, OSError):
        return path.is_symlink()


@lru_cache(maxsize=1024)
def _is_file_in_use(path_str: str) -> bool:
    """Verifica mediante un handle si un archivo está bloqueado por otro proceso."""
    if os.name != 'nt' or not isinstance(path_str, str):
        return False
    if not os.path.lexists(path_str):
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(path_str, 0x0080, 0x00000007, None, 3, 0x00000080, None)
        if handle == -1 or handle == 0xFFFFFFFF: 
            return True
        kernel32.CloseHandle(handle)
        return False
    except (AttributeError, OSError, PermissionError, TypeError, ctypes.ArgumentError):
        return True


def _is_sensitive_extension(path: Path) -> bool:
    """Determina si la extensión del archivo figura en SENSITIVE_EXTENSIONS."""
    return path.suffix.lower() in SENSITIVE_EXTENSIONS


_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _IntegrityCheck(ProtectionReason.REPARSE_POINT, lambda p, _: _is_reparse_point(p)),
    _IntegrityCheck(ProtectionReason.READ_ONLY, lambda _, st: not bool(st.st_mode & stat.S_IWRITE)),
    _IntegrityCheck(ProtectionReason.IN_USE, lambda p, _: _is_file_in_use(str(p))),
    _IntegrityCheck(ProtectionReason.SYSTEM_HIDDEN, lambda p, _: _is_system_or_hidden(p)),
    _IntegrityCheck(ProtectionReason.HARD_LINK, lambda p, st: p.is_file() and st.st_nlink > 1),
    _IntegrityCheck(ProtectionReason.ADS, lambda p, _: _has_alternate_data_stream(p.name)),
    _IntegrityCheck(ProtectionReason.EMPTY_FILE, lambda p, st: p.is_file() and st.st_size == 0),
    _IntegrityCheck(ProtectionReason.EXCESSIVE_SIZE, lambda p, st: p.is_file() and st.st_size > MAX_FILE_SIZE),
    _IntegrityCheck(ProtectionReason.MOUNT_POINT, lambda p, _: os.path.ismount(p)),
]


@lru_cache(maxsize=1024)
def _check_file_integrity_cached(path_str: str) -> bool:
    """Ejecuta el conjunto de reglas de integridad y retorna un estado booleano."""
    path = Path(path_str)
    try:
        file_stat = path.stat()
    except (PermissionError, OSError):
        return False
        
    for rule in _VALIDATORS:
        if rule.predicate(path, file_stat):
            return False
    return True


def _check_file_integrity(path: Path) -> None:
    """Wrapper para verificar integridad: lanza UnsafePathError si falla."""
    if not _check_file_integrity_cached(str(path)):
        raise UnsafePathError("Operación denegada por reglas de integridad.")


@lru_cache(maxsize=2048)
def _is_readonly(path: Path) -> bool:
    """Valida si el archivo carece de permisos de escritura."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Estandariza rutas, resolviendo enlaces y validando posibles traversals."""
    if path is None: raise ValueError("Ruta nula recibida.")
    path_str = str(path).strip()
    if not path_str: raise ValueError("Entrada de ruta vacía.")
        
    try:
        p = Path(path_str)
        if ".." in p.parts: raise ValueError("Path traversal detectado.")
        
        current = Path(p.anchor)
        for part in p.parts[1:]:
            current = current / part
            if _is_reparse_point(current):
                raise ValueError(f"Acceso restringido: componente {current} es un punto de reparse.")
                
        return p.resolve()
    except (OSError, RuntimeError, TypeError, PermissionError) as e:
        raise ValueError(f"Error irrecuperable al normalizar {path_str}: {e}")


def is_drive_root(path: PathLike) -> bool:
    """Determina si la ruta normalizada corresponde a la raíz de una unidad."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError): return True


@lru_cache(maxsize=2048)
def is_protected_path(path: PathLike) -> bool:
    """Verifica si la ruta reside dentro de directorios de sistema restringidos."""
    if not path: return True
    try:
        p = normalize(path)
        path_str = str(p)
        
        for root in _SYSTEM_ROOT_PATHS:
            if os.path.commonpath([path_str, root]) == root:
                return True
            
        if any(part.lower() in PROTECTED_DIR_NAMES for part in p.parts):
            return True
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError, RuntimeError): 
        return True


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Valida si una ruta es descendiente jerárquica de otra."""
    if child is None or parent is None: return False
    try:
        c_path = normalize(child)
        p_path = normalize(parent)
        
        if is_drive_root(c_path) or is_protected_path(c_path):
            return False
            
        common = os.path.commonpath([str(c_path), str(p_path)])
        return common == str(p_path) if allow_equal else (c_path != p_path and common == str(p_path))
    except (ValueError, TypeError, OSError, RuntimeError): return False


@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Wrapper para verificar si el archivo posee una extensión crítica."""
    if not path: return True
    try:
        return _is_sensitive_extension(Path(str(path)))
    except (TypeError, ValueError, OSError): return True 


def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """Realiza validaciones estructurales de la ruta antes de tocar el disco."""
    if "\0" in path_string:
        raise UnsafePathError("Inyección de carácter nulo detectada.", SafetyValidationErrorCode.NULL_CHAR)
    if _has_invalid_chars(path_string):
        raise UnsafePathError("La ruta contiene caracteres inválidos.", SafetyValidationErrorCode.INVALID_CHARS)
    
    for part in target_path.parts:
        if not part or part.strip() != part:
            raise UnsafePathError("Componente de ruta inválido.", SafetyValidationErrorCode.INVALID_CHARS)
        if _is_reserved_device_name(part):
            raise UnsafePathError(f"Nombre '{part}' reservado.", SafetyValidationErrorCode.RESERVED_NAME)

    if path_string.startswith(("\\\\", "//")):
        raise UnsafePathError("Operación en rutas de red (UNC) bloqueada.", SafetyValidationErrorCode.UNC_PATH)
    if len(str(target_path)) >= MAX_PATH_LENGTH:
        raise UnsafePathError("Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG)


def _validate_boundary_conditions(target_path: Path, root_directory: PathLike | None) -> None:
    """Valida si la ruta está dentro de los límites operativos permitidos."""
    if root_directory and not is_within_directory(target_path, root_directory, allow_equal=True):
        raise UnsafePathError("Fuera de alcance permitido.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    if is_within_directory(target_path, Path.cwd(), allow_equal=True):
        raise UnsafePathError("No se permite modificar archivos en la app raíz.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    if is_drive_root(target_path):
        raise UnsafePathError("Intento de acceso a la raíz de unidad.", SafetyValidationErrorCode.ROOT_ACCESS)
    if is_protected_path(target_path):
        raise UnsafePathError("Ruta en directorio protegido.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
    if _is_reparse_point(target_path):
        raise UnsafePathError("Nodo de reparse detectado.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: PathLike | None = None) -> Path:
    """Valida una ruta para modificación: lanza UnsafePathError si es insegura."""
    if path is None: raise UnsafePathError("Ruta nula recibida.")
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise UnsafePathError("Ruta inválida o vacía.")

    try:
        p = normalize(path)
    except ValueError as e:
        raise UnsafePathError(f"Ruta inválida: {e}")
    
    if not allow_sensitive and _is_sensitive_extension(p):
        raise UnsafePathError(f"Extensión sensible '{p.suffix}'.", SafetyValidationErrorCode.SENSITIVE_EXTENSION)

    _validate_structural_safety(p, str(p))
    _validate_boundary_conditions(p, base_dir)
    
    if p.exists():
        if not (p.is_file() or p.is_dir()):
            raise UnsafePathError("Objeto no soportado.")
        _check_file_integrity(p)
    elif p.parent and is_protected_path(p.parent):
        raise UnsafePathError("Directorio contenedor protegido.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
            
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> TypeGuard[PathLike]:
    """Retorna True si la ruta es segura, de lo contrario False."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError): return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una lista de rutas, manteniendo solo las seguras."""
    results = []
    for p in paths:
        if p is not None:
            try:
                if is_safe_to_modify(p, allow_sensitive=allow_sensitive):
                    results.append(normalize(p))
            except (ValueError, TypeError, OSError):
                continue
    return results


def describe_protection(path: PathLike) -> str:
    """Genera una explicación textual sobre por qué una ruta no superó las validaciones."""
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        raw_str = str(path)
    except (TypeError, ValueError): return "Ruta mal formada."

    if raw_str.startswith(("\\\\", "//")): return f"'{raw_str}' es ruta de red."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(p): return f"'{p}' protegida por sistema."
    try:
        if p.exists():
            if len(str(p)) >= MAX_PATH_LENGTH: return f"'{p}' longitud excesiva."
            if not os.access(p, os.W_OK): return f"'{p}' sin permisos de escritura."
            if os.path.islink(p): return f"'{p}' es un enlace simbólico."
            if os.path.ismount(p): return f"'{p}' es un punto de montaje."
            if _is_readonly(p): return f"'{p}' es solo lectura."
            if _is_file_in_use(str(p)): return f"'{p}' en uso."
            if _is_system_or_hidden(p): return f"'{p}' atributo oculto/sistema/offline."
            if _has_alternate_data_stream(p.name): return f"'{p}' contiene ADS."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío."
            if p.is_file() and p.stat().st_size > MAX_FILE_SIZE: return f"'{p}' tamaño excesivo."
    except OSError:
        pass
    if _is_sensitive_extension(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
