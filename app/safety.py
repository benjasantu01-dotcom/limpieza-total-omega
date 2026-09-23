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
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, Optional, TypeGuard, TypedDict
from functools import lru_cache
import unicodedata

PathLike: TypeAlias = Union[str, os.PathLike]
ViolationPredicate: TypeAlias = Callable[[Path, os.stat_result], bool]

class FileMetadata(TypedDict):
    """Representación de los atributos de archivo necesarios para evaluaciones de seguridad."""
    is_reparse: bool
    is_system_hidden: bool
    is_readonly: bool
    is_in_use: bool

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

# Constantes de atributos de archivo Win32 obtenidas de WinBase.h
FILE_ATTRIBUTE_HIDDEN: Final[int] = 0x02
FILE_ATTRIBUTE_SYSTEM: Final[int] = 0x04
FILE_ATTRIBUTE_TEMPORARY: Final[int] = 0x100
FILE_ATTRIBUTE_OFFLINE: Final[int] = 0x1000
FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x400
FILE_ATTRIBUTE_DIRECTORY: Final[int] = 0x10
FILE_ATTRIBUTE_COMPRESSED: Final[int] = 0x800
FILE_ATTRIBUTE_ENCRYPTED: Final[int] = 0x4000
FILE_ATTRIBUTE_SPARSE_FILE: Final[int] = 0x200
MAX_PATH_LENGTH: Final[int] = 260
MAX_FILENAME_LENGTH: Final[int] = 255
MAX_FILE_SIZE: Final[int] = 2 * 1024 * 1024 * 1024  # 2GB límite de seguridad arbitrario

# Constantes Win32 Drive Types (GetDriveType)
DRIVE_UNKNOWN: Final[int] = 0
DRIVE_NO_ROOT_DIR: Final[int] = 1
DRIVE_REMOVABLE: Final[int] = 2
DRIVE_FIXED: Final[int] = 3
DRIVE_REMOTE: Final[int] = 4
DRIVE_CDROM: Final[int] = 5
DRIVE_RAMDISK: Final[int] = 6

def _to_long_path(path_str: str) -> str:
    """Normaliza rutas para la API de Windows mediante el prefijo \\?\\ para superar límites de MAX_PATH."""
    if os.name == 'nt' and not path_str.startswith("\\\\?\\"):
        if path_str.startswith("\\\\"): return "\\\\?\\UNC" + path_str[1:]
        return "\\\\?\\" + path_str
    return path_str

@lru_cache(maxsize=1024)
def _get_file_attrs(path_str: str) -> int:
    """Consulta centralizada de atributos Win32 para reducir syscalls."""
    if os.name != 'nt': return 0
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(path_str))
        return attrs if attrs != 0xFFFFFFFF else 0
    except (AttributeError, OSError, ctypes.ArgumentError):
        return 0

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
    HARD_LINK_DETECTED = 12
    ACCESS_DENIED = 13
    IO_ERROR = 14
    RELATIVE_PATH_NOT_ALLOWED = 15
    SUSPICIOUS_ENCODING = 16
    ADS_DETECTED = 17
    OFFLINE_FILE = 18
    ENCRYPTED_OR_COMPRESSED = 19
    VOLUME_READ_ONLY = 20
    REMOTE_DRIVE_DETECTED = 21
    REMOVABLE_DRIVE_DETECTED = 22
    KERNEL_LOCKED_FILE = 23
    WRITE_ACCESS_DENIED = 24
    MOUNT_POINT_DETECTED = 25
    TOCTOU_VIOLATION = 26
    SPARSE_FILE_DETECTED = 27
    DEVICE_FILE_DETECTED = 28

class UnsafePathError(Exception):
    """Lanzada cuando una operación intenta manipular rutas protegidas."""
    def __init__(self, message: str, code: SafetyValidationErrorCode = SafetyValidationErrorCode.GENERIC):
        super().__init__(f"[{code.name}] {message}")
        self.code = code

class ProtectionReason(Enum):
    """Categorías de riesgo detectadas durante la validación de integridad."""
    INACCESSIBLE = "inaccesible"
    REPARSE_POINT = "punto de reparse"
    READ_ONLY = "solo lectura"
    IN_USE = "en uso"
    SYSTEM_HIDDEN = "sistema/oculto/offline/temporal"
    HARD_LINK = "hard link detectado"
    SYMLINK = "enlace simbólico detectado"
    ADS = "ADS (flujos alternativos)"
    EMPTY_FILE = "archivo vacío"
    EXCESSIVE_DEPTH = "profundidad excesiva"
    MOUNT_POINT = "punto de montaje detectado"
    EXCESSIVE_SIZE = "tamaño de archivo excedido"
    INVALID_TYPE = "tipo de archivo no soportado"
    OFFLINE = "archivo offline (nube)"
    ENCRYPTED_OR_COMPRESSED = "cifrado o comprimido"
    VOLUME_READ_ONLY = "volumen de solo lectura"
    REMOTE_DRIVE = "unidad de red detectada"
    REMOVABLE_DRIVE = "unidad extraíble detectada"
    KERNEL_LOCKED = "archivo bloqueado por kernel"
    ACCESS_WRITE = "acceso de escritura denegado"
    TOCTOU_VIOLATION = "violación de consistencia (TOCTOU)"
    SPARSE_FILE = "archivo disperso (sparse file)"
    DEVICE_FILE = "archivo de dispositivo detectado"

class ValidationContext(Enum):
    """Define si la validación es estructural o requiere acceso a disco."""
    STRUCTURAL = auto()
    INTEGRITY = auto()

# Directorios críticos del sistema
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

_SYSTEM_ROOT_PATHS_SET: Final[frozenset[str]] = frozenset(p.lower() for p in _SYSTEM_ROOT_PATHS)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)

class _IntegrityCheck(NamedTuple):
    """Regla de seguridad que vincula un motivo de protección a un predicado evaluable."""
    reason: ProtectionReason
    predicate: ViolationPredicate

class _CheckResult(NamedTuple):
    """Resultado del chequeo de integridad."""
    is_safe: bool
    reason: Optional[ProtectionReason] = None

@lru_cache(maxsize=1)
def is_running_as_admin() -> bool:
    """Verifica privilegios elevados: Windows (IsUserAnAdmin) o POSIX (EUID 0)."""
    if os.name != 'nt':
        try: return os.geteuid() == 0
        except (AttributeError, OSError): return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False

def _has_invalid_chars(path_str: Optional[str]) -> bool:
    """Detecta caracteres prohibidos en rutas Windows, secuencias de control RTL o nulos."""
    if not isinstance(path_str, str) or not path_str: return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E\u206A-\u206F]|[\x00-\x1f\x7f]', path_str))

@lru_cache(maxsize=128)
def _is_reserved_device_name(name: str) -> bool:
    """Valida contra nombres reservados de dispositivos legados (CON, NUL) que no pueden ser archivos."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))

def _is_device_file(path: Path) -> bool:
    """Detecta rutas de dispositivos de Windows (e.g., \\.\PhysicalDrive0) no aptas para archivos."""
    path_str = str(path).upper()
    return path_str.startswith(("\\\\.\\", "//./"))

@lru_cache(maxsize=512)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta la presencia de NTFS ADS (ej. 'archivo.txt:stream') usado para ocultar payloads."""
    return ":" in path_name and len(path_name.split(":")) > 2

def _is_system_or_hidden(path_str: str) -> bool:
    """Consulta atributos Win32 para identificar archivos protegidos por el SO."""
    if not os.path.isabs(path_str): return False
    attrs = _get_file_attrs(path_str)
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_TEMPORARY))

def _is_reparse_point(path_str: str) -> bool:
    """Consulta Win32 para identificar Junctions o Symlinks que pueden causar bucles infinitos."""
    if os.name != 'nt': return os.path.islink(path_str)
    return bool(_get_file_attrs(path_str) & FILE_ATTRIBUTE_REPARSE_POINT)

def _is_encrypted_or_compressed_or_sparse(path_str: str) -> bool:
    """Consulta atributos Win32 para detectar NTFS compresión, cifrado o archivos dispersos."""
    if os.name != 'nt': return False
    return bool(_get_file_attrs(path_str) & (FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_ENCRYPTED | FILE_ATTRIBUTE_SPARSE_FILE))

def _is_offline(path_str: str) -> bool:
    """Detecta si un archivo es gestionado por proveedores en la nube."""
    if os.name != 'nt': return False
    return bool(_get_file_attrs(path_str) & FILE_ATTRIBUTE_OFFLINE)

@lru_cache(maxsize=1024)
def _is_file_in_use(path_str: str) -> bool:
    """Verifica bloqueos mediante intento de apertura con acceso de solo lectura y compartido."""
    if os.name != 'nt' or not isinstance(path_str, str) or not os.path.isabs(path_str):
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(_to_long_path(path_str), 0x80000000, 0x00000007, None, 3, 0x00000080, None)
        if handle == -1: 
            return True
        kernel32.CloseHandle(handle)
    except (OSError, PermissionError, AttributeError, ctypes.ArgumentError, Exception):
        return True
    return False

@lru_cache(maxsize=128)
def _is_volume_readonly(path_str: str) -> bool:
    """Consulta el flag de solo lectura del volumen montado mediante GetVolumeInformationW."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        flags = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0):
            return bool(flags.value & 0x80000)
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
        pass
    return False

def _is_directory_junction(path_str: str) -> bool:
    """Especialización para detectar directorios que son puntos de unión de NTFS."""
    attrs = _get_file_attrs(path_str)
    return bool(attrs & FILE_ATTRIBUTE_DIRECTORY and attrs & FILE_ATTRIBUTE_REPARSE_POINT)

def _is_kernel_managed(path: Path) -> bool:
    """Previene manipulación de archivos esenciales que el kernel mantiene bloqueados."""
    return path.name.lower() in ("pagefile.sys", "hiberfil.sys", "swapfile.sys")

@lru_cache(maxsize=1024)
def _is_sensitive_extension(ext: str) -> bool:
    """Valida si la extensión es crítica del sistema."""
    return ext.lower() in SENSITIVE_EXTENSIONS

# Lista de validadores de integridad aplicada secuencialmente
_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _IntegrityCheck(ProtectionReason.SYMLINK, lambda p, _: p.is_symlink()),
    _IntegrityCheck(ProtectionReason.REPARSE_POINT, lambda p, _: _is_reparse_point(str(p))),
    _IntegrityCheck(ProtectionReason.KERNEL_LOCKED, lambda p, _: _is_kernel_managed(p)),
    _IntegrityCheck(ProtectionReason.READ_ONLY, lambda _, st: not bool(st.st_mode & stat.S_IWRITE)),
    _IntegrityCheck(ProtectionReason.VOLUME_READ_ONLY, lambda p, _: _is_volume_readonly(str(p))),
    _IntegrityCheck(ProtectionReason.IN_USE, lambda p, _: _is_file_in_use(str(p))),
    _IntegrityCheck(ProtectionReason.SYSTEM_HIDDEN, lambda p, _: _is_system_or_hidden(str(p))),
    _IntegrityCheck(ProtectionReason.OFFLINE, lambda p, _: _is_offline(str(p))),
    _IntegrityCheck(ProtectionReason.ENCRYPTED_OR_COMPRESSED, lambda p, _: _is_encrypted_or_compressed_or_sparse(str(p))),
    _IntegrityCheck(ProtectionReason.SPARSE_FILE, lambda p, _: _is_encrypted_or_compressed_or_sparse(str(p))),
    _IntegrityCheck(ProtectionReason.HARD_LINK, lambda p, st: p.is_file() and st.st_nlink > 1),
    _IntegrityCheck(ProtectionReason.ADS, lambda p, _: _has_alternate_data_stream(p.name)),
    _IntegrityCheck(ProtectionReason.EMPTY_FILE, lambda p, st: p.is_file() and st.st_size == 0),
    _IntegrityCheck(ProtectionReason.EXCESSIVE_SIZE, lambda p, st: p.is_file() and st.st_size > MAX_FILE_SIZE),
    _IntegrityCheck(ProtectionReason.MOUNT_POINT, lambda p, _: os.path.ismount(p)),
    _IntegrityCheck(ProtectionReason.INVALID_TYPE, lambda _, st: not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))),
]

_REASON_TO_CODE: Final[dict[ProtectionReason, SafetyValidationErrorCode]] = {
    ProtectionReason.SYMLINK: SafetyValidationErrorCode.REPARSE_POINT_DETECTED,
    ProtectionReason.HARD_LINK: SafetyValidationErrorCode.HARD_LINK_DETECTED,
    ProtectionReason.OFFLINE: SafetyValidationErrorCode.OFFLINE_FILE,
    ProtectionReason.ENCRYPTED_OR_COMPRESSED: SafetyValidationErrorCode.ENCRYPTED_OR_COMPRESSED,
    ProtectionReason.IN_USE: SafetyValidationErrorCode.FILE_IN_USE,
    ProtectionReason.REPARSE_POINT: SafetyValidationErrorCode.REPARSE_POINT_DETECTED,
    ProtectionReason.ADS: SafetyValidationErrorCode.ADS_DETECTED,
    ProtectionReason.VOLUME_READ_ONLY: SafetyValidationErrorCode.VOLUME_READ_ONLY,
    ProtectionReason.REMOTE_DRIVE: SafetyValidationErrorCode.REMOTE_DRIVE_DETECTED,
    ProtectionReason.REMOVABLE_DRIVE: SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED,
    ProtectionReason.KERNEL_LOCKED: SafetyValidationErrorCode.KERNEL_LOCKED_FILE,
    ProtectionReason.ACCESS_WRITE: SafetyValidationErrorCode.WRITE_ACCESS_DENIED,
    ProtectionReason.MOUNT_POINT: SafetyValidationErrorCode.MOUNT_POINT_DETECTED,
    ProtectionReason.SPARSE_FILE: SafetyValidationErrorCode.SPARSE_FILE_DETECTED
}

def _evaluate_security_rules(path: Path, current_stat: os.stat_result) -> None:
    """Ejecuta los predicados de seguridad sobre el archivo y lanza UnsafePathError si falla."""
    for rule in _VALIDATORS:
        if rule.predicate(path, current_stat):
            code = _REASON_TO_CODE.get(rule.reason, SafetyValidationErrorCode.GENERIC)
            raise UnsafePathError(f"Integridad comprometida: {rule.reason.value}", code)

def _check_file_integrity(path: Path, initial_stat: os.stat_result) -> None:
    """Verifica metadatos en disco y compara con estado inicial para prevenir ataques TOCTOU."""
    if not os.access(path, os.R_OK):
        raise UnsafePathError(f"Acceso de lectura denegado a {path.name}", SafetyValidationErrorCode.ACCESS_DENIED)
    try:
        current_stat = path.stat()
    except (PermissionError, OSError, FileNotFoundError) as e:
        raise UnsafePathError(f"Acceso denegado o archivo perdido ({e}): {path.name}", SafetyValidationErrorCode.IO_ERROR)
    
    if getattr(current_stat, 'st_dev', 0) != initial_stat.st_dev or getattr(current_stat, 'st_ino', 0) != initial_stat.st_ino:
        raise UnsafePathError(f"Consistencia fallida (TOCTOU): {path.name}", SafetyValidationErrorCode.TOCTOU_VIOLATION)
    
    if _is_directory_junction(str(path)):
        raise UnsafePathError(f"Junction detectada: {path.name}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        
    _evaluate_security_rules(path, current_stat)

@lru_cache(maxsize=2048)
def _is_readonly(path_str: str) -> bool:
    """Verifica estado de escritura del sistema de archivos mediante permisos básicos."""
    try:
        path = Path(path_str)
        if not path.exists(): return True
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError, FileNotFoundError):
        return True

def _validate_access_permissions(path: Path) -> None:
    """Verifica si el proceso actual tiene permisos de lectura/escritura sobre el recurso."""
    if not path.exists(): return
    try:
        if not os.access(path, os.R_OK):
            raise UnsafePathError("Permisos de lectura denegados.", SafetyValidationErrorCode.ACCESS_DENIED)
        if not os.access(path, os.W_OK):
            raise UnsafePathError("Permisos de escritura denegados.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
    except (OSError, PermissionError):
        raise UnsafePathError("Acceso al sistema de archivos denegado.", SafetyValidationErrorCode.IO_ERROR)

@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Estandariza ruta, resuelve relativos y normaliza Unicode (NFKC)."""
    if path is None: raise ValueError("Ruta nula recibida.")
    path_str = str(path).strip()
    if not path_str: raise ValueError("Entrada de ruta vacía.")
    if unicodedata.normalize('NFKC', path_str) != path_str:
         raise ValueError("Ruta contiene secuencias Unicode sospechosas.")
    try:
        p = Path(path_str)
        if ".." in p.parts: raise ValueError("Path traversal detectado.")
        if p.exists(): return p.resolve()
        return Path(os.path.abspath(path_str))
    except (OSError, RuntimeError, TypeError, PermissionError) as e:
        raise ValueError(f"Error irrecuperable al normalizar {path_str}: {e}")

def is_absolute_path_allowed(path: PathLike) -> bool:
    """Verifica que la ruta sea absoluta."""
    try: return Path(path).is_absolute()
    except (TypeError, ValueError): return False

def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta es la raíz del volumen (ej. C:\\)."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError): return True

@lru_cache(maxsize=4096)
def _is_system_path_cached(path_str: str) -> bool:
    """Verifica si la ruta está dentro de directorios de sistema."""
    path_norm = os.path.normpath(path_str).lower()
    if any(path_norm.startswith(root) for root in _SYSTEM_ROOT_PATHS_SET): return True
    return not PROTECTED_DIR_NAMES.isdisjoint(Path(path_norm).parts)

@lru_cache(maxsize=4096)
def is_protected_path(path: PathLike) -> TypeGuard[str]:
    """Valida si la ruta pertenece a directorios protegidos o es raíz del sistema."""
    if not path: return True
    try:
        p = normalize(path)
        return _is_system_path_cached(str(p)) or p == Path(p.anchor)
    except (ValueError, TypeError, OSError, RuntimeError): return True

@lru_cache(maxsize=4096)
def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica si 'child' reside debajo de 'parent'."""
    if child is None or parent is None: return False
    try:
        c_path = normalize(child)
        p_path = normalize(parent)
        if is_drive_root(c_path) or is_protected_path(c_path): return False
        return os.path.commonpath([c_path, p_path]) == str(p_path) if allow_equal else os.path.commonpath([c_path, p_path]) == str(p_path) and c_path != p_path
    except (ValueError, TypeError, OSError, RuntimeError): return False

@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Verifica si la extensión del archivo está bloqueada."""
    if not path: return True
    try: 
        p_str = str(path)
        return _is_sensitive_extension(os.path.splitext(p_str)[1])
    except (TypeError, ValueError, OSError): return True 

def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """Realiza una validación puramente estructural de la ruta."""
    if not isinstance(path_string, str):
        raise UnsafePathError("Ruta no es texto.", SafetyValidationErrorCode.GENERIC)
    if ".." in path_string.split(os.sep):
        raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    if len(path_string) > MAX_PATH_LENGTH:
        raise UnsafePathError("Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG)
    if "\0" in path_string:
        raise UnsafePathError("Inyección de carácter nulo.", SafetyValidationErrorCode.NULL_CHAR)
    if _has_invalid_chars(path_string):
        raise UnsafePathError("Caracteres inválidos detectados.", SafetyValidationErrorCode.INVALID_CHARS)
    if unicodedata.normalize('NFKC', path_string) != path_string:
        raise UnsafePathError("Codificación de caracteres sospechosa.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING)
    if _has_alternate_data_stream(path_string):
        raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED)
    if _is_device_file(target_path):
        raise UnsafePathError("Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
    # Verificación preventiva de reparse point antes de acceder al disco
    if os.name == 'nt' and (_get_file_attrs(path_string) & FILE_ATTRIBUTE_REPARSE_POINT):
        raise UnsafePathError("Punto de reparse detectado estructuralmente.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
    try:
        if target_path.exists() and not target_path.is_absolute():
            raise UnsafePathError("Ruta inconsistente con el sistema.", SafetyValidationErrorCode.GENERIC)
        if not target_path.parts or (len(target_path.parts) == 1 and target_path.parts[0] == os.sep):
             raise UnsafePathError("Ruta raíz no permitida.", SafetyValidationErrorCode.ROOT_ACCESS)
        for part in target_path.parts:
            if len(part) > MAX_FILENAME_LENGTH:
                raise UnsafePathError(f"Nombre de componente demasiado largo: {part[:10]}...", SafetyValidationErrorCode.PATH_TOO_LONG)
            if not part or part.strip() != part:
                raise UnsafePathError(f"Componente '{part}' con espacios envolventes.", SafetyValidationErrorCode.INVALID_CHARS)
            if part.endswith(('.', ' ')):
                raise UnsafePathError(f"Componente '{part}' termina en caracter inválido.", SafetyValidationErrorCode.INVALID_CHARS)
            if "  " in part:
                 raise UnsafePathError(f"Componente '{part}' con espacios excesivos.", SafetyValidationErrorCode.INVALID_CHARS)
            part_cleaned = part.split('.')[0]
            if _is_reserved_device_name(part_cleaned):
                raise UnsafePathError(f"Nombre reservado '{part}'.", SafetyValidationErrorCode.RESERVED_NAME)
    except (AttributeError, TypeError, ValueError):
        raise UnsafePathError("Estructura de ruta inválida.", SafetyValidationErrorCode.GENERIC)
    if path_string.startswith(("\\\\", "//")):
        raise UnsafePathError("Rutas UNC bloqueadas.", SafetyValidationErrorCode.UNC_PATH)

def _validate_boundary_conditions(target_path: Path, root_directory: Optional[PathLike]) -> None:
    """Verifica límites de alcance y restricciones de volumen."""
    if not is_absolute_path_allowed(target_path):
        raise UnsafePathError("Solo se permiten rutas absolutas.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED)
    if root_directory and not is_within_directory(target_path, root_directory, allow_equal=True):
        raise UnsafePathError("Fuera de alcance permitido.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    if is_protected_path(target_path):
        raise UnsafePathError("Ruta en directorio del sistema bloqueada.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
    # Verificación adicional de nombres de dispositivo en cualquier parte de la ruta
    for part in target_path.parts:
        if _is_reserved_device_name(part.split('.')[0]):
            raise UnsafePathError(f"Acceso a dispositivo reservado '{part}' bloqueado.", SafetyValidationErrorCode.RESERVED_NAME)
    if os.name == 'nt':
        try:
            root = target_path.anchor
            if root:
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
                if drive_type == DRIVE_NO_ROOT_DIR:
                     raise UnsafePathError("Unidad inaccesible.", SafetyValidationErrorCode.IO_ERROR)
                if drive_type == DRIVE_REMOTE:
                     raise UnsafePathError("Unidad de red bloqueada.", SafetyValidationErrorCode.REMOTE_DRIVE_DETECTED)
                if drive_type == DRIVE_REMOVABLE:
                     raise UnsafePathError("Unidad extraíble bloqueada.", SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED)
                if drive_type == DRIVE_CDROM or _is_volume_readonly(str(target_path)):
                     raise UnsafePathError("Volumen de solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY)
        except (OSError, AttributeError, ctypes.ArgumentError) as e:
             raise UnsafePathError(f"Fallo al consultar unidad: {e}", SafetyValidationErrorCode.IO_ERROR)
    try:
        app_root = Path(os.getcwd()).resolve()
        if target_path == app_root or app_root in target_path.parents:
            raise UnsafePathError("Modificación de App denegada.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    except (OSError, RuntimeError, ValueError): pass
    if is_drive_root(target_path):
        raise UnsafePathError("Acceso a raíz denegado.", SafetyValidationErrorCode.ROOT_ACCESS)
    if not target_path.exists() and is_protected_path(target_path.parent):
        raise UnsafePathError("Creación en directorio restringido.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)

def _get_final_path_normalized(path: Path) -> Optional[Path]:
    """Resuelve la ruta física real en disco mediante Win32 Handles."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW(_to_long_path(str(path)), 0, 0, None, 3, 0x02000000, None)
    if handle == -1: return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.GetFinalPathNameByHandleW(handle, buf, 1024, 0) > 0:
            return Path(buf.value).resolve()
    finally: kernel32.CloseHandle(handle)
    return None

def _validate_ntfs_reparse_redirection(path: Path) -> None:
    """Asegura que la ruta no sea un proxy hacia otro volumen o unidad."""
    if not path.exists(): return
    if _is_reparse_point(str(path.parent)):
        raise UnsafePathError("Directorio padre es un punto de reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
    final_path = _get_final_path_normalized(path)
    if final_path:
        if final_path.drive != path.resolve().drive:
            raise UnsafePathError("Redirección de unidad detectada.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        if not str(final_path).startswith(str(path.parent)):
            raise UnsafePathError("Salida de carpeta permitida vía redirección.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)

def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> Path:
    """
    Valida integridad y seguridad de una ruta.
    """
    if path is None: raise UnsafePathError("Ruta nula.", SafetyValidationErrorCode.GENERIC)
    try: p = normalize(path)
    except (ValueError, TypeError, PermissionError, OSError) as e: 
        raise UnsafePathError(f"Ruta no normalizable: {e}", SafetyValidationErrorCode.GENERIC)
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Extensión bloqueada '{p.suffix}'.", SafetyValidationErrorCode.SENSITIVE_EXTENSION)
    _validate_structural_safety(p, str(p))
    _validate_boundary_conditions(p, base_dir)
    
    if p.exists():
        _validate_access_permissions(p)
        try: initial_stat = p.stat()
        except (OSError, PermissionError) as e: raise UnsafePathError(f"No se pueden obtener metadatos: {e}", SafetyValidationErrorCode.IO_ERROR)
        if not bool(initial_stat.st_mode & stat.S_IWRITE):
            raise UnsafePathError(f"Acceso de escritura denegado: {p.name}", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
        if os.name == 'nt': 
            _validate_ntfs_reparse_redirection(p)
            try:
                if not os.access(p.parent, os.W_OK):
                     raise UnsafePathError("Directorio contenedor marcado como solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY)
            except (OSError, PermissionError): raise UnsafePathError("Directorio contenedor inaccesible.", SafetyValidationErrorCode.IO_ERROR)
        try: _check_file_integrity(p, initial_stat)
        except (OSError, PermissionError) as e: raise UnsafePathError(f"Error durante validación: {e}", SafetyValidationErrorCode.IO_ERROR)
    else:
        parent = p.parent
        try:
            if parent.exists() and not os.access(parent, os.W_OK):
                 raise UnsafePathError("Directorio contenedor no tiene permisos de escritura.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
        except (OSError, PermissionError): raise UnsafePathError("Directorio contenedor inaccesible.", SafetyValidationErrorCode.IO_ERROR)
        if parent.exists() and is_protected_path(parent):
            raise UnsafePathError("Creación en directorio restringido.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
    return p

def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """
    Wrapper booleano para validar seguridad sin lanzar excepciones.
    """
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, ValueError, TypeError, OSError, PermissionError): return False

def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """
    Filtra una colección de rutas, devolviendo solo aquellas consideradas seguras.
    """
    results = []
    for p in paths:
        if p is None: continue
        try: results.append(ensure_safe_to_modify(p, allow_sensitive=allow_sensitive))
        except (UnsafePathError, ValueError, TypeError, OSError, PermissionError): continue
    return results

def describe_protection(path: PathLike) -> str:
    """
    Provee un diagnóstico humano legible de por qué una ruta fue marcada como insegura.
    """
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        raw_str = str(path)
    except (TypeError, ValueError): return "Ruta mal formada."
    if raw_str.startswith(("\\\\", "//")): return f"'{raw_str}' es ruta de red."
    if _is_device_file(p): return f"'{raw_str}' es un archivo de dispositivo."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(p): return f"'{p}' protegida por sistema."
    try:
        if p.exists():
            if p.is_symlink(): return f"'{p}' es un enlace simbólico."
            if _is_reparse_point(str(p)): return f"'{p}' es un punto de reparse (Junction/Symlink)."
            if os.path.ismount(p): return f"'{p}' es un punto de montaje."
            if _is_readonly(str(p)): return f"'{p}' es solo lectura."
            if _is_volume_readonly(str(p)): return f"'{p}' pertenece a un volumen de solo lectura."
            if _is_file_in_use(str(p)): return f"'{p}' en uso."
            if _is_encrypted_or_compressed_or_sparse(str(p)): return f"'{p}' archivo cifrado, comprimido o disperso."
            if _is_offline(str(p)): return f"'{p}' archivo offline/nube."
            if _is_system_or_hidden(str(p)): return f"'{p}' atributo oculto/sistema/temporal."
            if _has_alternate_data_stream(p.name): return f"'{p}' contiene ADS."
            if not (p.is_file() or p.is_dir()): return f"'{p}' tipo de objeto no soportado."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío."
            if p.is_file() and p.stat().st_size > MAX_FILE_SIZE: return f"'{p}' tamaño excesivo."
            if p.is_file() and p.stat().st_nlink > 1: return f"'{p}' detectado como hard link."
    except (OSError, FileNotFoundError, AttributeError): pass
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
