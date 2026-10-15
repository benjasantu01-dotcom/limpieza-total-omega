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
import errno
from enum import Enum, auto, IntEnum
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, Optional, TypedDict
from functools import lru_cache
import unicodedata

# Constantes de error de Win32 API
ERROR_SHARING_VIOLATION: Final[int] = 32
ERROR_FILE_NOT_FOUND: Final[int] = 2
ERROR_ACCESS_DENIED: Final[int] = 5
ERROR_INSUFFICIENT_BUFFER: Final[int] = 122
ERROR_INVALID_NAME: Final[int] = 123
INVALID_FILE_ATTRIBUTES: Final[int] = 0xFFFFFFFF
LONG_PATH_PREFIX: Final[str] = "\\\\?\\"

PathLike: TypeAlias = Union[str, os.PathLike]
ViolationPredicate: TypeAlias = Callable[[Path, os.stat_result, "SecurityDescriptor"], bool]

class SafetyAction(Enum):
    """Define la criticidad de la operación para ajustar el rigor de la validación."""
    READ = auto()
    MODIFY = auto()

class SecurityDescriptor(NamedTuple):
    """Contenedor de metadatos de seguridad extraídos vía Win32 API."""
    attrs: int
    is_protected_system: bool
    is_in_use: bool
    is_readonly: bool
    is_reparse: bool
    
    def has_flag(self, flag: Win32Attr) -> bool:
        """Verifica si un atributo específico está presente en el descriptor."""
        return bool(self.attrs & flag)

    @classmethod
    def from_path(cls, path_str: str) -> SecurityDescriptor:
        """Fabrica un descriptor a partir de una ruta mediante llamadas de sistema."""
        if not path_str:
            return cls(0, True, True, True, True)
        try:
            attrs = _get_file_attrs(path_str)
            is_protected = bool(attrs & (Win32Attr.HIDDEN | Win32Attr.SYSTEM | Win32Attr.OFFLINE | Win32Attr.TEMPORARY | Win32Attr.REPARSE_POINT))
            is_in_use = is_protected or (attrs != INVALID_FILE_ATTRIBUTES and _is_file_locked_by_other_process(path_str))
            
            return cls(
                attrs=attrs,
                is_protected_system=is_protected,
                is_in_use=is_in_use,
                is_readonly=bool(attrs & Win32Attr.READONLY),
                is_reparse=bool(attrs & Win32Attr.REPARSE_POINT)
            )
        except Exception:
            return cls(0, True, True, True, True)

class FileMetadata(TypedDict):
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

class Win32Attr(IntEnum):
    """Mapeo de constantes de atributos de archivos Win32."""
    READONLY: int = 0x01
    HIDDEN: int = 0x02
    SYSTEM: int = 0x04
    DIRECTORY: int = 0x10
    TEMPORARY: int = 0x100
    SPARSE_FILE: int = 0x200
    REPARSE_POINT: int = 0x400
    COMPRESSED: int = 0x800
    OFFLINE: int = 0x1000
    ENCRYPTED: int = 0x4000

MAX_PATH_LENGTH: Final[int] = 260
MAX_FILENAME_LENGTH: Final[int] = 255
MAX_FILE_SIZE: Final[int] = 2 * 1024 * 1024 * 1024
MAX_DIRECTORY_DEPTH: Final[int] = 32

DRIVE_UNKNOWN: Final[int] = 0
DRIVE_NO_ROOT_DIR: Final[int] = 1
DRIVE_REMOVABLE: Final[int] = 2 
DRIVE_FIXED: Final[int] = 3     
DRIVE_REMOTE: Final[int] = 4    
DRIVE_CDROM: Final[int] = 5     
DRIVE_RAMDISK: Final[int] = 6   

@lru_cache(maxsize=4096)
def _to_long_path(path_str: str) -> str:
    """Aplica el prefijo \\\\?\\ para evitar limitaciones de longitud de ruta en Windows."""
    if not isinstance(path_str, str) or not path_str:
        return ""
    if os.name == 'nt':
        if path_str.startswith(LONG_PATH_PREFIX):
            return path_str
        if path_str.startswith("\\\\"):
            return "\\\\?\\UNC" + path_str[1:]
        try:
            abs_path = os.path.abspath(path_str)
            return LONG_PATH_PREFIX + abs_path
        except (OSError, ValueError):
            return path_str
    return path_str

def _is_path_too_long(path_str: str) -> bool:
    return len(path_str) > MAX_PATH_LENGTH

@lru_cache(maxsize=4096)
def _get_file_attrs(path_str: Optional[str]) -> int:
    """Consulta atributos crudos de sistema sin lanzar excepciones."""
    if not isinstance(path_str, str) or not path_str or not os.path.isabs(path_str): 
        return 0
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(path_str))
        if attrs == INVALID_FILE_ATTRIBUTES:
            err = ctypes.windll.kernel32.GetLastError()
            if err == ERROR_ACCESS_DENIED:
                return Win32Attr.SYSTEM
            return 0
        return attrs
    except (AttributeError, ctypes.ArgumentError, OSError, MemoryError):
        return 0

class SafetyValidationErrorCode(IntEnum):
    """Códigos de error detallados para auditoría de fallos en la validación."""
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
    EMPTY_FILE = 29
    VOLUME_RESTRICTED = 30
    PATH_TOO_DEEP = 31
    SYSTEM_OWNER_PROTECTION = 32
    VIRTUAL_DRIVE_DETECTED = 33
    BIDI_SPOOFING = 34

class UnsafePathError(Exception):
    """Excepción específica para cualquier violación de las políticas de seguridad."""
    def __init__(self, message: str, code: SafetyValidationErrorCode = SafetyValidationErrorCode.GENERIC):
        super().__init__(f"[{code.name}] {message}")
        self.code = code

class ProtectionReason(Enum):
    INACCESSIBLE = auto()
    REPARSE_POINT = auto()
    READ_ONLY = auto()
    IN_USE = auto()
    SYSTEM_HIDDEN = auto()
    HARD_LINK = auto()
    SYMLINK = auto()
    ADS = auto()
    EMPTY_FILE = auto()
    EXCESSIVE_DEPTH = auto()
    MOUNT_POINT = auto()
    EXCESSIVE_SIZE = auto()
    INVALID_TYPE = auto()
    OFFLINE = auto()
    ENCRYPTED_OR_COMPRESSED = auto()
    VOLUME_READ_ONLY = auto()
    REMOTE_DRIVE = auto()
    REMOVABLE_DRIVE = auto()
    KERNEL_LOCKED = auto()
    ACCESS_WRITE = auto()
    TOCTOU_VIOLATION = auto()
    SPARSE_FILE = auto()
    DEVICE_FILE = auto()
    VOLUME_RESTRICTED = auto()
    SYSTEM_OWNER = auto()
    VIRTUAL_DRIVE = auto()

class ValidationContext(Enum):
    STRUCTURAL = auto()
    INTEGRITY = auto()

PROTECTED_DIR_NAMES: Final[set[str]] = {
    "windows", "winnt", "system32", "syswow64", "system", "boot",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "perflogs", "msocache", "$windows.~bt", "$windows.~ws",
    "windowsapps", "assembly", "winsxs", "drivers", "drivestore",
    ".ssh", ".gnupg", "microsoft\\crypto", "protect",
    "bin", "sbin", "usr", "etc", "var", "lib", "lib64", "proc", "sys",
    "dev", "root", "library", "applications",
    "config.msi", "installer",
}

SENSITIVE_EXTENSIONS: Final[set[str]] = {
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
}

_SYSTEM_ROOT_PATHS: Final[set[Path]] = {
    Path(os.environ[v]).resolve() for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
}

_SYSTEM_ROOT_PATHS_TUPLE: Final[tuple[str, ...]] = tuple(str(p).lower() for p in _SYSTEM_ROOT_PATHS)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)

_KERNEL_BLOCKED_FILES: Final[frozenset[str]] = frozenset(
    ("pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp", "memory.dmp")
)

_KERNEL_BLOCKED_DIRS: Final[frozenset[str]] = frozenset(("config.msi", "installer"))

class _IntegrityCheck(NamedTuple):
    reason: ProtectionReason
    predicate: ViolationPredicate

class _CheckResult(NamedTuple):
    is_safe: bool
    reason: Optional[ProtectionReason] = None

@lru_cache(maxsize=1)
def is_running_as_admin() -> bool:
    """Verifica privilegios elevados del proceso actual."""
    if os.name != 'nt':
        try: return os.geteuid() == 0
        except (AttributeError, OSError): return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False

def _is_virtual_drive(path_str: str) -> bool:
    """Detecta si la unidad es un remapeo de dispositivo virtual o unión de DOS."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    drive = os.path.splitdrive(path_str)[0]
    if not drive: return False
    try:
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(512)
        res = kernel32.QueryDosDeviceW(drive, buf, 512)
        if res > 0:
            device_path = buf.value
            return device_path.startswith("\\DosDevices\\") or device_path.startswith("\\??\\")
    except (AttributeError, OSError, ctypes.ArgumentError):
        return True 
    return False

def _is_file_owned_by_system(path_str: str) -> bool:
    """Valida la propiedad mediante el Security Identifier (SID) de sistema."""
    if os.name != 'nt' or not isinstance(path_str, str): return False
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        sid_ptr = ctypes.c_void_p()
        res = advapi32.GetNamedSecurityInfoW(
            path_str, 1, 0x00000001, ctypes.byref(sid_ptr), None, None, None, None
        )
        if res == 0 and sid_ptr:
            try:
                sid_str = ctypes.create_unicode_buffer(128)
                if advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str)):
                    sid_val = sid_str.value
                    return sid_val.startswith("S-1-5-18") or sid_val.startswith("S-1-5-80")
            finally:
                kernel32.LocalFree(sid_ptr)
    except (AttributeError, OSError, ctypes.ArgumentError, PermissionError):
        pass
    return False

def _has_invalid_chars(path_str: Optional[str]) -> bool:
    """Busca caracteres de control y secuencias BIDI potencialmente maliciosas."""
    if not isinstance(path_str, str) or not path_str: return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E\u206A-\u206F]', path_str))

def _is_path_empty_or_whitespace(path_str: str) -> bool:
    return not path_str or path_str.strip() == ""

def _is_unc_path(path_str: str) -> bool:
    if not isinstance(path_str, str): return False
    return path_str.startswith(("\\\\", "//"))

@lru_cache(maxsize=128)
def _is_reserved_device_name(name: str) -> bool:
    if not name: return False
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))

def _is_device_file(path: Path) -> bool:
    path_str = str(path).upper()
    return path_str.startswith(("\\\\.\\", "//./"))

@lru_cache(maxsize=512)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta flujos de datos NTFS (ADS) que pueden esconder payloads."""
    return ":" in path_name and len(path_name.split(":")) > 2

def _is_system_directory_junction(path_str: str) -> bool:
    if os.name != 'nt' or not isinstance(path_str, str): return False
    attrs = _get_file_attrs(path_str)
    return bool(attrs & Win32Attr.REPARSE_POINT)

@lru_cache(maxsize=1024)
def _is_file_locked_by_other_process(path_str: str) -> bool:
    """Verifica disponibilidad exclusiva del archivo mediante CreateFileW."""
    if not isinstance(path_str, str) or os.name != 'nt' or not os.path.isabs(path_str): return False
    if os.path.isdir(path_str) or not os.path.exists(path_str): return False
    
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            _to_long_path(path_str), 0, 0, None, 3, 0x00000080, None
        )
        if handle == -1: 
            err = kernel32.GetLastError()
            return err in (ERROR_SHARING_VIOLATION, ERROR_INVALID_NAME)
        kernel32.CloseHandle(handle)
        return False
    except (OSError, ctypes.ArgumentError, AttributeError, MemoryError, PermissionError):
        return True

def _get_security_descriptor(path: Path) -> SecurityDescriptor:
    return SecurityDescriptor.from_path(str(path))

def _is_file_in_use_by_system(path_str: str) -> bool:
    if os.name != 'nt' or not isinstance(path_str, str): return False
    try:
        h_module = ctypes.windll.kernel32.GetModuleHandleW(path_str)
        return h_module != 0
    except (OSError, ctypes.ArgumentError): return False

@lru_cache(maxsize=128)
def _is_volume_readonly(path_str: Optional[str]) -> bool:
    """Verifica si el volumen destino tiene el bit de solo lectura activo."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str or not os.path.isabs(path_str): return False
    try:
        drive = os.path.splitdrive(path_str)[0]
        if not drive: return False
        root = drive + "\\"
        if not os.path.exists(root): return False
        flags = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
            return bool(flags.value & 0x80000)
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError, MemoryError): pass
    return False

@lru_cache(maxsize=128)
def _is_volume_busy_or_invalid(path_str: Optional[str]) -> bool:
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    try:
        drive = os.path.splitdrive(path_str)[0] + "\\"
        kernel32 = ctypes.windll.kernel32
        if kernel32.GetVolumeInformationW(drive, None, 0, None, None, None, None, 0) == 0:
            return True
        return False
    except Exception: return True

@lru_cache(maxsize=128)
def _is_volume_removable_media(path_str: Optional[str]) -> bool:
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str or not os.path.isabs(path_str): return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        if not os.path.exists(root): return False
        return ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_REMOVABLE
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError): pass
    return False

@lru_cache(maxsize=128)
def _is_volume_compressed_or_encrypted(path_str: Optional[str]) -> bool:
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str or not os.path.isabs(path_str): return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        if not os.path.exists(root): return False
        flags = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
            return bool(flags.value & (0x10 | 0x20000))
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError): pass
    return False

@lru_cache(maxsize=1024)
def _is_kernel_managed(path_str: str) -> bool:
    """Verifica si la ruta pertenece a directorios de gestión exclusiva del OS."""
    if not path_str: return False
    path = Path(path_str)
    try:
        if path.name.lower() in _KERNEL_BLOCKED_FILES: return True
        if os.name == 'nt':
            buf = ctypes.create_unicode_buffer(512)
            if ctypes.windll.kernel32.GetSystemDirectoryW(buf, 512) > 0:
                sys_dir = Path(buf.value).resolve()
                if sys_dir in path.parents: return True
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            app_data = os.environ.get("APPDATA", "")
            p_str = path_str.lower()
            if (local_app_data and p_str.startswith(local_app_data.lower())) or \
               (app_data and p_str.startswith(app_data.lower())): return True
        return any(part.lower() in _KERNEL_BLOCKED_DIRS for part in path.parts)
    except (OSError, RuntimeError): return False

@lru_cache(maxsize=1024)
def _is_sensitive_extension(ext: str) -> bool:
    return ext.lower() in SENSITIVE_EXTENSIONS

def _rule(reason: ProtectionReason, predicate: ViolationPredicate) -> _IntegrityCheck:
    return _IntegrityCheck(reason, predicate)

def _validate_drive_restrictions(target_path: Path) -> None:
    """Valida que el volumen raíz soporte operaciones de escritura seguras."""
    anchor = target_path.anchor
    if not anchor: return
    
    try:
        kernel32 = ctypes.windll.kernel32
        if _is_volume_busy_or_invalid(str(target_path)):
             raise UnsafePathError("Volumen inaccesible.", SafetyValidationErrorCode.IO_ERROR)
        drive_type = kernel32.GetDriveTypeW(anchor)
        if drive_type == DRIVE_NO_ROOT_DIR:
            raise UnsafePathError("Unidad inaccesible.", SafetyValidationErrorCode.IO_ERROR)
        if drive_type == DRIVE_REMOTE:
            raise UnsafePathError("Unidad de red bloqueada.", SafetyValidationErrorCode.REMOTE_DRIVE_DETECTED)
        if drive_type == DRIVE_REMOVABLE:
            raise UnsafePathError("Unidad extraíble bloqueada.", SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED)
        if drive_type == DRIVE_CDROM or _is_volume_readonly(str(target_path)):
            raise UnsafePathError("Volumen de solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY)
        if _is_volume_compressed_or_encrypted(str(target_path)):
            raise UnsafePathError("Volumen cifrado o comprimido.", SafetyValidationErrorCode.VOLUME_RESTRICTED)
        if _is_virtual_drive(str(target_path)):
            raise UnsafePathError("Unidad virtual bloqueada.", SafetyValidationErrorCode.VIRTUAL_DRIVE_DETECTED)
    except (AttributeError, OSError, ctypes.ArgumentError):
        pass

def _check_symlink(p: Path, _, __) -> bool: return p.is_symlink()
def _check_reparse(p: Path, _, sd: SecurityDescriptor) -> bool: return sd.is_reparse
def _check_kernel_locked(p: Path, _, __) -> bool: return _is_kernel_managed(str(p))
def _check_read_only(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: 
    return not bool(st.st_mode & stat.S_IWRITE) or sd.is_readonly
def _check_vol_read_only(p: Path, _, __) -> bool: return _is_volume_readonly(str(p))
def _check_vol_removable(p: Path, _, __) -> bool: return _is_volume_removable_media(str(p))
def _check_vol_restricted(p: Path, _, __) -> bool: return _is_volume_compressed_or_encrypted(str(p))
def _check_in_use(_, __, sd: SecurityDescriptor) -> bool: return sd.is_in_use
def _check_sys_hidden(_, __, sd: SecurityDescriptor) -> bool: return sd.is_protected_system
def _check_offline(_, __, sd: SecurityDescriptor) -> bool: return sd.has_flag(Win32Attr.OFFLINE)
def _check_enc_comp(_, __, sd: SecurityDescriptor) -> bool: return bool(sd.attrs & (Win32Attr.COMPRESSED | Win32Attr.ENCRYPTED))
def _check_sparse(_, __, sd: SecurityDescriptor) -> bool: return sd.has_flag(Win32Attr.SPARSE_FILE)
def _check_hard_link(p: Path, st: os.stat_result, __) -> bool: return p.is_file() and st.st_nlink > 1
def _check_ads(p: Path, _, __) -> bool: return _has_alternate_data_stream(p.name)
def _check_empty(p: Path, st: os.stat_result, __) -> bool: return p.is_file() and st.st_size == 0
def _check_size(p: Path, st: os.stat_result, __) -> bool: return p.is_file() and st.st_size > MAX_FILE_SIZE
def _check_mount(p: Path, _, __) -> bool: return os.path.ismount(p)
def _check_type(_, st: os.stat_result, __) -> bool: return not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))
def _check_owner(p: Path, _, __) -> bool: return _is_file_owned_by_system(str(p))
def _check_virtual(p: Path, _, __) -> bool: return _is_virtual_drive(str(p))

_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _rule(ProtectionReason.SYMLINK, _check_symlink),
    _rule(ProtectionReason.REPARSE_POINT, _check_reparse),
    _rule(ProtectionReason.KERNEL_LOCKED, _check_kernel_locked),
    _rule(ProtectionReason.READ_ONLY, _check_read_only),
    _rule(ProtectionReason.VOLUME_READ_ONLY, _check_vol_read_only),
    _rule(ProtectionReason.REMOVABLE_DRIVE, _check_vol_removable),
    _rule(ProtectionReason.VOLUME_RESTRICTED, _check_vol_restricted),
    _rule(ProtectionReason.IN_USE, _check_in_use),
    _rule(ProtectionReason.SYSTEM_HIDDEN, _check_sys_hidden),
    _rule(ProtectionReason.OFFLINE, _check_offline),
    _rule(ProtectionReason.ENCRYPTED_OR_COMPRESSED, _check_enc_comp),
    _rule(ProtectionReason.SPARSE_FILE, _check_sparse),
    _rule(ProtectionReason.HARD_LINK, _check_hard_link),
    _rule(ProtectionReason.ADS, _check_ads),
    _rule(ProtectionReason.EMPTY_FILE, _check_empty),
    _rule(ProtectionReason.EXCESSIVE_SIZE, _check_size),
    _rule(ProtectionReason.MOUNT_POINT, _check_mount),
    _rule(ProtectionReason.INVALID_TYPE, _check_type),
    _rule(ProtectionReason.SYSTEM_OWNER, _check_owner),
    _rule(ProtectionReason.VIRTUAL_DRIVE, _check_virtual),
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
    ProtectionReason.REMOVABLE_DRIVE: SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED,
    ProtectionReason.VOLUME_RESTRICTED: SafetyValidationErrorCode.VOLUME_RESTRICTED,
    ProtectionReason.REMOTE_DRIVE: SafetyValidationErrorCode.REMOTE_DRIVE_DETECTED,
    ProtectionReason.KERNEL_LOCKED: SafetyValidationErrorCode.KERNEL_LOCKED_FILE,
    ProtectionReason.ACCESS_WRITE: SafetyValidationErrorCode.WRITE_ACCESS_DENIED,
    ProtectionReason.MOUNT_POINT: SafetyValidationErrorCode.MOUNT_POINT_DETECTED,
    ProtectionReason.SPARSE_FILE: SafetyValidationErrorCode.SPARSE_FILE_DETECTED,
    ProtectionReason.EMPTY_FILE: SafetyValidationErrorCode.EMPTY_FILE,
    ProtectionReason.READ_ONLY: SafetyValidationErrorCode.WRITE_ACCESS_DENIED,
    ProtectionReason.SYSTEM_OWNER: SafetyValidationErrorCode.SYSTEM_OWNER_PROTECTION,
    ProtectionReason.VIRTUAL_DRIVE: SafetyValidationErrorCode.VIRTUAL_DRIVE_DETECTED,
}

def _evaluate_security_rules(path: Path, current_stat: os.stat_result) -> None:
    """Evalúa la lista de validadores contra los metadatos obtenidos."""
    if not path.exists():
         raise UnsafePathError("Archivo eliminado antes de la validación.", SafetyValidationErrorCode.IO_ERROR)
    try:
        sd = _get_security_descriptor(path)
        for rule in _VALIDATORS:
            if rule.predicate(path, current_stat, sd):
                code = _REASON_TO_CODE.get(rule.reason, SafetyValidationErrorCode.GENERIC)
                raise UnsafePathError(f"Integridad comprometida: {rule.reason.name}", code)
    except UnsafePathError:
        raise
    except Exception as e:
        raise UnsafePathError(f"Error evaluando reglas: {e}", SafetyValidationErrorCode.IO_ERROR)

def _get_path_stat_robust(path: Path) -> os.stat_result:
    """Lee metadatos de archivo con reintentos para manejar bloqueos transitorios."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not isinstance(path, Path):
                raise UnsafePathError("Tipo de objeto de ruta inválido", SafetyValidationErrorCode.GENERIC)
            if _is_device_file(path):
                raise UnsafePathError(f"Acceso a dispositivo bloqueado: {path.name}", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
                
            if os.name == 'nt':
                class WIN32_FILE_ATTRIBUTE_DATA(ctypes.Structure):
                    _fields_ = [("dwFileAttributes", ctypes.c_uint32),
                                ("ftCreationTime", ctypes.c_uint64),
                                ("ftLastAccessTime", ctypes.c_uint64),
                                ("ftLastWriteTime", ctypes.c_uint64),
                                ("nFileSizeHigh", ctypes.c_uint32),
                                ("nFileSizeLow", ctypes.c_uint32)]
                
                data = WIN32_FILE_ATTRIBUTE_DATA()
                if not ctypes.windll.kernel32.GetFileAttributesExW(_to_long_path(str(path)), 0, ctypes.byref(data)):
                    raise UnsafePathError(f"Acceso denegado o archivo inexistente: {path.name}", SafetyValidationErrorCode.ACCESS_DENIED)
            return path.stat()
        except (PermissionError, FileNotFoundError):
            raise UnsafePathError(f"Acceso denegado o archivo inexistente: {path.name}", SafetyValidationErrorCode.ACCESS_DENIED)
        except OSError as e:
            if e.errno in (errno.ETIMEDOUT, errno.EBUSY, errno.EAGAIN):
                if attempt < max_retries - 1:
                     time.sleep(0.1 * (2 ** attempt))
                     continue
            if getattr(e, 'winerror', None) == ERROR_ACCESS_DENIED:
                raise UnsafePathError(f"Acceso denegado: {path.name}", SafetyValidationErrorCode.ACCESS_DENIED)
            if getattr(e, 'winerror', None) in (ERROR_SHARING_VIOLATION, 1920) or e.errno == 13:
                 if attempt < max_retries - 1:
                     time.sleep(0.05 * (2 ** attempt))
                     continue
                 raise UnsafePathError(f"Archivo bloqueado por otro proceso: {path.name}", SafetyValidationErrorCode.FILE_IN_USE)
            raise UnsafePathError(f"Acceso fallido: {path.name}", SafetyValidationErrorCode.IO_ERROR)
    raise UnsafePathError(f"Error tras reintentos leyendo metadatos: {path.name}", SafetyValidationErrorCode.IO_ERROR)

def _check_file_integrity(path: Path, initial_stat: os.stat_result) -> None:
    """Verifica condiciones de Time-of-Check Time-of-Use (TOCTOU)."""
    if not path.exists():
        raise UnsafePathError("El archivo ya no existe (TOCTOU).", SafetyValidationErrorCode.IO_ERROR)
    current_stat = _get_path_stat_robust(path)
    if stat.S_ISREG(initial_stat.st_mode) != stat.S_ISREG(current_stat.st_mode):
        raise UnsafePathError(f"Cambio de tipo detectado (TOCTOU): {path.name}", SafetyValidationErrorCode.TOCTOU_VIOLATION)
    if current_stat.st_dev != initial_stat.st_dev or current_stat.st_ino != initial_stat.st_ino:
        raise UnsafePathError(f"Consistencia fallida (TOCTOU): {path.name}", SafetyValidationErrorCode.TOCTOU_VIOLATION)
    _evaluate_security_rules(path, current_stat)

@lru_cache(maxsize=2048)
def _is_readonly(path_str: str) -> bool:
    if not path_str: return True
    try:
        path = Path(path_str)
        if not path.exists(): return True
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError, FileNotFoundError):
        return True

def _validate_access_permissions(path: Path) -> None:
    if not path.exists():
        raise UnsafePathError("Archivo inexistente, no se pueden validar permisos.", SafetyValidationErrorCode.ACCESS_DENIED)
    try:
        if not os.access(path, os.R_OK):
            raise UnsafePathError("Permisos de lectura denegados.", SafetyValidationErrorCode.ACCESS_DENIED)
        if not os.access(path, os.W_OK):
            raise UnsafePathError("Permisos de escritura denegados.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
    except Exception:
        raise UnsafePathError("Acceso al archivo denegado por el sistema.", SafetyValidationErrorCode.ACCESS_DENIED)

@lru_cache(maxsize=4096)
def _normalize_resolved(path_str: str) -> Path:
    try:
        return Path(os.path.abspath(path_str)).resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(path_str).absolute()

@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Limpia, normaliza y resuelve una ruta, verificando trazas de seguridad."""
    if path is None: raise UnsafePathError("Ruta nula recibida.", SafetyValidationErrorCode.GENERIC)
    path_str = str(path).strip()
    
    if os.name == 'nt' and _is_path_too_long(path_str):
        if not path_str.startswith(LONG_PATH_PREFIX):
             raise UnsafePathError("Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG)

    if _is_path_empty_or_whitespace(path_str): raise UnsafePathError("Entrada de ruta vacía o inválida.", SafetyValidationErrorCode.GENERIC)
    if unicodedata.normalize('NFKC', path_str) != path_str:
         raise UnsafePathError("Ruta contiene secuencias Unicode sospechosas.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING)
    try:
        p = Path(path_str)
        current_subpath = Path(p.anchor)
        for part in p.parts:
            if part in (os.sep, os.altsep): continue
            current_subpath = current_subpath / part
            if current_subpath.exists() and _is_system_directory_junction(str(current_subpath)):
                raise UnsafePathError("Segmento de ruta contiene punto de reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        
        if _is_device_file(p): raise UnsafePathError("Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
        if _has_alternate_data_stream(p.name): raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED)
        
        resolved = _normalize_resolved(path_str)
        if ".." in p.parts: raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
        return resolved
    except (OSError, PermissionError, RuntimeError, ValueError, TypeError) as e:
        raise UnsafePathError(f"Error al normalizar ruta {path_str}: {e}", SafetyValidationErrorCode.IO_ERROR)

def is_absolute_path_allowed(path: PathLike) -> bool:
    try: return Path(path).is_absolute()
    except (TypeError, ValueError): return False

def is_drive_root(path: PathLike) -> bool:
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (UnsafePathError, TypeError, OSError): return True

@lru_cache(maxsize=4096)
def is_protected_path(path: PathLike) -> bool:
    """Determina si la ruta forma parte de una zona protegida del sistema."""
    if not isinstance(path, (str, Path)) or not path: return True
    try:
        p_resolved = _normalize_resolved(str(path))
        
        if p_resolved.is_symlink() or _is_system_directory_junction(str(p_resolved)): return True
        
        if PROTECTED_DIR_NAMES.isdisjoint(part.lower() for part in p_resolved.parts):
            if not _is_kernel_managed(str(p_resolved)):
                if not any(p_resolved.is_relative_to(root) for root in _SYSTEM_ROOT_PATHS):
                    attrs = _get_file_attrs(str(p_resolved))
                    if not (attrs & (Win32Attr.SYSTEM | Win32Attr.HIDDEN)):
                        return p_resolved == Path(p_resolved.anchor)
        return True
    except Exception: return True

@lru_cache(maxsize=4096)
def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica si una ruta es subdirectorio de otra, evitando escapes."""
    if child is None or parent is None: return False
    try:
        c_path = normalize(child)
        p_path = normalize(parent)
        if is_drive_root(c_path) or is_protected_path(str(c_path)): return False
        
        for part in c_path.parts:
             if _is_system_directory_junction(str(c_path.anchor) + os.sep.join(c_path.parts[:c_path.parts.index(part)+1])):
                 return False

        return c_path.is_relative_to(p_path) if allow_equal else (c_path.is_relative_to(p_path) and c_path != p_path)
    except (UnsafePathError, TypeError, OSError, RuntimeError, ValueError): return False

@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Determina si el archivo es sensible por su extensión."""
    if not path: return True
    try: 
        p_str = str(path)
        return _is_sensitive_extension(os.path.splitext(p_str)[1])
    except (TypeError, ValueError, OSError): return True 

def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """Validación sintáctica profunda de la ruta."""
    if not isinstance(path_string, str):
        raise UnsafePathError("Ruta no es texto.", SafetyValidationErrorCode.GENERIC)
    if path_string.startswith(("\\\\.\\", "//./", LONG_PATH_PREFIX)) and not path_string.startswith(LONG_PATH_PREFIX):
        raise UnsafePathError("Acceso a Namespace de dispositivos prohibido.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
    if _is_unc_path(path_string):
        raise UnsafePathError("Rutas UNC/Red bloqueadas.", SafetyValidationErrorCode.UNC_PATH)
    if ".." in path_string.split(os.sep):
        raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    if _is_path_too_long(path_string):
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
    if _is_reserved_device_name(target_path.name):
        raise UnsafePathError("Nombre de dispositivo reservado detectado.", SafetyValidationErrorCode.RESERVED_NAME)
    if re.search(r'[\u200E\u200F\u202A-\u202E]', path_string):
        raise UnsafePathError("Posible ataque de bidi spoofing detectado.", SafetyValidationErrorCode.BIDI_SPOOFING)
    if "." in target_path.name and _is_reserved_device_name(target_path.stem):
        raise UnsafePathError("Nombre de dispositivo con extensión prohibido.", SafetyValidationErrorCode.RESERVED_NAME)

    try:
        if target_path.exists() and not target_path.is_absolute():
            raise UnsafePathError("Ruta inconsistente con el sistema.", SafetyValidationErrorCode.GENERIC)
        if not target_path.parts or (len(target_path.parts) == 1 and target_path.parts[0] == os.sep):
             raise UnsafePathError("Ruta raíz no permitida.", SafetyValidationErrorCode.ROOT_ACCESS)
        if len(target_path.parts) == 1 and target_path == Path(target_path.anchor):
             raise UnsafePathError("Operación sobre raíz de unidad prohibida.", SafetyValidationErrorCode.ROOT_ACCESS)
        if len(target_path.parts) > MAX_DIRECTORY_DEPTH:
             raise UnsafePathError("Profundidad de directorio excesiva.", SafetyValidationErrorCode.PATH_TOO_DEEP)
    except (AttributeError, TypeError, ValueError):
        raise UnsafePathError("Estructura de ruta inválida.", SafetyValidationErrorCode.GENERIC)

def _validate_preconditions(target_path: Path, base_dir: Optional[PathLike]) -> None:
    """Verificaciones de prerrequisitos antes de operar sobre un archivo."""
    if not is_absolute_path_allowed(target_path):
        raise UnsafePathError("Solo se permiten rutas absolutas.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED)
    
    if _is_kernel_managed(str(target_path)):
        raise UnsafePathError(f"Archivo de sistema crítico: {target_path.name}", SafetyValidationErrorCode.KERNEL_LOCKED_FILE)
    
    if os.name == 'nt':
        if _is_volume_readonly(str(target_path)) or _is_volume_compressed_or_encrypted(str(target_path)) or \
           _is_virtual_drive(str(target_path)) or _is_volume_busy_or_invalid(str(target_path)):
            raise UnsafePathError(f"Volumen restringido/inaccesible: {target_path.anchor}", SafetyValidationErrorCode.VOLUME_RESTRICTED)
        if os.path.ismount(target_path):
            raise UnsafePathError(f"Punto de montaje bloqueado: {target_path}", SafetyValidationErrorCode.MOUNT_POINT_DETECTED)
        _validate_drive_restrictions(target_path)

def _validate_boundary_conditions(target_path: Path, root_directory: Optional[PathLike]) -> None:
    """Valida los límites del alcance (jail) y permisos generales de la ruta."""
    if root_directory:
        if not isinstance(root_directory, (str, Path, os.PathLike)):
             raise UnsafePathError("Directorio base inválido.", SafetyValidationErrorCode.GENERIC)
        try:
            rd_path = Path(root_directory)
            if not rd_path.is_absolute():
                raise UnsafePathError("El directorio base debe ser absoluto.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED)
            if not is_within_directory(target_path, rd_path, allow_equal=True):
                raise UnsafePathError("Fuera de alcance permitido.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
        except (TypeError, ValueError):
            raise UnsafePathError("Directorio base inválido.", SafetyValidationErrorCode.GENERIC)

    if is_protected_path(str(target_path)):
        raise UnsafePathError("Ruta en directorio del sistema bloqueada.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
    
    try:
        app_root = _normalize_resolved(os.getcwd())
        if target_path == app_root or app_root in target_path.parents:
            raise UnsafePathError("Modificación de App denegada.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    except (OSError, RuntimeError, ValueError): pass
    if is_drive_root(target_path):
        raise UnsafePathError("Acceso a raíz denegado.", SafetyValidationErrorCode.ROOT_ACCESS)

def _get_final_path_normalized(path: Path) -> Optional[Path]:
    """Resuelve rutas finales resolviendo junctions y enlaces NTFS."""
    if os.name != 'nt': return _normalize_resolved(str(path))
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(_to_long_path(str(path)), 0, 0, None, 3, 0x02000000, None)
        if handle == -1: return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.GetFinalPathNameByHandleW(handle, buf, 1024, 0) > 0:
                return _normalize_resolved(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ctypes.ArgumentError):
        return None
    return None

def _is_junction_target_outside_base(path: Path, base_dir: Path) -> bool:
    final_path = _get_final_path_normalized(path)
    if not final_path: return False
    return not final_path.is_relative_to(base_dir)

def _is_reparse_point_safe(path: Path, base_dir: Optional[Path]) -> bool:
    if not base_dir: return True
    final_path = _get_final_path_normalized(path)
    return final_path is not None and final_path.is_relative_to(base_dir)

def _validate_ntfs_reparse_redirection(path: Path, base_dir: Optional[Path]) -> None:
    """Verifica que los reparse points no redirijan fuera de la zona segura."""
    if not path.exists(): return
    if _is_system_directory_junction(str(path)):
        if not _is_reparse_point_safe(path, base_dir):
            raise UnsafePathError("Redirección de reparse point fuera de base.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        
        for parent in path.parents:
            if _is_system_directory_junction(str(parent)):
                if not _is_reparse_point_safe(parent, base_dir):
                    raise UnsafePathError("Reparse point anidado detectado.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
            if parent == base_dir: break

def _validate_path_components(path: Path) -> None:
    current_check = Path(path.anchor)
    for part in path.parts:
        if part in (os.sep, os.altsep): continue
        current_check = current_check / part
        if current_check.exists() and _is_system_directory_junction(str(current_check)):
            raise UnsafePathError(f"Punto de reparse detectado en el camino: {current_check}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)

def _check_hard_link_security(path: Path, stat_res: os.stat_result) -> None:
    """Previene manipulación de archivos con múltiples enlaces duros."""
    if path.is_file() and stat_res.st_nlink > 1:
        raise UnsafePathError(f"Archivo con múltiples hard links detectado: {path.name}", SafetyValidationErrorCode.HARD_LINK_DETECTED)

def _validate_file_operations_security(p: Path, allow_sensitive: bool, base: Optional[Path]) -> None:
    """Validaciones finales sobre archivos específicos para operaciones de escritura."""
    if not os.access(p, os.W_OK):
        raise UnsafePathError(f"Permisos de escritura insuficientes: {p.name}", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
    
    if not p.exists():
        raise UnsafePathError(f"El archivo fue eliminado: {p.name}", SafetyValidationErrorCode.ACCESS_DENIED)
        
    _validate_access_permissions(p)
    if _is_file_in_use_by_system(str(p)):
        raise UnsafePathError(f"Archivo en uso por el sistema: {p.name}", SafetyValidationErrorCode.FILE_IN_USE)
    
    initial_stat = _get_path_stat_robust(p)
    _check_hard_link_security(p, initial_stat)
    if os.name == 'nt': 
        _validate_ntfs_reparse_redirection(p, base)
    _check_file_integrity(p, initial_stat)

def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> Path:
    """
    Función de entrada principal para validar si un archivo puede ser modificado.
    Lanza `UnsafePathError` ante cualquier riesgo.
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.", SafetyValidationErrorCode.GENERIC)
        
    if not isinstance(path, (str, Path, os.PathLike)):
        raise UnsafePathError(f"Tipo de ruta no soportado: {type(path).__name__}", SafetyValidationErrorCode.GENERIC)
    try:
        path_str = str(path)
        if not path_str.strip():
            raise UnsafePathError("Ruta vacía no permitida.", SafetyValidationErrorCode.GENERIC)
            
        if os.name == 'nt' and os.path.exists(path_str) and _is_file_locked_by_other_process(path_str):
             raise UnsafePathError(f"Archivo bloqueado por el sistema: {path_str}", SafetyValidationErrorCode.FILE_IN_USE)

        if _has_invalid_chars(path_str):
            raise UnsafePathError("Caracteres inválidos detectados.", SafetyValidationErrorCode.INVALID_CHARS)
        
        p = normalize(path)
        base = None
        if base_dir:
            try:
                base = _normalize_resolved(str(base_dir))
            except (TypeError, ValueError):
                raise UnsafePathError("Directorio base inválido.", SafetyValidationErrorCode.GENERIC)
        
        _validate_path_components(p)
        _validate_preconditions(p, base_dir)
        
        if not allow_sensitive and is_sensitive_file(p):
            raise UnsafePathError(f"Extensión bloqueada '{p.suffix}'.", SafetyValidationErrorCode.SENSITIVE_EXTENSION)
            
        _validate_structural_safety(p, str(p))
        _validate_boundary_conditions(p, base_dir)
        
        if p.exists():
            _validate_file_operations_security(p, allow_sensitive, base)
        else:
            parent = p.parent
            if parent.exists() and not os.access(parent, os.W_OK):
                raise UnsafePathError("Directorio contenedor no tiene permisos de escritura.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
        return p
    except UnsafePathError:
        raise
    except (OSError, AttributeError, RuntimeError, ValueError, TypeError) as e:
        raise UnsafePathError(f"Validación fallida: {e}", SafetyValidationErrorCode.IO_ERROR)
    except Exception as e:
        raise UnsafePathError(f"Error crítico durante la validación: {e}", SafetyValidationErrorCode.IO_ERROR)

def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> bool:
    """Versión booleana de `ensure_safe_to_modify` para filtros en bucles."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive, base_dir=base_dir)
        return True
    except (UnsafePathError, ValueError, TypeError, OSError, PermissionError): return False

def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> list[Path]:
    """Aplica la validación de seguridad a una colección de rutas."""
    results = []
    if paths is None: return results
    for p in paths:
        if p is None: continue
        try: results.append(ensure_safe_to_modify(p, allow_sensitive=allow_sensitive, base_dir=base_dir))
        except (UnsafePathError, ValueError, TypeError, OSError, PermissionError): continue
    return results

def describe_protection(path: PathLike) -> str:
    """Genera una descripción legible de por qué una ruta fue bloqueada o permitida."""
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        raw_str = str(path)
    except (UnsafePathError, TypeError, ValueError): return "Ruta mal formada."
    
    if _is_unc_path(raw_str): return f"'{raw_str}' es ruta de red."
    if _is_device_file(p): return f"'{raw_str}' es un archivo de dispositivo."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(str(p)): return f"'{p}' protegida por sistema."
    
    try:
        if p.exists():
            sd = _get_security_descriptor(p)
            diagnostics = {
                "enlace simbólico": p.is_symlink(),
                "punto de reparse": sd.is_reparse,
                "punto de montaje": os.path.ismount(p),
                "unidad virtual mapeada": _is_virtual_drive(str(p)),
                "atributo de solo lectura": sd.is_readonly,
                "solo lectura (permisos)": _is_readonly(str(p)),
                "volumen de solo lectura": _is_volume_readonly(str(p)),
                "volumen extraíble": _is_volume_removable_media(str(p)),
                "volumen cifrado/comprimido": _is_volume_compressed_or_encrypted(str(p)),
                "archivo en uso": sd.is_in_use,
                "archivo cifrado o comprimido": sd.has_flag(Win32Attr.COMPRESSED) or sd.has_flag(Win32Attr.ENCRYPTED),
                "archivo disperso (sparse)": sd.has_flag(Win32Attr.SPARSE_FILE),
                "archivo offline/nube": sd.has_flag(Win32Attr.OFFLINE),
                "atributo oculto/sistema": sd.is_protected_system,
                "contiene ADS": _has_alternate_data_stream(p.name),
                "tamaño excesivo": p.is_file() and p.stat().st_size > MAX_FILE_SIZE,
                "detectado como hard link": p.is_file() and p.stat().st_nlink > 1
            }
            for reason, is_active in diagnostics.items():
                if is_active: return f"'{p}' es {reason}."
            
            if not (p.is_file() or p.is_dir()): return f"'{p}' tipo de objeto no soportado."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío (potencialmente crítico)."
            
    except (OSError, FileNotFoundError, AttributeError): pass
    
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
