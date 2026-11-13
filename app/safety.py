"""
safety.py — capa de seguridad para la manipulación de archivos.

Este módulo provee funciones de validación estricta para prevenir modificaciones
accidentales o maliciosas en directorios del sistema o archivos críticos.
Todo cambio destructivo debe pasar obligatoriamente por `ensure_safe_to_modify`.

El diseño sigue un enfoque defensivo:
1. Las rutas son normalizadas y resueltas antes de cualquier inspección.
2. Se utilizan identificadores de archivo (File ID) en lugar de nombres de ruta 
   para mitigar condiciones de carrera (Time-of-Check Time-of-Use).
3. Las operaciones de E/S nativas se encapsulan para manejar excepciones de 
   Windows que no mapean directamente a `OSError`.
"""

from __future__ import annotations
import os
import stat
import re
import ctypes
import time
import errno
import sys
from enum import Enum, auto, IntEnum
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, Optional, TypedDict
from functools import lru_cache
import unicodedata

# Constantes de error de Win32 API para llamadas nativas
ERROR_SHARING_VIOLATION: Final[int] = 32
ERROR_FILE_NOT_FOUND: Final[int] = 2
ERROR_ACCESS_DENIED: Final[int] = 5
ERROR_INSUFFICIENT_BUFFER: Final[int] = 122
ERROR_INVALID_NAME: Final[int] = 123
ERROR_DEVICE_NOT_AVAILABLE: Final[int] = 55
ERROR_NETWORK_UNREACHABLE: Final[int] = 1231
INVALID_FILE_ATTRIBUTES: Final[int] = 0xFFFFFFFF
INVALID_HANDLE_VALUE: Final[int] = -1
LONG_PATH_PREFIX: Final[str] = "\\\\?\\"
FILE_ID_INFO_LEVEL: Final[int] = 18
FILE_FLAG_BACKUP_SEMANTICS: Final[int] = 0x02000000

PathLike: TypeAlias = Union[str, os.PathLike]
ViolationPredicate: TypeAlias = Callable[[Path, os.stat_result, "SecurityDescriptor"], bool]

class PathValidationResult(NamedTuple):
    """Resultado estructurado de una validación de ruta."""
    is_valid: bool
    error: Optional[UnsafePathError] = None

class PathContext(NamedTuple):
    """Encapsula el estado de una ruta bajo inspección para el pipeline de seguridad."""
    path: Path
    path_str: str
    guard: SafetyGuard

# Cachés para optimización de rendimiento: TTL de 5s y límite de 1024 entradas
_SECURITY_CACHE: Final[dict[tuple[int, int, int, int], float]] = {}
_NEGATIVE_SECURITY_CACHE: Final[dict[tuple[int, int, int, int], float]] = {}
_STRUCTURAL_CACHE: Final[dict[str, tuple[bool, float]]] = {}
_FILE_ID_CACHE: Final[dict[str, tuple[int, int, int, int]]] = {}
_ATTR_CACHE: Final[dict[str, tuple[int, float]]] = {}
_PROTECTION_CACHE: Final[dict[str, tuple[bool, float]]] = {}
_VOLUME_SERIAL_CACHE: Final[dict[str, tuple[int, float]]] = {}

def _is_cache_valid(cache_map: dict[any, float | tuple[any, float]], key: any, ttl: float = 5.0) -> bool:
    """Verifica si la entrada en caché está dentro del periodo de validez."""
    if key not in cache_map: return False
    val = cache_map[key]
    ts = val[1] if isinstance(val, tuple) else val
    if time.monotonic() - ts > ttl:
        cache_map.pop(key, None)
        return False
    return True

class SafetyValidationCategory(Enum):
    """Clasificación de alto nivel para errores de seguridad, usada para diagnóstico."""
    STRUCTURE = auto()
    INTEGRITY = auto()
    PERMISSION = auto()
    ENVIRONMENT = auto()

class SafetyValidationErrorCode(IntEnum):
    """Códigos de error específicos para cada falla detectada en validación."""
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
    CIRCULAR_REFERENCE = 35
    ADMIN_PRIVILEGE_VIOLATION = 36
    VOLUME_SHADOW_COPY = 38
    FILENAME_TOO_LONG = 39
    INVALID_PATH_TYPE = 40
    APP_DIRECTORY_VIOLATION = 41
    FILE_TOO_LARGE = 42

class SafetyGuard:
    """
    Objeto de contexto que encapsula la comprobación de bloqueos y la extracción
    de metadatos de seguridad. Facilita la toma de decisiones basada en estado.
    """
    def __init__(self, target_path: Path):
        self.path = target_path
        self.path_str = str(target_path)
        self.is_nt = os.name == 'nt'

    def is_locked(self) -> bool:
        """Determina si el SO mantiene un bloqueo sobre la ruta (exclusivo o sistema)."""
        if not self.is_nt: return False
        try:
            return _is_file_in_use_exclusive(self.path_str) or _is_file_in_use_by_system(self.path_str)
        except (OSError, FileNotFoundError, AttributeError, TypeError):
            return True

    def get_descriptor(self) -> SecurityDescriptor:
        """Crea una instantánea inmutable de las propiedades de seguridad de la ruta."""
        return SecurityDescriptor.from_path(self.path_str)

class FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

class FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_uint64),
        ("FileId", FILE_ID_128),
    ]

class WIN32_FILE_ATTRIBUTE_DATA(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", ctypes.c_uint64),
        ("ftLastAccessTime", ctypes.c_uint64),
        ("ftLastWriteTime", ctypes.c_uint64),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
    ]

class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", ctypes.c_uint64),
        ("ftLastAccessTime", ctypes.c_uint64),
        ("ftLastWriteTime", ctypes.c_uint64),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]

class SafetyState(NamedTuple):
    """Estado capturado (point-in-time) de un archivo para validaciones de integridad."""
    path: Path
    descriptor: SecurityDescriptor
    stat_result: os.stat_result
    file_id: tuple[int, int, int, int]

def _get_volume_serial(root_path: str) -> int:
    """Obtiene el número de serie de volumen para validar integridad persistente."""
    if os.name != 'nt' or not root_path: return 0
    if _is_cache_valid(_VOLUME_SERIAL_CACHE, root_path):
        return _VOLUME_SERIAL_CACHE[root_path][0]
    
    serial = ctypes.c_ulong(0)
    try:
        if ctypes.windll.kernel32.GetVolumeInformationW(root_path, None, 0, ctypes.byref(serial), None, None, None, 0):
            val = serial.value
            _VOLUME_SERIAL_CACHE[root_path] = (val, time.monotonic())
            return val
    except Exception: pass
    return 0

def _is_file_in_use_exclusive(path_str: str) -> bool:
    """Intenta abrir el archivo para escritura exclusiva con flags permisivos."""
    if os.name != 'nt': return False
    kernel32 = ctypes.windll.kernel32
    path_long = _to_long_path(path_str)
    handle = kernel32.CreateFileW(path_long, 0x40000000, 0x07, None, 3, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        return True
    try:
        return False
    finally:
        kernel32.CloseHandle(handle)

def _get_file_id(path_str: str) -> tuple[int, int, int, int]:
    """Genera un identificador único (Volumen + FileID) mediante handles de sistema."""
    if not isinstance(path_str, str) or not path_str or path_str.upper().startswith(("\\\\.\\", "//./")):
        return (0, 0, 0, 0)
    
    if path_str in _FILE_ID_CACHE:
        return _FILE_ID_CACHE[path_str]
    
    res = (0, 0, 0, 0)
    try:
        if os.name != 'nt':
            st = os.stat(path_str)
            res = (st.st_dev, st.st_ino, 0, 0)
        else:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateFileW(_to_long_path(path_str), 0x00, 0x03, None, 3, FILE_FLAG_BACKUP_SEMANTICS, None)
            if handle != INVALID_HANDLE_VALUE:
                try:
                    fid = FILE_ID_INFO()
                    if kernel32.GetFileInformationByHandleEx(handle, FILE_ID_INFO_LEVEL, ctypes.byref(fid), ctypes.sizeof(fid)):
                        res = (int(fid.VolumeSerialNumber), int.from_bytes(bytes(fid.FileId.Identifier), 'little'), 0, 0)
                finally:
                    kernel32.CloseHandle(handle)
    except Exception:
        pass
                
    if len(_FILE_ID_CACHE) >= 4096: _FILE_ID_CACHE.clear()
    _FILE_ID_CACHE[path_str] = res
    return res

class SafetyAction(Enum):
    READ = auto()
    MODIFY = auto()

class Win32Attr(IntEnum):
    """Flags de atributos de archivo de Windows."""
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

class SecurityDescriptor(NamedTuple):
    """Inmutable que encapsula las propiedades de seguridad de un nodo del sistema."""
    attrs: int
    is_protected_system: bool
    is_in_use: bool
    is_readonly: bool
    is_reparse: bool
    
    def has_flag(self, flag: Win32Attr) -> bool:
        """Verifica si un atributo Win32 específico está activo."""
        return bool(self.attrs & flag)

    @classmethod
    @lru_cache(maxsize=1024)
    def from_path(cls, path_str: str) -> SecurityDescriptor:
        """Constructor de fábrica que analiza atributos y estado de seguridad."""
        if not isinstance(path_str, str) or not path_str:
            return cls(0, True, True, True, True)
        try:
            attrs = _get_file_attrs(path_str)
            if attrs == INVALID_FILE_ATTRIBUTES:
                return cls(0, True, True, True, True)
                
            risk_flags = (Win32Attr.HIDDEN | Win32Attr.SYSTEM | Win32Attr.OFFLINE | 
                          Win32Attr.TEMPORARY | Win32Attr.REPARSE_POINT)
            is_protected = bool(attrs & risk_flags) or _is_file_owned_by_system(path_str)
            is_in_use = is_protected or _is_file_in_use_exclusive(path_str)
            
            return cls(
                attrs=attrs,
                is_protected_system=is_protected,
                is_in_use=is_in_use,
                is_readonly=bool(attrs & Win32Attr.READONLY),
                is_reparse=bool(attrs & Win32Attr.REPARSE_POINT)
            )
        except (OSError, ValueError, TypeError, AttributeError, ctypes.ArgumentError, Exception):
            return cls(0, True, True, True, True)

class FileMetadata(TypedDict):
    is_reparse: bool
    is_system_hidden: bool
    is_readonly: bool
    is_in_use: bool

__all__ = [
    "UnsafePathError", "PROTECTED_DIR_NAMES", "SENSITIVE_EXTENSIONS", "normalize",
    "is_drive_root", "is_protected_path", "is_within_directory", "ensure_safe_to_modify",
    "is_safe_to_modify", "filter_safe_paths", "is_sensitive_file", "describe_protection",
    "is_running_as_admin",
]

MAX_PATH_LENGTH: Final[int] = 260
MAX_FILENAME_LENGTH: Final[int] = 255
MAX_FILE_SIZE: Final[int] = 2 * 1024 * 1024 * 1024
MAX_DIRECTORY_DEPTH: Final[int] = 32
MAX_REPARSE_DEPTH: Final[int] = 4
DRIVE_UNKNOWN: Final[int] = 0
DRIVE_NO_ROOT_DIR: Final[int] = 1
DRIVE_REMOVABLE: Final[int] = 2 
DRIVE_FIXED: Final[int] = 3     
DRIVE_REMOTE: Final[int] = 4    
DRIVE_CDROM: Final[int] = 5     
DRIVE_RAMDISK: Final[int] = 6   

@lru_cache(maxsize=8192)
def _to_long_path(path_str: str) -> str:
    """Habilita rutas de hasta 32k caracteres anteponiendo el prefijo '\\\\?\\'."""
    if not isinstance(path_str, str) or not path_str:
        return ""
    if os.name == 'nt':
        if path_str.startswith(LONG_PATH_PREFIX):
            return path_str
        if path_str.startswith("\\\\"):
            if path_str.startswith("\\\\?\\"): return path_str
            return "\\\\?\\UNC" + path_str[1:]
        try:
            abs_path = os.path.abspath(path_str)
            return LONG_PATH_PREFIX + abs_path
        except (OSError, ValueError, TypeError):
            return path_str
    return path_str

def _is_path_too_long(path_str: str) -> bool:
    """Verifica si la cadena de ruta excede el estándar MAX_PATH."""
    return isinstance(path_str, str) and len(path_str) > MAX_PATH_LENGTH

def _get_file_attrs(path_str: Optional[str]) -> int:
    """Recupera los atributos de archivo mediante GetFileAttributesW."""
    if not isinstance(path_str, str) or not path_str:
        return 0
    if _is_cache_valid(_ATTR_CACHE, path_str):
        return _ATTR_CACHE[path_str][0]
    try:
        long_p = _to_long_path(path_str)
        attrs = ctypes.windll.kernel32.GetFileAttributesW(long_p)
        if attrs == INVALID_FILE_ATTRIBUTES:
            err = ctypes.windll.kernel32.GetLastError()
            if err == ERROR_ACCESS_DENIED:
                attrs = Win32Attr.SYSTEM
            else:
                return INVALID_FILE_ATTRIBUTES
        if len(_ATTR_CACHE) > 8192: _ATTR_CACHE.clear()
        _ATTR_CACHE[path_str] = (attrs, time.monotonic())
        return attrs
    except (AttributeError, ctypes.ArgumentError, OSError, ValueError, Exception):
        return 0

class UnsafePathError(Exception):
    """Excepción base para cualquier violación de las políticas de seguridad."""
    def __init__(self, message: str, code: SafetyValidationErrorCode = SafetyValidationErrorCode.GENERIC, category: SafetyValidationCategory = SafetyValidationCategory.STRUCTURE):
        super().__init__(f"[{category.name}] [{code.name}] {message}")
        self.code = code
        self.category = category

class ProtectionReason(Enum):
    """Enumeración de por qué un objeto ha sido protegido."""
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
    DEVICE_FILE_DETECTED = auto()
    VOLUME_RESTRICTED = auto()
    SYSTEM_OWNER = auto()
    VIRTUAL_DRIVE = auto()
    CIRCULAR_REF = auto()
    ADMIN_PRIVILEGE = auto()
    SHADOW_COPY = auto()
    INVALID_PATH_TYPE = auto()

class ValidationContext(Enum):
    """Define el contexto de la validación (estructura vs integridad)."""
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

@lru_cache(maxsize=1)
def _get_system_root_paths() -> tuple[str, ...]:
    """Obtiene y normaliza rutas raíz del sistema."""
    paths = []
    for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        env_val = os.environ.get(v)
        if env_val:
            paths.append(os.path.normcase(env_val))
    return tuple(paths)

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
    """Verifica si el proceso actual tiene privilegios elevados."""
    if os.name != 'nt':
        try: return os.geteuid() == 0
        except (AttributeError, OSError): return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, ctypes.ArgumentError, Exception):
        return False

@lru_cache(maxsize=128)
def _is_virtual_drive(path_str: str) -> bool:
    """Detección de unidades virtuales mapeadas mediante `QueryDosDeviceW`."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    try:
        drive = os.path.splitdrive(path_str)[0]
        if not drive: return False
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(512)
        res = kernel32.QueryDosDeviceW(drive, buf, 512)
        if res > 0:
            device_path = buf.value
            return device_path.startswith("\\DosDevices\\") or device_path.startswith("\\??\\")
    except (AttributeError, OSError, ctypes.ArgumentError, Exception):
        return False 
    return False

def _is_volume_shadow_copy(path_str: str) -> bool:
    """Verifica si la ruta reside en una instantánea VSS."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str or not path_str.startswith("\\\\?\\"): return False
    return "\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy" in path_str.upper()

def _is_file_owned_by_system(path_str: str) -> bool:
    """Determina si el propietario del archivo es el usuario SYSTEM."""
    if os.name != 'nt' or not isinstance(path_str, str): return False
    long_path = _to_long_path(path_str)
    if not os.path.exists(long_path): return False
    
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    sid_ptr = ctypes.c_void_p()
    try:
        res = advapi32.GetNamedSecurityInfoW(
            long_path, 1, 1, ctypes.byref(sid_ptr), None, None, None, None
        )
        if res == 0 and sid_ptr.value:
            sid_str = ctypes.create_unicode_buffer(128)
            if advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str)):
                sid_val = sid_str.value
                return sid_val.startswith(("S-1-5-18", "S-1-5-19", "S-1-5-20", "S-1-5-80"))
    except (AttributeError, OSError, ctypes.ArgumentError, TypeError):
        pass
    finally:
        if sid_ptr.value:
            kernel32.LocalFree(sid_ptr)
    return False

def _has_invalid_chars(path_str: Optional[str]) -> bool:
    """Filtra rutas que contienen caracteres no permitidos."""
    if not isinstance(path_str, str) or not path_str: return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E\u206A-\u206F]', path_str))

def _is_spoofing_detected(text: str) -> bool:
    """Detecta secuencias Unicode usadas para ofuscación."""
    return bool(re.search(r'[\u200E\u200F\u202A-\u202E\u206A-\u206F]', text))

def _is_path_empty_or_whitespace(path_str: str) -> bool:
    """Valida que la cadena de ruta no sea nula o vacía."""
    return not isinstance(path_str, str) or not path_str or path_str.strip() == ""

def _is_unc_path(path_str: str) -> bool:
    """Identifica rutas UNC."""
    if not isinstance(path_str, str): return False
    return path_str.startswith(("\\\\", "//"))

def _is_unc_path_accessible(path_str: str) -> bool:
    """Verifica accesibilidad de una ruta UNC."""
    if not _is_unc_path(path_str) or os.name != 'nt': return False
    try:
        return ctypes.windll.kernel32.GetDiskFreeSpaceW(path_str, None, None, None, None) != 0
    except (OSError, ctypes.ArgumentError): return False

@lru_cache(maxsize=128)
def _is_reserved_device_name(name: str) -> bool:
    """Detecta dispositivos reservados por DOS."""
    if not name: return False
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))

def _is_device_file(path: Path) -> bool:
    """Comprueba si un objeto apunta a un dispositivo."""
    if not isinstance(path, Path): return False
    path_str = str(path).upper()
    if path_str.startswith(("\\\\.\\", "//./")): return True
    try:
        name = path.name.split('.')[0]
        return _is_reserved_device_name(name)
    except Exception: return False

@lru_cache(maxsize=512)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta streams NTFS."""
    return isinstance(path_name, str) and ":" in path_name and len(path_name.split(":")) > 2

def _is_system_directory_junction(path_str: str) -> bool:
    """Verifica si una ruta es un punto de unión NTFS."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    attrs = _get_file_attrs(path_str)
    if attrs == INVALID_FILE_ATTRIBUTES: return True
    return bool(attrs & Win32Attr.REPARSE_POINT)

def _is_file_in_use_by_system(path_str: str) -> bool:
    """Detecta bloqueos del sistema mediante el handle de módulo."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    try:
        h_module = ctypes.windll.kernel32.GetModuleHandleW(path_str)
        return h_module != 0
    except (OSError, ctypes.ArgumentError): return False

@lru_cache(maxsize=128)
def _is_volume_readonly(path_str: Optional[str]) -> bool:
    """Verifica si el volumen destino tiene flag de solo lectura."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    if _is_unc_path(path_str): return False
    try:
        drive = os.path.splitdrive(path_str)[0] + "\\"
        serial = _get_volume_serial(drive)
        if not serial: return True
        flags = ctypes.c_ulong(0)
        if ctypes.windll.kernel32.GetVolumeInformationW(drive, None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
            return bool(flags.value & 0x80000)
    except (AttributeError, OSError, ctypes.ArgumentError, TypeError): pass
    return True

@lru_cache(maxsize=128)
def _is_volume_busy_or_invalid(path_str: Optional[str]) -> bool:
    """Comprueba si un volumen está inaccesible."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    if _is_unc_path(path_str): return not _is_unc_path_accessible(path_str)
    try:
        drive = os.path.splitdrive(path_str)[0] + "\\"
        kernel32 = ctypes.windll.kernel32
        err_mode = kernel32.SetErrorMode(0x0001)
        res = kernel32.GetVolumeInformationW(drive, None, 0, None, None, None, None, 0)
        kernel32.SetErrorMode(err_mode)
        return res == 0
    except (OSError, AttributeError, TypeError, ctypes.ArgumentError): return True

@lru_cache(maxsize=128)
def _get_drive_type(root: str) -> int:
    """Obtiene el tipo de unidad."""
    try: return ctypes.windll.kernel32.GetDriveTypeW(root)
    except (OSError, AttributeError, ctypes.ArgumentError): return DRIVE_UNKNOWN

@lru_cache(maxsize=128)
def _is_volume_removable_media(path_str: Optional[str]) -> bool:
    """Determina si la ruta reside en medios extraíbles."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    if _is_unc_path(path_str): return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        return _get_drive_type(root) == DRIVE_REMOVABLE
    except (AttributeError, OSError, ctypes.ArgumentError): pass
    return False

@lru_cache(maxsize=128)
def _is_volume_compressed_or_encrypted(path_str: Optional[str]) -> bool:
    """Verifica si el volumen soporta compresión o cifrado."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    if _is_unc_path(path_str): return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        if not os.path.exists(root): return False
        flags = ctypes.c_ulong(0)
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
            return bool(flags.value & (0x10 | 0x20000))
    except (AttributeError, OSError, ctypes.ArgumentError): pass
    return False

@lru_cache(maxsize=2048)
def _is_kernel_managed(path_str: str) -> bool:
    """Determina si un archivo es crítico para la estabilidad del SO."""
    if not isinstance(path_str, str) or not path_str: return False
    p_lower = path_str.lower()
    if p_lower in _KERNEL_BLOCKED_FILES: return True
    if any(p_lower.endswith(f) for f in _KERNEL_BLOCKED_FILES): return True
    if os.name == 'nt':
        local_app_data = os.environ.get("LOCALAPPDATA", "").lower()
        app_data = os.environ.get("APPDATA", "").lower()
        if (local_app_data and p_lower.startswith(local_app_data)) or \
           (app_data and p_lower.startswith(app_data)): return True
    return any(d in p_lower for d in _KERNEL_BLOCKED_DIRS)

@lru_cache(maxsize=1024)
def _is_sensitive_extension(ext: str) -> bool:
    """Retorna True si la extensión es crítica."""
    return isinstance(ext, str) and ext.lower() in SENSITIVE_EXTENSIONS

def _rule(reason: ProtectionReason, predicate: ViolationPredicate) -> _IntegrityCheck:
    """Helper para crear reglas de integridad."""
    return _IntegrityCheck(reason, predicate)

def _validate_junction_depth(path: Path) -> None:
    """Evita la recursión infinita validando la profundidad de los reparse points."""
    count = 0
    curr = path
    while curr.is_symlink() or _is_system_directory_junction(str(curr)):
        count += 1
        if count > MAX_REPARSE_DEPTH:
            raise UnsafePathError("Profundidad de reparse point excesiva.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.INTEGRITY)
        try:
            curr = _get_final_path_normalized(curr)
        except (OSError, PermissionError, RuntimeError):
            break

def _validate_drive_restrictions(target_path: Path) -> None:
    """Aplica restricciones a nivel de volumen."""
    path_s = str(target_path)
    anchor = target_path.anchor
    if not anchor: return

    is_unc = _is_unc_path(path_s)
    
    try:
        serial = _get_volume_serial(anchor)
        if not serial and not is_unc:
            raise UnsafePathError("Volumen sin serial detectable (inestable).", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.ENVIRONMENT)
            
        restrictions = [
            (_is_volume_shadow_copy(path_s), "Volumen de instantánea bloqueado.", SafetyValidationErrorCode.VOLUME_SHADOW_COPY),
            (_is_volume_busy_or_invalid(path_s), "Volumen inaccesible.", SafetyValidationErrorCode.IO_ERROR),
            (not is_unc and _get_drive_type(anchor) == DRIVE_REMOTE, "Unidad de red bloqueada.", SafetyValidationErrorCode.IO_ERROR),
            (not is_unc and _get_drive_type(anchor) == DRIVE_REMOVABLE, "Unidad extraíble bloqueada.", SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED),
            (not is_unc and (_get_drive_type(anchor) == DRIVE_CDROM or _is_volume_readonly(path_s)), "Volumen de solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY),
            (not is_unc and _is_volume_compressed_or_encrypted(path_s), "Volumen cifrado o comprimido.", SafetyValidationErrorCode.VOLUME_RESTRICTED),
            (not is_unc and _is_virtual_drive(path_s), "Unidad virtual bloqueada.", SafetyValidationErrorCode.VIRTUAL_DRIVE_DETECTED)
        ]
        
        for condition, msg, code in restrictions:
            if condition:
                raise UnsafePathError(msg, code, SafetyValidationCategory.ENVIRONMENT)
    except (AttributeError, TypeError, OSError):
        raise UnsafePathError("Error validando restricciones de volumen.", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.ENVIRONMENT)

_VALIDATOR_MAP: Final[dict[ProtectionReason, ViolationPredicate]] = {
    ProtectionReason.SYMLINK: lambda p, st, sd: p.is_symlink(),
    ProtectionReason.REPARSE_POINT: lambda p, st, sd: sd.is_reparse,
    ProtectionReason.KERNEL_LOCKED: lambda p, st, sd: _is_kernel_managed(str(p)),
    ProtectionReason.READ_ONLY: lambda p, st, sd: not bool(st.st_mode & stat.S_IWRITE) or sd.is_readonly,
    ProtectionReason.VOLUME_READ_ONLY: lambda p, st, sd: _is_volume_readonly(str(p)),
    ProtectionReason.REMOVABLE_DRIVE: lambda p, st, sd: _is_volume_removable_media(str(p)),
    ProtectionReason.VOLUME_RESTRICTED: lambda p, st, sd: _is_volume_compressed_or_encrypted(str(p)),
    ProtectionReason.IN_USE: lambda p, st, sd: sd.is_in_use,
    ProtectionReason.SYSTEM_HIDDEN: lambda p, st, sd: sd.is_protected_system,
    ProtectionReason.OFFLINE: lambda p, st, sd: sd.has_flag(Win32Attr.OFFLINE),
    ProtectionReason.ENCRYPTED_OR_COMPRESSED: lambda p, st, sd: sd.has_flag(Win32Attr.COMPRESSED) or sd.has_flag(Win32Attr.ENCRYPTED),
    ProtectionReason.SPARSE_FILE: lambda p, st, sd: sd.has_flag(Win32Attr.SPARSE_FILE),
    ProtectionReason.HARD_LINK: lambda p, st, sd: p.is_file() and st.st_nlink > 1,
    ProtectionReason.ADS: lambda p, st, sd: _has_alternate_data_stream(p.name),
    ProtectionReason.EMPTY_FILE: lambda p, st, sd: False,
    ProtectionReason.MOUNT_POINT: lambda p, st, sd: os.path.ismount(p),
    ProtectionReason.INVALID_TYPE: lambda p, st, sd: not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)),
    ProtectionReason.SYSTEM_OWNER: lambda p, st, sd: _is_file_owned_by_system(str(p)),
    ProtectionReason.VIRTUAL_DRIVE: lambda p, st, sd: _is_virtual_drive(str(p)),
    ProtectionReason.SHADOW_COPY: lambda p, st, sd: _is_volume_shadow_copy(str(p)),
    ProtectionReason.INVALID_PATH_TYPE: lambda p, st, sd: bool(sd.attrs & (Win32Attr.OFFLINE)) and not p.is_file(),
}

_VALIDATORS: Final[list[_IntegrityCheck]] = [_rule(r, p) for r, p in _VALIDATOR_MAP.items()]

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
    ProtectionReason.KERNEL_LOCKED: SafetyValidationErrorCode.KERNEL_LOCKED_FILE,
    ProtectionReason.ACCESS_WRITE: SafetyValidationErrorCode.WRITE_ACCESS_DENIED,
    ProtectionReason.MOUNT_POINT: SafetyValidationErrorCode.MOUNT_POINT_DETECTED,
    ProtectionReason.SPARSE_FILE: SafetyValidationErrorCode.SPARSE_FILE_DETECTED,
    ProtectionReason.EMPTY_FILE: SafetyValidationErrorCode.EMPTY_FILE,
    ProtectionReason.READ_ONLY: SafetyValidationErrorCode.WRITE_ACCESS_DENIED,
    ProtectionReason.SYSTEM_OWNER: SafetyValidationErrorCode.SYSTEM_OWNER_PROTECTION,
    ProtectionReason.VIRTUAL_DRIVE: SafetyValidationErrorCode.VIRTUAL_DRIVE_DETECTED,
    ProtectionReason.SHADOW_COPY: SafetyValidationErrorCode.VOLUME_SHADOW_COPY,
    ProtectionReason.INVALID_PATH_TYPE: SafetyValidationErrorCode.INVALID_PATH_TYPE,
}

def _evaluate_security_rules(state: SafetyState) -> None:
    """Verifica el estado del archivo contra el mapa de reglas de integridad."""
    if _is_cache_valid(_NEGATIVE_SECURITY_CACHE, state.file_id):
        raise UnsafePathError("Acceso bloqueado: ruta conocida como insegura.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.INTEGRITY)
    if _is_cache_valid(_SECURITY_CACHE, state.file_id):
        return

    for reason, predicate in _VALIDATOR_MAP.items():
        try:
            if predicate(state.path, state.stat_result, state.descriptor):
                if len(_NEGATIVE_SECURITY_CACHE) >= 1024: _NEGATIVE_SECURITY_CACHE.clear()
                _NEGATIVE_SECURITY_CACHE[state.file_id] = time.monotonic()
                code = _REASON_TO_CODE.get(reason, SafetyValidationErrorCode.GENERIC)
                raise UnsafePathError(f"Integridad comprometida: {reason.name}", code, SafetyValidationCategory.INTEGRITY)
        except (OSError, PermissionError, AttributeError, TypeError):
            continue
    
    if len(_SECURITY_CACHE) >= 1024: _SECURITY_CACHE.clear()
    _SECURITY_CACHE[state.file_id] = time.monotonic()

def _get_path_stat_robust(path: Path) -> os.stat_result:
    """Obtiene metadatos con reintentos."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not path.exists():
                raise FileNotFoundError(f"Archivo inexistente: {path}")
            if _is_device_file(path):
                raise UnsafePathError(f"Acceso a dispositivo bloqueado: {path.name}", SafetyValidationErrorCode.DEVICE_FILE_DETECTED, SafetyValidationCategory.STRUCTURE)
                
            st = path.stat()
            if os.name == 'nt':
                attrs = _get_file_attrs(str(path))
                if attrs != INVALID_FILE_ATTRIBUTES and (attrs & (Win32Attr.DIRECTORY | Win32Attr.REPARSE_POINT | Win32Attr.SPARSE_FILE)):
                    if not path.is_file() and not path.is_dir():
                        raise UnsafePathError(f"Estructura no válida o corrupta: {path.name}", SafetyValidationErrorCode.INVALID_PATH_TYPE, SafetyValidationCategory.STRUCTURE)
            
            return st
        except (PermissionError, OSError) as e:
            err_code = getattr(e, 'winerror', None)
            if isinstance(e, (PermissionError, OSError)) and (err_code in (ERROR_SHARING_VIOLATION, ERROR_DEVICE_NOT_AVAILABLE, ERROR_NETWORK_UNREACHABLE) or e.errno in (errno.ETIMEDOUT, errno.EBUSY, errno.EAGAIN)):
                if attempt < max_retries - 1:
                     time.sleep(0.05 * (2 ** attempt))
                     continue
            code = SafetyValidationErrorCode.ACCESS_DENIED if isinstance(e, PermissionError) else SafetyValidationErrorCode.IO_ERROR
            raise UnsafePathError(f"Error accediendo a metadatos: {path.name}", code, SafetyValidationCategory.INTEGRITY)
        except (AttributeError, TypeError, Exception) as e:
            raise UnsafePathError(f"Falla inesperada al leer metadatos: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY)
    raise UnsafePathError(f"Archivo inestable tras reintentos: {path.name}", SafetyValidationErrorCode.FILE_IN_USE, SafetyValidationCategory.INTEGRITY)

def _check_file_integrity(state: SafetyState) -> None:
    """Valida la integridad de la ruta detectando cambios tipo TOCTOU."""
    if _is_cache_valid(_NEGATIVE_SECURITY_CACHE, state.file_id):
        raise UnsafePathError("Acceso bloqueado: ruta conocida como insegura.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.INTEGRITY)
        
    if not state.path.exists():
        raise UnsafePathError("El archivo ya no existe (TOCTOU).", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY)
    
    try:
        current_stat = _get_path_stat_robust(state.path)
        current_id = _get_file_id(str(state.path))
        
        if state.file_id != (0, 0, 0, 0) and current_id != state.file_id:
            raise UnsafePathError(f"Manipulación de identidad detectada (TOCTOU): {state.path.name}", SafetyValidationErrorCode.TOCTOU_VIOLATION, SafetyValidationCategory.INTEGRITY)
        
        if stat.S_ISREG(state.stat_result.st_mode) != stat.S_ISREG(current_stat.st_mode):
            raise UnsafePathError(f"Cambio de tipo detectado (TOCTOU): {state.path.name}", SafetyValidationErrorCode.TOCTOU_VIOLATION, SafetyValidationCategory.INTEGRITY)
        
        _evaluate_security_rules(state)
    except (OSError, PermissionError, UnsafePathError) as e:
        if isinstance(e, UnsafePathError):
            raise
        raise UnsafePathError(f"Fallo crítico durante integridad: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY)

@lru_cache(maxsize=2048)
def _is_readonly(path_str: str) -> bool:
    """Verifica flag de solo lectura mediante acceso lógico."""
    if not isinstance(path_str, str) or not path_str: return True
    try:
        path = Path(path_str)
        if not path.exists(): return True
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError, FileNotFoundError, AttributeError, TypeError):
        return True

def _validate_access_permissions(path: Path) -> None:
    """Valida si el proceso tiene permisos de lectura y escritura."""
    if not isinstance(path, Path): raise UnsafePathError("Tipo de ruta inválido", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)
    try:
        if not path.exists():
            raise UnsafePathError("Archivo inexistente, no se pueden validar permisos.", SafetyValidationErrorCode.ACCESS_DENIED, SafetyValidationCategory.PERMISSION)
        if not os.access(path, os.R_OK):
            raise UnsafePathError("Permisos de lectura denegados.", SafetyValidationErrorCode.ACCESS_DENIED, SafetyValidationCategory.PERMISSION)
        if not os.access(path, os.W_OK):
            raise UnsafePathError("Permisos de escritura denegados.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED, SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
    except (OSError, PermissionError, FileNotFoundError, AttributeError):
        raise UnsafePathError("Error de acceso al validar permisos.", SafetyValidationErrorCode.ACCESS_DENIED, SafetyValidationCategory.PERMISSION)

def _validate_volume_integrity(path: Path) -> None:
    """Verifica aptitud del volumen para operaciones de escritura."""
    if os.name != 'nt': return
    try:
        _validate_drive_restrictions(path)
        if os.path.ismount(path):
            final = _get_final_path_normalized(path)
            if not final.drive == path.drive:
                 raise UnsafePathError(f"Punto de montaje en volumen externo: {path}", SafetyValidationErrorCode.MOUNT_POINT_DETECTED, SafetyValidationCategory.ENVIRONMENT)
            raise UnsafePathError(f"Punto de montaje bloqueado: {path}", SafetyValidationErrorCode.MOUNT_POINT_DETECTED, SafetyValidationCategory.ENVIRONMENT)
    except UnsafePathError:
        raise
    except (OSError, PermissionError, AttributeError, TypeError):
        raise UnsafePathError("Error validando integridad de volumen.", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.ENVIRONMENT)

@lru_cache(maxsize=4096)
def _normalize_resolved(path_str: str) -> Path:
    """Resuelve y normaliza una ruta con pre-verificación de integridad."""
    if not path_str or _is_device_path(path_str): return Path(path_str)
    try:
        p = Path(os.path.abspath(path_str))
        if os.name == 'nt' and p.parent.exists() and _is_system_directory_junction(str(p.parent)):
             raise UnsafePathError("Directorio padre es punto de unión.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.STRUCTURE)
        
        resolved = p.resolve(strict=False)
        if resolved.exists() and _is_device_file(resolved):
             raise UnsafePathError("Ruta resuelta apunta a dispositivo.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED, SafetyValidationCategory.STRUCTURE)
        return resolved
    except (OSError, RuntimeError, PermissionError, ValueError, TypeError, AttributeError):
        return Path(path_str).absolute()

@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Normaliza ruta y valida que no sufra path traversal."""
    if path is None: raise UnsafePathError("Ruta nula recibida.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)
    path_str = str(path).strip()
    
    if os.name == 'nt' and (path_str.endswith('.') or path_str.endswith(' ')):
        raise UnsafePathError("Ruta con terminación inválida.", SafetyValidationErrorCode.INVALID_CHARS, SafetyValidationCategory.STRUCTURE)
    
    if os.name == 'nt' and _is_path_too_long(path_str):
        if not path_str.startswith(LONG_PATH_PREFIX):
             raise UnsafePathError("Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG, SafetyValidationCategory.STRUCTURE)

    if _is_path_empty_or_whitespace(path_str): raise UnsafePathError("Entrada de ruta vacía o inválida.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)
    if unicodedata.normalize('NFKC', path_str) != path_str:
         raise UnsafePathError("Ruta contiene secuencias Unicode sospechosas.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING, SafetyValidationCategory.STRUCTURE)
    try:
        p = Path(path_str)
        try:
            anchor_path = p.anchor
            parts = p.parts
        except (AttributeError, ValueError, TypeError):
            raise UnsafePathError("Estructura de ruta inválida.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)
            
        if len(parts) > MAX_DIRECTORY_DEPTH:
            raise UnsafePathError("Profundidad de ruta excesiva.", SafetyValidationErrorCode.PATH_TOO_DEEP, SafetyValidationCategory.STRUCTURE)

        sub_path = anchor_path
        for part in parts:
            if part in (os.sep, os.altsep): continue
            if _is_spoofing_detected(part):
                raise UnsafePathError("Spoofing visual detectado en nombre de componente.", SafetyValidationErrorCode.INVALID_CHARS, SafetyValidationCategory.STRUCTURE)
            sub_path = os.path.join(sub_path, part)
            if os.path.exists(sub_path) and _is_system_directory_junction(sub_path):
                raise UnsafePathError("Segmento de ruta contiene punto de reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.STRUCTURE)
        
        if _is_device_file(p): raise UnsafePathError("Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED, SafetyValidationCategory.STRUCTURE)
        if _has_alternate_data_stream(p.name): raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED, SafetyValidationCategory.STRUCTURE)
        
        resolved = _normalize_resolved(path_str)
        if ".." in parts: raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS, SafetyValidationCategory.STRUCTURE)
        
        if resolved.exists():
            original_id = _get_file_id(path_str)
            resolved_id = _get_file_id(str(resolved))
            if original_id != (0,0,0,0) and resolved_id != original_id:
                raise UnsafePathError("Inconsistencia en resolución de ID (TOCTOU).", SafetyValidationErrorCode.TOCTOU_VIOLATION, SafetyValidationCategory.INTEGRITY)
        
        return resolved
    except (OSError, PermissionError, RuntimeError, ValueError, TypeError, AttributeError) as e:
        raise UnsafePathError(f"Error al normalizar ruta {path_str}: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.STRUCTURE)

def is_absolute_path_allowed(path: PathLike) -> bool:
    """Verifica si la ruta es absoluta."""
    try: return Path(path).is_absolute()
    except (TypeError, ValueError, AttributeError): return False

def is_drive_root(path: PathLike) -> bool:
    """Determina si la ruta apunta a la raíz de una unidad."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (UnsafePathError, TypeError, OSError, AttributeError): return True

@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """Evalúa si la ruta está dentro de directorios del sistema."""
    if path is None: return True
    path_str = str(path)
    if _is_cache_valid(_PROTECTION_CACHE, path_str):
        return _PROTECTION_CACHE[path_str][0]

    try:
        norm_p = _normalize_resolved(path_str)
        # Comparación mediante partes normalizadas de Path en vez de cadenas
        system_roots = _get_system_root_paths()
        
        is_protected = False
        norm_str_case = str(norm_p).lower()
        if any(norm_str_case.startswith(r.lower()) for r in system_roots):
            is_protected = True
        elif any(part.lower() in PROTECTED_DIR_NAMES for part in norm_p.parts):
            is_protected = True
        else:
            attrs = _get_file_attrs(str(norm_p))
            is_protected = (attrs != INVALID_FILE_ATTRIBUTES and (attrs & (Win32Attr.SYSTEM | Win32Attr.HIDDEN | Win32Attr.REPARSE_POINT))) or len(norm_p.parts) <= 1
        
        if len(_PROTECTION_CACHE) > 2048: _PROTECTION_CACHE.clear()
        _PROTECTION_CACHE[path_str] = (is_protected, time.monotonic())
        return is_protected
    except (OSError, AttributeError, ValueError, TypeError):
        return True

def _get_final_path_normalized(p: Path) -> Path:
    """Resuelve la ruta final evitando seguir enlaces de forma insegura."""
    if os.name != 'nt':
        return p.resolve()
    
    kernel32 = ctypes.windll.kernel32
    try:
        if not p.exists(): return p.resolve(strict=False)
        handle = kernel32.CreateFileW(
            _to_long_path(str(p)), 0, 0, None, 3, FILE_FLAG_BACKUP_SEMANTICS, None
        )
        if handle == INVALID_HANDLE_VALUE:
            return p.resolve(strict=False)
        
        try:
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.GetFinalPathNameByHandleW(handle, buf, 1024, 0) > 0:
                return Path(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, Exception):
        pass
    return p.resolve(strict=False)

@lru_cache(maxsize=4096)
def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica si 'child' es hijo de 'parent'."""
    if child is None or parent is None: return False
    try:
        c_path = normalize(child)
        p_path = normalize(parent)
        if is_drive_root(c_path) or is_protected_path(str(c_path)): return False
        
        final_c = _get_final_path_normalized(c_path)
        final_p = _get_final_path_normalized(p_path)
        
        if final_c.anchor != final_p.anchor: return False
        
        return final_c.is_relative_to(final_p) if allow_equal else (final_c.is_relative_to(final_p) and final_c != final_p)
    except (UnsafePathError, TypeError, OSError, RuntimeError, ValueError, AttributeError): return False

@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Detecta archivos con extensiones críticas."""
    if path is None: return True
    try: 
        ext = Path(path).suffix
        return _is_sensitive_extension(ext)
    except (TypeError, ValueError, OSError, AttributeError): return True 

def _is_device_path(path_str: str) -> bool:
    """Verifica si una cadena de texto corresponde a namespace de dispositivo."""
    return any(path_str.startswith(p) for p in ("\\\\.\\", "\\\\?\\", "//./"))

def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """Verifica que la estructura léxica de la ruta no sea maliciosa."""
    if _is_cache_valid(_STRUCTURAL_CACHE, path_string):
        return

    if not isinstance(path_string, str):
        raise UnsafePathError("Ruta no es texto.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)
    
    if len(target_path.name) > MAX_FILENAME_LENGTH:
        raise UnsafePathError("Nombre de archivo demasiado largo.", SafetyValidationErrorCode.FILENAME_TOO_LONG, SafetyValidationCategory.STRUCTURE)

    if _is_spoofing_detected(path_string):
        raise UnsafePathError("Secuencias de escape visual detectadas.", SafetyValidationErrorCode.INVALID_CHARS, SafetyValidationCategory.STRUCTURE)
    
    if _is_device_path(path_string) and not path_string.startswith(LONG_PATH_PREFIX):
        raise UnsafePathError("Acceso a Namespace de dispositivos prohibido.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED, SafetyValidationCategory.STRUCTURE)
        
    if _is_unc_path(path_string) and not _is_unc_path_accessible(path_string):
        raise UnsafePathError("Rutas UNC/Red bloqueadas o inaccesibles.", SafetyValidationErrorCode.UNC_PATH, SafetyValidationCategory.ENVIRONMENT)
        
    if ".." in path_string.split(os.sep):
        raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS, SafetyValidationCategory.STRUCTURE)
        
    if _is_path_too_long(path_string):
        raise UnsafePathError("Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG, SafetyValidationCategory.STRUCTURE)
        
    if "\0" in path_string:
        raise UnsafePathError("Inyección de carácter nulo.", SafetyValidationErrorCode.NULL_CHAR, SafetyValidationCategory.STRUCTURE)
        
    if _has_invalid_chars(path_string):
        raise UnsafePathError("Caracteres inválidos detectados.", SafetyValidationErrorCode.INVALID_CHARS, SafetyValidationCategory.STRUCTURE)
        
    if unicodedata.normalize('NFKC', path_string) != path_string:
        raise UnsafePathError("Codificación de caracteres sospechosa.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING, SafetyValidationCategory.STRUCTURE)
        
    if _has_alternate_data_stream(target_path.name):
        raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED, SafetyValidationCategory.STRUCTURE)
        
    if _is_device_file(target_path):
        raise UnsafePathError("Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED, SafetyValidationCategory.STRUCTURE)
        
    if _RESERVED_NAMES_PATTERN.fullmatch(target_path.name):
        raise UnsafePathError("Nombre de dispositivo reservado detectado.", SafetyValidationErrorCode.RESERVED_NAME, SafetyValidationCategory.STRUCTURE)

    _validate_path_depth_and_root_access(target_path)

    if len(_STRUCTURAL_CACHE) > 2048: _STRUCTURAL_CACHE.clear()
    _STRUCTURAL_CACHE[path_string] = (True, time.monotonic())

def _validate_path_depth_and_root_access(target_path: Path) -> None:
    """Verifica límites de profundidad y previene operaciones en la raíz."""
    try:
        t_parts = target_path.parts
        if target_path.exists() and not target_path.is_absolute():
            raise UnsafePathError("Ruta inconsistente con el sistema.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)
        if not t_parts or (len(t_parts) == 1 and t_parts[0] == os.sep):
             raise UnsafePathError("Ruta raíz no permitida.", SafetyValidationErrorCode.ROOT_ACCESS, SafetyValidationCategory.STRUCTURE)
        if len(t_parts) == 1 and target_path == Path(target_path.anchor):
             raise UnsafePathError("Operación sobre raíz de unidad prohibida.", SafetyValidationErrorCode.ROOT_ACCESS, SafetyValidationCategory.STRUCTURE)
        if len(t_parts) > MAX_DIRECTORY_DEPTH:
             raise UnsafePathError("Profundidad de directorio excesiva.", SafetyValidationErrorCode.PATH_TOO_DEEP, SafetyValidationCategory.STRUCTURE)
    except (AttributeError, TypeError, ValueError):
        raise UnsafePathError("Estructura de ruta inválida.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)

def _validate_security_descriptor_attributes(sd: SecurityDescriptor) -> None:
    """Valida atributos de seguridad contra políticas defensivas."""
    if sd.is_protected_system:
        raise UnsafePathError("Protección defensiva: Archivo de sistema oculto/protegido.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH, SafetyValidationCategory.PERMISSION)
    if sd.has_flag(Win32Attr.OFFLINE):
        raise UnsafePathError("Protección defensiva: Archivo offline.", SafetyValidationErrorCode.OFFLINE_FILE, SafetyValidationErrorCode.INTEGRITY)
    if sd.is_reparse:
        raise UnsafePathError("Protección defensiva: Redirección no autorizada.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationErrorCode.INTEGRITY)
    if sd.has_flag(Win32Attr.SPARSE_FILE):
        raise UnsafePathError("Protección defensiva: Archivo disperso no permitido.", SafetyValidationErrorCode.SPARSE_FILE_DETECTED, SafetyValidationErrorCode.INTEGRITY)

def _check_container_permissions(target_path: Path) -> None:
    """Verifica permisos del contenedor (padre) antes de operar."""
    try:
        parent = target_path.parent
        if parent.exists():
            if _is_system_directory_junction(str(parent)):
                raise UnsafePathError("Protección: No se puede operar dentro de un punto de reparseo.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.INTEGRITY)
            if parent == Path(parent.anchor) or os.path.ismount(parent):
                raise UnsafePathError("Protección: No se puede operar en la raíz o punto de montaje.", SafetyValidationErrorCode.ROOT_ACCESS, SafetyValidationCategory.PERMISSION)
            if not os.access(parent, os.W_OK):
                raise UnsafePathError("Directorio contenedor sin permisos de escritura.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED, SafetyValidationCategory.PERMISSION)
    except (OSError, PermissionError, AttributeError):
        pass

def _validate_preconditions(ctx: PathContext) -> None:
    """Validación exhaustiva de precondiciones para autorizar acceso."""
    target_path = ctx.path
    if not is_absolute_path_allowed(target_path):
        raise UnsafePathError("Solo se permiten rutas absolutas.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED, SafetyValidationCategory.STRUCTURE)
    
    if _is_device_path(ctx.path_str):
        raise UnsafePathError("Prohibido uso de rutas de dispositivo en modificación.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED, SafetyValidationCategory.PERMISSION)
    
    _check_container_permissions(target_path)

    try:
        app_path = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(os.getcwd()).resolve()
        if target_path.resolve().is_relative_to(app_path):
            raise UnsafePathError("Protección: No se puede modificar el directorio de la aplicación.", SafetyValidationErrorCode.APP_DIRECTORY_VIOLATION, SafetyValidationCategory.PERMISSION)
    except (ValueError, TypeError, AttributeError, RuntimeError):
        pass

    if is_running_as_admin():
        if _is_file_owned_by_system(ctx.path_str) or _is_kernel_managed(ctx.path_str):
            raise UnsafePathError("Operación denegada en modo privilegiado.", SafetyValidationErrorCode.ADMIN_PRIVILEGE_VIOLATION, SafetyValidationCategory.PERMISSION)

    try:
        if os.name == 'nt' and _is_system_directory_junction(ctx.path_str):
             final_path = _get_final_path_normalized(target_path)
             if not final_path.drive == target_path.drive:
                  raise UnsafePathError("Redirección de reparse point hacia otro volumen.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.INTEGRITY)

        file_id = _get_file_id(ctx.path_str)
        if _is_cache_valid(_NEGATIVE_SECURITY_CACHE, file_id):
            raise UnsafePathError("Acceso bloqueado: ruta conocida como insegura.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.INTEGRITY)

        if ctx.guard.is_locked():
            raise UnsafePathError("Archivo bloqueado por otro proceso o sistema.", SafetyValidationErrorCode.FILE_IN_USE, SafetyValidationCategory.INTEGRITY)

        sd = ctx.guard.get_descriptor()
        if sd.attrs != INVALID_FILE_ATTRIBUTES:
            _validate_security_descriptor_attributes(sd)
            
        try:
            parent = target_path.parent
            if parent.exists():
                if os.name == 'nt' and _is_volume_readonly(ctx.path_str):
                    raise UnsafePathError("Directorio contenedor en volumen de solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY, SafetyValidationCategory.ENVIRONMENT)
        except (OSError, PermissionError, AttributeError):
            pass
        
        if _is_kernel_managed(ctx.path_str):
            raise UnsafePathError(f"Archivo de sistema crítico: {target_path.name}", SafetyValidationErrorCode.KERNEL_LOCKED_FILE, SafetyValidationCategory.PERMISSION)
        
        _validate_volume_integrity(target_path)
    except UnsafePathError:
        raise
    except (OSError, PermissionError, ValueError, AttributeError, Exception) as e:
        raise UnsafePathError(f"Error accediendo a precondiciones: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY)

def _validate_boundary_conditions(target_path: Path, root_directory: Optional[PathLike]) -> None:
    """Valida límites de la operación frente al alcance permitido."""
    if root_directory:
        try:
            rd_path = Path(root_directory)
            if not rd_path.is_absolute():
                raise UnsafePathError("El directorio base debe ser absoluto.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED, SafetyValidationCategory.STRUCTURE)
            if not is_within_directory(target_path, rd_path, allow_equal=True):
                raise UnsafePathError("Fuera de alcance permitido.", SafetyValidationErrorCode.OUT_OF_BOUNDS, SafetyValidationCategory.STRUCTURE)
        except (TypeError, ValueError, OSError, AttributeError):
            raise UnsafePathError("Directorio base inválido.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)

    if is_protected_path(str(target_path)):
        raise UnsafePathError("Ruta en directorio del sistema bloqueada.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH, SafetyValidationCategory.PERMISSION)
    
    try:
        app_root = _normalize_resolved(os.getcwd())
        if target_path == app_root or app_root in target_path.parents:
            raise UnsafePathError("Modificación de App denegada.", SafetyValidationErrorCode.OUT_OF_BOUNDS, SafetyValidationCategory.PERMISSION)
    except (OSError, RuntimeError, ValueError, AttributeError): pass
    if is_drive_root(target_path):
        raise UnsafePathError("Acceso a raíz denegado.", SafetyValidationErrorCode.ROOT_ACCESS, SafetyValidationCategory.STRUCTURE)

def _validate_ntfs_reparse_redirection(path: Path, base_dir: Optional[Path]) -> None:
    """Analiza recursivamente reparse points."""
    for p in [path] + list(path.parents):
        if _is_system_directory_junction(str(p)):
            try:
                _validate_junction_depth(p)
                final_path = _get_final_path_normalized(p)
                if base_dir and not final_path.is_relative_to(base_dir):
                    raise UnsafePathError(f"Redirección insegura en reparse point: {p}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.INTEGRITY)
            except (OSError, PermissionError, AttributeError):
                continue

def _validate_path_components(path: Path) -> None:
    """Verifica componentes buscando spoofings o ciclos referenciales."""
    resolved_path = _get_final_path_normalized(path)
    anchor = Path(resolved_path.anchor)
    parts = resolved_path.parts
    visited: set[Path] = set()

    if len(parts) > MAX_DIRECTORY_DEPTH:
        raise UnsafePathError("Ruta demasiado profunda.", SafetyValidationErrorCode.PATH_TOO_DEEP, SafetyValidationCategory.STRUCTURE)

    for i in range(1, len(parts) + 1):
        node = anchor.joinpath(*parts[:i])
        _verify_path_integrity_at_node(node, visited)
        
    _validate_no_reparse_traversal(path)

def _verify_path_integrity_at_node(node: Path, visited: set[Path]) -> None:
    """Valida la integridad de un nodo individual."""
    if _is_spoofing_detected(node.name):
        raise UnsafePathError(f"Spoofing de nombres detectado en: {node.name}", SafetyValidationErrorCode.INVALID_CHARS, SafetyValidationCategory.STRUCTURE)
    
    try:
        resolved = node.resolve()
        if resolved in visited:
            raise UnsafePathError(f"Referencia circular detectada: {node}", SafetyValidationErrorCode.CIRCULAR_REFERENCE, SafetyValidationCategory.INTEGRITY)
        visited.add(resolved)
        
        if _is_system_directory_junction(str(node)):
            if _get_final_path_normalized(node) is None:
                raise UnsafePathError(f"Punto de reparse inaccesible: {node}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.INTEGRITY)
    except (OSError, RuntimeError, PermissionError, AttributeError):
        pass

def _validate_no_reparse_traversal(path: Path) -> None:
    """Asegura que el traversal de la ruta no resulte en escape de volumen."""
    root_anchor = Path(path.anchor)
    current = root_anchor
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink() or _is_system_directory_junction(str(current)):
            _validate_junction_depth(current)
            target = _get_final_path_normalized(current)
            if not target.is_relative_to(root_anchor):
                raise UnsafePathError(f"Escape inseguro detectado: {current}", SafetyValidationErrorCode.OUT_OF_BOUNDS, SafetyValidationCategory.INTEGRITY)

def _validate_file_operations_security(ctx: PathContext, allow_sensitive: bool, base: Optional[Path]) -> None:
    """Inspección final de seguridad antes de autorizar escritura."""
    p = ctx.path
    if not p.exists():
        raise UnsafePathError(f"El archivo fue eliminado: {p.name}", SafetyValidationErrorCode.ACCESS_DENIED, SafetyValidationCategory.INTEGRITY)
    
    if p.is_file() and p.stat().st_size > MAX_FILE_SIZE:
        raise UnsafePathError(f"Archivo demasiado grande para procesar: {p.name}", SafetyValidationErrorCode.FILE_TOO_LARGE, SafetyValidationCategory.INTEGRITY)
    
    _validate_access_permissions(p)
    if not os.access(p, os.W_OK):
        raise UnsafePathError(f"Permisos insuficientes de escritura: {p.name}", SafetyValidationErrorCode.WRITE_ACCESS_DENIED, SafetyValidationCategory.PERMISSION)

    if ctx.guard.is_locked():
        raise UnsafePathError(f"Archivo en uso: {p.name}", SafetyValidationErrorCode.FILE_IN_USE, SafetyValidationCategory.INTEGRITY)
    
    try:
        state = SafetyState(
            path=p,
            descriptor=ctx.guard.get_descriptor(),
            stat_result=_get_path_stat_robust(p),
            file_id=_get_file_id(ctx.path_str)
        )
        
        if os.name == 'nt': 
            _validate_ntfs_reparse_redirection(p, base)
        _check_file_integrity(state)
    except (OSError, PermissionError, UnsafePathError, AttributeError, Exception) as e:
        if isinstance(e, UnsafePathError):
            raise
        raise UnsafePathError(f"Validación de seguridad fallida: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY)

def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> Path:
    """Valida que una ruta pueda ser modificada de forma segura."""
    if path is None:
        raise UnsafePathError("Entrada nula recibida para modificación.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)
        
    result = _execute_safety_pipeline(path, allow_sensitive=allow_sensitive, base_dir=base_dir)
    if not result.is_valid:
        raise result.error
    return Path(path)

def _execute_safety_pipeline(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> PathValidationResult:
    """Pipeline de validación centralizado."""
    if path is None:
        return PathValidationResult(False, UnsafePathError("Ruta nula recibida.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE))
        
    try:
        path_str = str(path).strip()
        if _is_spoofing_detected(path_str):
            return PathValidationResult(False, UnsafePathError("Secuencias de control visual bloqueadas.", SafetyValidationErrorCode.INVALID_CHARS, SafetyValidationCategory.STRUCTURE))
        
        p = normalize(path_str)
        ctx = PathContext(p, str(p), SafetyGuard(p))
        
        if _is_kernel_managed(ctx.path_str):
            return PathValidationResult(False, UnsafePathError(f"Protección: Modificación de archivo de sistema crítico bloqueada: {p.name}", SafetyValidationErrorCode.KERNEL_LOCKED_FILE, SafetyValidationCategory.PERMISSION))
        
        if not allow_sensitive and is_sensitive_file(p):
            return PathValidationResult(False, UnsafePathError(f"Extensión bloqueada '{p.suffix}'.", SafetyValidationErrorCode.SENSITIVE_EXTENSION, SafetyValidationCategory.STRUCTURE))
        
        _validate_path_components(p)
        _validate_structural_safety(p, ctx.path_str)
        
        base = None
        if base_dir:
            try: base = _normalize_resolved(str(base_dir))
            except (TypeError, ValueError, OSError, AttributeError) as e:
                return PathValidationResult(False, UnsafePathError(f"Directorio base inválido: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.STRUCTURE))
        
        _validate_boundary_conditions(p, base_dir)
        _validate_preconditions(ctx)
        
        if p.exists():
            if os.name == 'nt': _validate_ntfs_reparse_redirection(p, base)
            _validate_file_operations_security(ctx, allow_sensitive, base)
        else:
            if p.parent.exists() and not os.access(p.parent, os.W_OK):
                return PathValidationResult(False, UnsafePathError("Directorio contenedor sin permisos de escritura.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED, SafetyValidationCategory.PERMISSION))
        
        return PathValidationResult(True)
    except UnsafePathError as e:
        return PathValidationResult(False, e)
    except (OSError, PermissionError, RuntimeError, ValueError, TypeError, AttributeError) as e:
        return PathValidationResult(False, UnsafePathError(f"Falla inesperada en pipeline: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY))

def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> bool:
    """Booleano de conveniencia para validar seguridad."""
    if path is None: return False
    return _execute_safety_pipeline(path, allow_sensitive=allow_sensitive, base_dir=base_dir).is_valid

def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> list[Path]:
    """Filtra una lista de rutas preservando solo las seguras."""
    results: list[Path] = []
    if paths is None: return results
    for p in paths:
        if p is None: continue
        try: results.append(ensure_safe_to_modify(p, allow_sensitive=allow_sensitive, base_dir=base_dir))
        except (UnsafePathError, ValueError, TypeError, OSError, PermissionError, AttributeError): continue
    return results

def describe_protection(path: PathLike) -> str:
    """Genera una descripción humana del estado de seguridad."""
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        guard = SafetyGuard(p)
    except (UnsafePathError, TypeError, ValueError, AttributeError): return "Ruta mal formada."
    
    if _is_unc_path(guard.path_str): return f"'{guard.path_str}' es ruta de red."
    if _is_device_file(p): return f"'{guard.path_str}' es un archivo de dispositivo."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(str(p)): return f"'{p}' protegida por sistema."
    
    try:
        if p.exists():
            sd = guard.get_descriptor()
            diagnostics = {
                "enlace simbólico": p.is_symlink(),
                "punto de reparse": sd.is_reparse,
                "punto de montaje": os.path.ismount(p),
                "unidad virtual mapeada": _is_virtual_drive(guard.path_str),
                "atributo de solo lectura": sd.is_readonly,
                "solo lectura (permisos)": _is_readonly(guard.path_str),
                "volumen de solo lectura": _is_volume_readonly(guard.path_str),
                "volumen extraíble": _is_volume_removable_media(guard.path_str),
                "volumen cifrado/comprimido": _is_volume_compressed_or_encrypted(guard.path_str),
                "archivo en uso": sd.is_in_use,
                "archivo cifrado o comprimido": sd.has_flag(Win32Attr.COMPRESSED) or sd.has_flag(Win32Attr.ENCRYPTED),
                "archivo disperso (sparse)": sd.has_flag(Win32Attr.SPARSE_FILE),
                "archivo offline/nube": sd.has_flag(Win32Attr.OFFLINE),
                "atributo oculto/sistema": sd.is_protected_system,
                "contiene ADS": _has_alternate_data_stream(p.name),
                "tamaño excesivo": p.is_file() and p.stat().st_size > MAX_FILE_SIZE,
                "detectado como hard link": p.is_file() and p.stat().st_nlink > 1,
                "volumen de instantánea (VSS)": _is_volume_shadow_copy(guard.path_str)
            }
            for reason, is_active in diagnostics.items():
                if is_active: return f"'{p}' es {reason}."
            
            if not (p.is_file() or p.is_dir()): return f"'{p}' tipo de objeto no soportado."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío (potencialmente crítico)."
            
    except (OSError, FileNotFoundError, AttributeError): pass
    
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
