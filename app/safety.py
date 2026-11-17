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
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, Optional, TypedDict, Any
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
FILE_SHARE_READ: Final[int] = 0x00000001
FILE_SHARE_WRITE: Final[int] = 0x00000002
FILE_SHARE_DELETE: Final[int] = 0x00000004
GetFileExInfoStandard: Final[int] = 0

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

# Cachés para optimización de rendimiento: TTL de 5s y límites ajustados
_SECURITY_CACHE: Final[dict[tuple[int, int, int, int], float]] = {}
_NEGATIVE_SECURITY_CACHE: Final[dict[tuple[int, int, int, int], float]] = {}
_STRUCTURAL_CACHE: Final[dict[str, tuple[bool, float]]] = {}
_FILE_ID_CACHE: Final[dict[str, tuple[tuple[int, int, int, int], float]]] = {}
_ATTR_CACHE: Final[dict[str, tuple[int, float]]] = {}
_PROTECTION_CACHE: Final[dict[str, tuple[bool, float]]] = {}
_VOLUME_SERIAL_CACHE: Final[dict[tuple[int, float]]] = {}

def _is_cache_valid(cache_map: dict[Any, Any], key: Any, ttl: float = 5.0) -> bool:
    """Verifica si la entrada en caché existe y está dentro del periodo de validez."""
    if key not in cache_map: 
        return False
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
    de metadatos de seguridad con caché interno para evitar syscalls redundantes.
    """
    def __init__(self, target_path: Path):
        self.path = target_path
        self.path_str = str(target_path)
        self.is_nt = os.name == 'nt'
        self._descriptor_cache: Optional[SecurityDescriptor] = None

    def is_locked(self) -> bool:
        """Determina si el SO mantiene un bloqueo sobre la ruta (exclusivo o sistema)."""
        if not self.is_nt or not self.path_str: 
            return False
        try:
            return not _is_file_accessible(self.path_str) or _is_file_in_use_by_system(self.path_str)
        except (OSError, FileNotFoundError, AttributeError, TypeError):
            return True

    def get_descriptor(self) -> SecurityDescriptor:
        """Crea una instantánea inmutable de las propiedades de seguridad de la ruta."""
        if self._descriptor_cache is None:
            self._descriptor_cache = SecurityDescriptor.from_path(self.path_str)
        return self._descriptor_cache

def _is_file_accessible(path_str: str) -> bool:
    """Verifica si un archivo permite apertura compartida mediante Win32 API con reintentos."""
    if os.name != 'nt' or not path_str: 
        return True
    kernel32 = ctypes.windll.kernel32
    long_path = _to_long_path(path_str)
    
    # Suprimir diálogos de sistema (ej. "No hay disco")
    old_error_mode = kernel32.SetErrorMode(0x0001 | 0x8000)
    try:
        for attempt in range(3):
            handle = kernel32.CreateFileW(
                long_path, 
                0, 
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None, 
                3, # OPEN_EXISTING
                FILE_FLAG_BACKUP_SEMANTICS, 
                None
            )
            if handle != INVALID_HANDLE_VALUE:
                kernel32.CloseHandle(handle)
                return True
            
            err = kernel32.GetLastError()
            if err == ERROR_SHARING_VIOLATION:
                time.sleep(0.05 * (2 ** attempt))
                continue
            return False
    finally:
        kernel32.SetErrorMode(old_error_mode)
    return False

class FILE_ID_128(ctypes.Structure):
    """Estructura de bajo nivel para identificadores de archivo de 128 bits."""
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

class FILE_ID_INFO(ctypes.Structure):
    """Información de identificación de archivo para consultas NTFS."""
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_uint64),
        ("FileId", FILE_ID_128),
    ]

class WIN32_FILE_ATTRIBUTE_DATA(ctypes.Structure):
    """Datos de atributos de archivo para llamadas nativas."""
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", ctypes.c_uint64),
        ("ftLastAccessTime", ctypes.c_uint64),
        ("ftLastWriteTime", ctypes.c_uint64),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
    ]

class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    """Información extendida recuperada mediante un handle de archivo."""
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
    """Obtiene el número de serie de volumen del sistema de archivos."""
    if os.name != 'nt' or not root_path: 
        return 0
    if _is_cache_valid(_VOLUME_SERIAL_CACHE, root_path):
        return _VOLUME_SERIAL_CACHE[root_path][0]
    
    serial = ctypes.c_ulong(0)
    kernel32 = ctypes.windll.kernel32
    old_error_mode = kernel32.SetErrorMode(0x0001 | 0x8000)
    try:
        if kernel32.GetVolumeInformationW(root_path, None, 0, ctypes.byref(serial), None, None, None, 0):
            val = serial.value
            _VOLUME_SERIAL_CACHE[root_path] = (val, time.monotonic())
            return val
    except Exception: 
        pass
    finally:
        kernel32.SetErrorMode(old_error_mode)
    return 0

def _get_file_id(path_str: str) -> tuple[int, int, int, int]:
    """Genera un identificador único (Volumen + FileID) optimizado mediante stats."""
    if not isinstance(path_str, str) or not path_str or path_str.upper().startswith(("\\\\.\\", "//./")):
        return (0, 0, 0, 0)
    
    if _is_cache_valid(_FILE_ID_CACHE, path_str):
        return _FILE_ID_CACHE[path_str][0]
    
    res: tuple[int, int, int, int] = (0, 0, 0, 0)
    try:
        if os.name != 'nt':
            st = os.stat(path_str)
            res = (st.st_dev, st.st_ino, 0, 0)
        else:
            st = os.stat(path_str)
            res = (st.st_dev, st.st_ino, 0, 0)
    except Exception:
        pass
                
    if len(_FILE_ID_CACHE) >= 2048: 
        _FILE_ID_CACHE.clear()
    _FILE_ID_CACHE[path_str] = (res, time.monotonic())
    return res

class SafetyAction(Enum):
    """Define el tipo de permiso requerido para una operación."""
    READ = auto()
    MODIFY = auto()

class Win32Attr(IntEnum):
    """Flags de atributos de archivo definidos por el kernel de Windows."""
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
    """Estructura inmutable que encapsula las propiedades de seguridad de un nodo del sistema."""
    attrs: int
    is_protected_system: bool
    is_in_use: bool
    is_readonly: bool
    is_reparse: bool
    
    def has_flag(self, flag: Win32Attr) -> bool:
        """Verifica si un atributo Win32 específico está activo en la máscara."""
        return bool(self.attrs & flag)

    @classmethod
    @lru_cache(maxsize=1024)
    def from_path(cls, path_str: str) -> SecurityDescriptor:
        """Constructor de fábrica que analiza atributos y estado de seguridad del sistema."""
        if not isinstance(path_str, str) or not path_str:
            return cls(0, True, True, True, True)
        try:
            attrs = _get_file_attrs(path_str)
            if attrs == INVALID_FILE_ATTRIBUTES:
                return cls(0, True, True, True, True)
                
            risk_flags = (Win32Attr.HIDDEN | Win32Attr.SYSTEM | Win32Attr.OFFLINE | 
                          Win32Attr.TEMPORARY | Win32Attr.REPARSE_POINT)
            is_protected = bool(attrs & risk_flags) or _is_file_owned_by_system(path_str)
            is_in_use = is_protected or not _is_file_accessible(path_str)
            
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
    """Representación tipada de los metadatos de seguridad recuperados."""
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

@lru_cache(maxsize=4096)
def _to_long_path(path_str: str) -> str:
    """Habilita el acceso a rutas largas (hasta 32k caracteres) mediante el prefijo UNC."""
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
    """Valida si la longitud de la ruta excede el límite estándar de Windows."""
    return isinstance(path_str, str) and len(path_str) > MAX_PATH_LENGTH

def _get_file_attrs(path_str: Optional[str]) -> int:
    """Recupera los atributos de archivo mediante la llamada GetFileAttributesW."""
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
        if len(_ATTR_CACHE) > 4096: _ATTR_CACHE.clear()
        _ATTR_CACHE[path_str] = (attrs, time.monotonic())
        return attrs
    except (AttributeError, ctypes.ArgumentError, OSError, ValueError, Exception):
        return 0

class UnsafePathError(Exception):
    """Excepción base para cualquier violación de las políticas de seguridad del sistema."""
    def __init__(self, message: str, code: SafetyValidationErrorCode = SafetyValidationErrorCode.GENERIC, category: SafetyValidationCategory = SafetyValidationCategory.STRUCTURE):
        super().__init__(f"[{category.name}] [{code.name}] {message}")
        self.code = code
        self.category = category

class ProtectionReason(Enum):
    """Razones enumeradas por las cuales un objeto ha sido marcado como protegido."""
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
    """Define el contexto lógico de la validación: integridad de datos vs estructura."""
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
def _get_system_root_paths() -> tuple[Path, ...]:
    """Obtiene y normaliza rutas raíz del sistema mediante variables de entorno."""
    paths = []
    for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA"):
        env_val = os.environ.get(v)
        if env_val:
            try:
                paths.append(Path(env_val).resolve())
            except (OSError, RuntimeError):
                continue
    return tuple(paths)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)

_KERNEL_BLOCKED_FILES: Final[frozenset[str]] = frozenset(
    ("pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp", "memory.dmp")
)

_KERNEL_BLOCKED_DIRS: Final[frozenset[str]] = frozenset(("config.msi", "installer"))

class _IntegrityCheck(NamedTuple):
    """Representa una regla de integridad y su predicado lógico de validación."""
    reason: ProtectionReason
    predicate: ViolationPredicate

class _CheckResult(NamedTuple):
    """Resultado de una comprobación de seguridad individual."""
    is_safe: bool
    reason: Optional[ProtectionReason] = None

@lru_cache(maxsize=1)
def is_running_as_admin() -> bool:
    """Verifica la elevación de privilegios del proceso actual."""
    if os.name != 'nt':
        try: return os.geteuid() == 0
        except (AttributeError, OSError): return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, ctypes.ArgumentError, Exception):
        return False

@lru_cache(maxsize=64)
def _is_virtual_drive(path_str: str) -> bool:
    """Detección de unidades virtuales mapeadas a nivel de sistema mediante query de dispositivo."""
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
    """Verifica si la ruta reside en una instantánea VSS (Volume Shadow Copy Service)."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    if not path_str.startswith(("\\\\?\\", "\\")): return False
    return "\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy" in path_str.upper()

def _is_file_owned_by_system(path_str: str) -> bool:
    """Determina si el propietario (SID) del archivo es la entidad SYSTEM."""
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
    """Filtra cadenas de ruta con caracteres ASCII/Unicode no permitidos."""
    if not isinstance(path_str, str) or not path_str: return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E\u206A-\u206F]', path_str))

def _is_spoofing_detected(text: str) -> bool:
    """Detecta secuencias Unicode bi-direccionales usadas para ofuscación de nombres."""
    return bool(re.search(r'[\u200E\u200F\u202A-\u202E\u206A-\u206F]', text))

def _is_path_empty_or_whitespace(path_str: str) -> bool:
    """Valida la integridad básica de la cadena de texto de la ruta."""
    return not isinstance(path_str, str) or not path_str or path_str.strip() == ""

def _is_unc_path(path_str: str) -> bool:
    """Identifica rutas de red con formato UNC."""
    if not isinstance(path_str, str): return False
    return path_str.startswith(("\\\\", "//"))

def _is_unc_path_accessible(path_str: str) -> bool:
    """Verifica accesibilidad mínima de una ruta UNC."""
    if not _is_unc_path(path_str) or os.name != 'nt': return False
    kernel32 = ctypes.windll.kernel32
    old_error_mode = kernel32.SetErrorMode(0x0001 | 0x8000)
    try:
        return kernel32.GetDiskFreeSpaceW(path_str, None, None, None, None) != 0
    except (OSError, ctypes.ArgumentError): return False
    finally:
        kernel32.SetErrorMode(old_error_mode)

@lru_cache(maxsize=64)
def _is_reserved_device_name(name: str) -> bool:
    """Verifica si el nombre de archivo es un dispositivo reservado de legado (ej. CON, NUL)."""
    if not name: return False
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))

def _is_device_file(path: Path) -> bool:
    """Comprueba si el objeto apunta a un dispositivo en el espacio de nombres."""
    if not isinstance(path, Path): return False
    path_str = str(path).upper()
    if path_str.startswith(("\\\\.\\", "//./")): return True
    try:
        name = path.name.split('.')[0]
        return _is_reserved_device_name(name)
    except Exception: return False

@lru_cache(maxsize=256)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta flujos de datos alternativos (ADS) en sistemas NTFS."""
    return isinstance(path_name, str) and ":" in path_name and len(path_name.split(":")) > 2

def _is_system_directory_junction(path_str: str) -> bool:
    """Verifica si una ruta es un punto de unión NTFS o reparse point."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    attrs = _get_file_attrs(path_str)
    if attrs == INVALID_FILE_ATTRIBUTES: return True
    return bool(attrs & Win32Attr.REPARSE_POINT)

def _is_file_in_use_by_system(path_str: str) -> bool:
    """Verifica bloqueos del sistema mediante el manejo de módulos activos."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    try:
        h_module = ctypes.windll.kernel32.GetModuleHandleW(path_str)
        return h_module != 0
    except (OSError, ctypes.ArgumentError): return False

@lru_cache(maxsize=64)
def _is_volume_readonly(path_str: Optional[str]) -> bool:
    """Valida si el volumen del sistema está configurado en modo solo lectura."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    if _is_unc_path(path_str): return False
    try:
        drive = os.path.splitdrive(path_str)[0] + "\\"
        serial = _get_volume_serial(drive)
        if not serial: return True
        flags = ctypes.c_ulong(0)
        kernel32 = ctypes.windll.kernel32
        old_error_mode = kernel32.SetErrorMode(0x0001 | 0x8000)
        try:
            if kernel32.GetVolumeInformationW(drive, None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
                return bool(flags.value & 0x80000)
        finally:
            kernel32.SetErrorMode(old_error_mode)
    except (AttributeError, OSError, ctypes.ArgumentError, TypeError): pass
    return True

@lru_cache(maxsize=64)
def _is_volume_read_only_at_root(path_str: Optional[str]) -> bool:
    """Verifica si la unidad raíz está marcada como solo lectura."""
    if os.name != 'nt' or not path_str: return False
    drive = os.path.splitdrive(path_str)[0] + "\\"
    return _is_volume_readonly(drive)

@lru_cache(maxsize=64)
def _is_volume_busy_or_invalid(path_str: Optional[str]) -> bool:
    """Verifica disponibilidad operativa de un volumen."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    if _is_unc_path(path_str): return not _is_unc_path_accessible(path_str)
    try:
        drive = os.path.splitdrive(path_str)[0] + "\\"
        kernel32 = ctypes.windll.kernel32
        err_mode = kernel32.SetErrorMode(0x0001 | 0x8000)
        res = kernel32.GetVolumeInformationW(drive, None, 0, None, None, None, None, 0)
        kernel32.SetErrorMode(err_mode)
        return res == 0
    except (OSError, AttributeError, TypeError, ctypes.ArgumentError): return True

@lru_cache(maxsize=64)
def _get_drive_type(root: str) -> int:
    """Retorna el tipo de unidad del sistema."""
    try: return ctypes.windll.kernel32.GetDriveTypeW(root)
    except (OSError, AttributeError, ctypes.ArgumentError): return DRIVE_UNKNOWN

@lru_cache(maxsize=64)
def _is_volume_removable_media(path_str: Optional[str]) -> bool:
    """Identifica si la ruta corresponde a medios extraíbles."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    if _is_unc_path(path_str): return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        return _get_drive_type(root) == DRIVE_REMOVABLE
    except (AttributeError, OSError, ctypes.ArgumentError): pass
    return False

@lru_cache(maxsize=64)
def _is_volume_compressed_or_encrypted(path_str: Optional[str]) -> bool:
    """Verifica si el volumen soporta capacidades de compresión o cifrado nativo."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    if _is_unc_path(path_str): return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        if not os.path.exists(root): return False
        flags = ctypes.c_ulong(0)
        kernel32 = ctypes.windll.kernel32
        old_error_mode = kernel32.SetErrorMode(0x0001 | 0x8000)
        try:
            if kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
                return bool(flags.value & (0x10 | 0x20000))
        finally:
            kernel32.SetErrorMode(old_error_mode)
    except (AttributeError, OSError, ctypes.ArgumentError): pass
    return False

@lru_cache(maxsize=1024)
def _is_kernel_managed(path_str: str) -> bool:
    """Determina si un archivo es crítico para el SO, bloqueando su modificación."""
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

@lru_cache(maxsize=512)
def _is_sensitive_extension(ext: str) -> bool:
    """Filtra extensiones asociadas a archivos del sistema o configuraciones críticas."""
    return isinstance(ext, str) and ext.lower() in SENSITIVE_EXTENSIONS

def _rule(reason: ProtectionReason, predicate: ViolationPredicate) -> _IntegrityCheck:
    """Helper factory para generar reglas de validación de integridad."""
    return _IntegrityCheck(reason, predicate)

def _validate_junction_depth(path: Path) -> None:
    """Previene ataques de recursión infinita mediante la validación de profundidad de junctions."""
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

def _get_volume_violation(path_str: str, anchor: str, is_unc: bool) -> tuple[bool, str, SafetyValidationErrorCode]:
    """Evalúa condiciones de violación de volumen."""
    violations = [
        (_is_volume_shadow_copy(path_str), "Volumen de instantánea bloqueado.", SafetyValidationErrorCode.VOLUME_SHADOW_COPY),
        (_is_volume_busy_or_invalid(path_str), "Volumen inaccesible.", SafetyValidationErrorCode.IO_ERROR),
        (not is_unc and _get_drive_type(anchor) == DRIVE_REMOTE, "Unidad de red bloqueada.", SafetyValidationErrorCode.IO_ERROR),
        (not is_unc and _get_drive_type(anchor) == DRIVE_REMOVABLE, "Unidad extraíble bloqueada.", SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED),
        (not is_unc and (_get_drive_type(anchor) == DRIVE_CDROM or _is_volume_readonly(path_str) or _is_volume_read_only_at_root(path_str)), "Volumen de solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY),
        (not is_unc and _is_volume_compressed_or_encrypted(path_str), "Volumen cifrado o comprimido.", SafetyValidationErrorCode.VOLUME_RESTRICTED),
        (not is_unc and _is_virtual_drive(path_str), "Unidad virtual bloqueada.", SafetyValidationErrorCode.VIRTUAL_DRIVE_DETECTED)
    ]
    for condition, msg, code in violations:
        if condition: return True, msg, code
    return False, "", SafetyValidationErrorCode.GENERIC

def _validate_drive_restrictions(target_path: Path) -> None:
    """Aplica restricciones de seguridad a nivel de volumen y unidad de disco."""
    path_s = str(target_path)
    anchor = target_path.anchor
    if not anchor: return

    is_unc = _is_unc_path(path_s)
    if is_unc and not _is_unc_path_accessible(path_s):
        raise UnsafePathError("Recurso de red inaccesible.", SafetyValidationErrorCode.UNC_PATH, SafetyValidationCategory.ENVIRONMENT)

    serial = _get_volume_serial(anchor)
    if not serial and not is_unc:
        raise UnsafePathError("Volumen sin serial detectable.", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.ENVIRONMENT)

    is_violated, msg, code = _get_volume_violation(path_s, anchor, is_unc)
    if is_violated:
        raise UnsafePathError(msg, code, SafetyValidationCategory.ENVIRONMENT)

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
    """Verifica el estado del archivo contra el mapa de reglas de integridad predefinidas."""
    if _is_cache_valid(_NEGATIVE_SECURITY_CACHE, state.file_id):
        raise UnsafePathError("Acceso bloqueado: ruta conocida como insegura.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.INTEGRITY)
    if _is_cache_valid(_SECURITY_CACHE, state.file_id):
        return

    for reason, predicate in _VALIDATOR_MAP.items():
        try:
            if predicate(state.path, state.stat_result, state.descriptor):
                if len(_NEGATIVE_SECURITY_CACHE) >= 512: _NEGATIVE_SECURITY_CACHE.clear()
                _NEGATIVE_SECURITY_CACHE[state.file_id] = time.monotonic()
                code = _REASON_TO_CODE.get(reason, SafetyValidationErrorCode.GENERIC)
                raise UnsafePathError(f"Integridad comprometida: {reason.name}", code, SafetyValidationCategory.INTEGRITY)
        except (OSError, PermissionError, AttributeError, TypeError):
            continue
    
    if len(_SECURITY_CACHE) >= 256: _SECURITY_CACHE.clear()
    _SECURITY_CACHE[state.file_id] = time.monotonic()

def _get_path_stat_robust(path: Path) -> os.stat_result:
    """Obtiene metadatos de archivo con reintentos y manejo de errores de acceso Windows."""
    if not path.exists():
        raise UnsafePathError(f"Archivo no encontrado: {path.name}", SafetyValidationErrorCode.ACCESS_DENIED, SafetyValidationCategory.INTEGRITY)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return path.stat()
        except (PermissionError, OSError) as e:
            if os.name == 'nt' and (getattr(e, 'winerror', None) == ERROR_ACCESS_DENIED or isinstance(e, PermissionError)):
                attrs = _get_file_attrs(str(path))
                if attrs != INVALID_FILE_ATTRIBUTES:
                    return os.stat_result((attrs, 0, 0, 0, 0, 0, 0, 0, 0, 0))
            if attempt < max_retries - 1:
                time.sleep(0.05 * (2 ** attempt))
                continue
            raise UnsafePathError(f"Error accediendo a metadatos: {path.name}", SafetyValidationErrorCode.ACCESS_DENIED, SafetyValidationCategory.INTEGRITY)
        except Exception as e:
            raise UnsafePathError(f"Falla inesperada al leer metadatos: {type(e).__name__}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY)
    raise UnsafePathError(f"Archivo inestable tras reintentos: {path.name}", SafetyValidationErrorCode.FILE_IN_USE, SafetyValidationCategory.INTEGRITY)

def _check_file_integrity(state: SafetyState) -> None:
    """Valida la integridad del archivo detectando condiciones tipo TOCTOU."""
    if _is_cache_valid(_NEGATIVE_SECURITY_CACHE, state.file_id):
        raise UnsafePathError("Acceso bloqueado: ruta conocida como insegura.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.INTEGRITY)
        
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

@lru_cache(maxsize=1024)
def _is_readonly(path_str: str) -> bool:
    """Determina si la ruta posee el atributo de solo lectura."""
    if not isinstance(path_str, str) or not path_str: return True
    try:
        path = Path(path_str)
        if not path.exists(): return True
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError, FileNotFoundError, AttributeError, TypeError):
        return True

def _validate_access_permissions(path: Path) -> None:
    """Verifica si el proceso posee permisos efectivos de lectura y escritura."""
    if not isinstance(path, Path): raise UnsafePathError("Tipo de ruta inválido", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)
    try:
        if not os.access(path, os.R_OK):
            raise UnsafePathError("Permisos de lectura denegados.", SafetyValidationErrorCode.ACCESS_DENIED, SafetyValidationCategory.PERMISSION)
        if not os.access(path, os.W_OK):
            raise UnsafePathError("Permisos de escritura denegados.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED, SafetyValidationCategory.PERMISSION)
    except (OSError, PermissionError, FileNotFoundError, AttributeError):
        raise UnsafePathError("Error de acceso al validar permisos.", SafetyValidationErrorCode.ACCESS_DENIED, SafetyValidationCategory.PERMISSION)

def _validate_volume_integrity(path: Path) -> None:
    """Verifica la aptitud del volumen raíz para operaciones de escritura seguras."""
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

@lru_cache(maxsize=1024)
def _normalize_resolved(path_str: str) -> Path:
    """Resuelve y normaliza una ruta con pre-verificación de integridad."""
    if not path_str or _is_device_path(path_str): return Path(path_str)
    try:
        p = Path(os.path.abspath(path_str))
        parent = p.parent
        if os.name == 'nt' and parent.exists() and _is_system_directory_junction(str(parent)):
             raise UnsafePathError("Directorio padre es punto de unión.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.STRUCTURE)
        
        resolved = p.resolve(strict=False)
        if resolved.exists() and _is_device_file(resolved):
             raise UnsafePathError("Ruta resuelta apunta a dispositivo.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED, SafetyValidationCategory.STRUCTURE)
        return resolved
    except (OSError, RuntimeError, PermissionError, ValueError, TypeError, AttributeError):
        return Path(path_str).absolute()

@lru_cache(maxsize=1024)
def normalize(path: PathLike) -> Path:
    """Normaliza y valida una ruta, previniendo ataques de path traversal."""
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
    """Verifica si la ruta es absoluta según los estándares de `pathlib`."""
    try: return Path(path).is_absolute()
    except (TypeError, ValueError, AttributeError): return False

def is_drive_root(path: PathLike) -> bool:
    """Determina si la ruta apunta exactamente a la raíz de una unidad."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (UnsafePathError, TypeError, OSError, AttributeError): return True

@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """Evalúa si la ruta reside en directorios del sistema o rutas protegidas."""
    if path is None: return True
    path_str = str(path)
    if _is_cache_valid(_PROTECTION_CACHE, path_str):
        return _PROTECTION_CACHE[path_str][0]

    try:
        p = Path(path_str)
        # Optimización: Uso de conjuntos para una verificación O(1) en lugar de un bucle O(N)
        path_parts_lower = {part.lower() for part in p.parts}
        is_protected = not PROTECTED_DIR_NAMES.isdisjoint(path_parts_lower)
        
        if not is_protected:
            norm_p = _normalize_resolved(path_str)
            system_roots = _get_system_root_paths()
            # Se usa is_relative_to de Path, que es más seguro para detectar contenedores
            is_protected = any(norm_p == root or (hasattr(norm_p, 'is_relative_to') and norm_p.is_relative_to(root)) for root in system_roots)
            
            if not is_protected:
                attrs = _get_file_attrs(str(norm_p))
                is_protected = (attrs != INVALID_FILE_ATTRIBUTES and (attrs & (Win32Attr.SYSTEM | Win32Attr.HIDDEN | Win32Attr.REPARSE_POINT))) or len(norm_p.parts) <= 1 or os.path.ismount(norm_p)
        
        if len(_PROTECTION_CACHE) > 1024: _PROTECTION_CACHE.clear()
        _PROTECTION_CACHE[path_str] = (is_protected, time.monotonic())
        return is_protected
    except (OSError, AttributeError, ValueError, TypeError):
        return True

def _get_final_path_normalized(p: Path) -> Path:
    """Resuelve la ruta real evitando seguir enlaces simbólicos de forma insegura."""
    if os.name != 'nt':
        return p.resolve()
    
    kernel32 = ctypes.windll.kernel32
    try:
        handle = kernel32.CreateFileW(
            _to_long_path(str(p)), 0, FILE_SHARE_READ, None, 3, FILE_FLAG_BACKUP_SEMANTICS, None
        )
        if handle == INVALID_HANDLE_VALUE:
            return p.resolve(strict=False)
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = kernel32.GetFinalPathNameByHandleW(handle, buf, 1024, 0)
            if size > 0: 
                final = Path(buf.value)
                # Impedir que la resolución final escape del volumen raíz o raíz de red
                if final.anchor != p.anchor and not _is_unc_path(str(p)):
                    raise UnsafePathError("Redirección de reparse point fuera de volumen.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.INTEGRITY)
                return final
        finally:
            kernel32.CloseHandle(handle)
        return p.resolve(strict=False)
    except (OSError, Exception):
        pass
    return p.resolve(strict=False)

@lru_cache(maxsize=1024)
def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica si la ruta 'child' es hijo de 'parent' mediante resolución completa."""
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

@lru_cache(maxsize=1024)
def is_sensitive_file(path: PathLike) -> bool:
    """Detecta archivos críticos basándose en sus extensiones sensibles."""
    if path is None: return True
    try: 
        ext = Path(path).suffix
        return _is_sensitive_extension(ext)
    except (TypeError, ValueError, OSError, AttributeError): return True 

def _is_device_path(path_str: str) -> bool:
    """Verifica si una ruta corresponde a un namespace de dispositivo."""
    return any(path_str.startswith(p) for p in ("\\\\.\\", "\\\\?\\", "//./"))

def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """Valida la integridad léxica y estructural de la ruta para prevenir inyecciones."""
    if _is_cache_valid(_STRUCTURAL_CACHE, path_string):
        return

    checks = [
        (target_path.is_symlink(), "Símbolo de enlace prohibido.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED),
        (len(target_path.name) > MAX_FILENAME_LENGTH, "Nombre de archivo demasiado largo.", SafetyValidationErrorCode.FILENAME_TOO_LONG),
        (_is_spoofing_detected(path_string), "Secuencias de escape visual detectadas.", SafetyValidationErrorCode.INVALID_CHARS),
        (_is_device_path(path_string) and not path_string.startswith(LONG_PATH_PREFIX), "Namespace de dispositivos prohibido.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED),
        (_is_unc_path(path_string) and not _is_unc_path_accessible(path_string), "Rutas UNC/Red inaccesibles.", SafetyValidationErrorCode.UNC_PATH),
        (".." in path_string.split(os.sep), "Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS),
        (_is_path_too_long(path_string), "Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG),
        ("\0" in path_string, "Inyección de carácter nulo.", SafetyValidationErrorCode.NULL_CHAR),
        (_has_invalid_chars(path_string), "Caracteres inválidos detectados.", SafetyValidationErrorCode.INVALID_CHARS),
        (unicodedata.normalize('NFKC', path_string) != path_string, "Codificación sospechosa.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING),
        (_has_alternate_data_stream(target_path.name), "Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED),
        (_is_device_file(target_path), "Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED),
        (_RESERVED_NAMES_PATTERN.fullmatch(target_path.name), "Nombre de dispositivo reservado.", SafetyValidationErrorCode.RESERVED_NAME)
    ]
    
    for condition, msg, code in checks:
        if condition:
            raise UnsafePathError(msg, code, SafetyValidationCategory.STRUCTURE)

    _validate_path_depth_and_root_access(target_path)
    if len(_STRUCTURAL_CACHE) > 1024: _STRUCTURAL_CACHE.clear()
    _STRUCTURAL_CACHE[path_string] = (True, time.monotonic())

def _validate_path_depth_and_root_access(target_path: Path) -> None:
    """Verifica límites de profundidad de directorios y previene operaciones en la raíz."""
    try:
        t_parts = target_path.parts
        if not t_parts or (len(t_parts) == 1 and t_parts[0] == os.sep):
             raise UnsafePathError("Ruta raíz no permitida.", SafetyValidationErrorCode.ROOT_ACCESS, SafetyValidationCategory.STRUCTURE)
        if len(t_parts) == 1 and target_path == Path(target_path.anchor):
             raise UnsafePathError("Operación sobre raíz de unidad prohibida.", SafetyValidationErrorCode.ROOT_ACCESS, SafetyValidationCategory.STRUCTURE)
        if len(t_parts) > MAX_DIRECTORY_DEPTH:
             raise UnsafePathError("Profundidad de directorio excesiva.", SafetyValidationErrorCode.PATH_TOO_DEEP, SafetyValidationCategory.STRUCTURE)
    except (AttributeError, TypeError, ValueError):
        raise UnsafePathError("Estructura de ruta inválida.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE)

def _validate_security_descriptor_attributes(sd: SecurityDescriptor) -> None:
    """Valida los atributos de seguridad recuperados contra las políticas defensivas."""
    if sd.is_protected_system and not is_running_as_admin():
        raise UnsafePathError("Protección defensiva: Acceso denegado a archivo de sistema.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH, SafetyValidationCategory.PERMISSION)
    if sd.has_flag(Win32Attr.OFFLINE):
        raise UnsafePathError("Protección defensiva: Archivo offline.", SafetyValidationErrorCode.OFFLINE_FILE, SafetyValidationErrorCode.INTEGRITY)
    if sd.is_reparse:
        raise UnsafePathError("Protección defensiva: Redirección no autorizada.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationErrorCode.INTEGRITY)
    if sd.has_flag(Win32Attr.SPARSE_FILE):
        raise UnsafePathError("Protección defensiva: Archivo disperso no permitido.", SafetyValidationErrorCode.SPARSE_FILE_DETECTED, SafetyValidationErrorCode.INTEGRITY)

def _check_container_permissions(target_path: Path) -> None:
    """Verifica permisos de escritura en el contenedor (directorio padre)."""
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
    """Validación exhaustiva: verifica permisos, contexto de app y bloqueos de SO antes de la modificación."""
    if not is_absolute_path_allowed(ctx.path):
        raise UnsafePathError("Solo se permiten rutas absolutas.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED, SafetyValidationCategory.STRUCTURE)
    if _is_device_path(ctx.path_str):
        raise UnsafePathError("Prohibido uso de rutas de dispositivo.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED, SafetyValidationCategory.PERMISSION)
    
    _check_container_permissions(ctx.path)
    
    try:
        app_path = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(os.getcwd()).resolve()
        if ctx.path.resolve().is_relative_to(app_path):
            raise UnsafePathError("Protección: No se puede modificar la aplicación.", SafetyValidationErrorCode.APP_DIRECTORY_VIOLATION, SafetyValidationCategory.PERMISSION)
    except (ValueError, TypeError, AttributeError, RuntimeError):
        pass

    if is_running_as_admin() and (_is_file_owned_by_system(ctx.path_str) or _is_kernel_managed(ctx.path_str)):
        raise UnsafePathError("Operación denegada en modo privilegiado.", SafetyValidationErrorCode.ADMIN_PRIVILEGE_VIOLATION, SafetyValidationCategory.PERMISSION)

    try:
        if os.name == 'nt' and _is_system_directory_junction(ctx.path_str):
             final_path = _get_final_path_normalized(ctx.path)
             if not final_path.drive == ctx.path.drive:
                  raise UnsafePathError("Redirección de reparse point hacia otro volumen.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.INTEGRITY)

        file_id = _get_file_id(ctx.path_str)
        if _is_cache_valid(_NEGATIVE_SECURITY_CACHE, file_id):
            raise UnsafePathError("Acceso bloqueado: ruta conocida como insegura.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.INTEGRITY)
        if ctx.guard.is_locked():
            raise UnsafePathError("Archivo bloqueado por otro proceso.", SafetyValidationErrorCode.FILE_IN_USE, SafetyValidationCategory.INTEGRITY)

        sd = ctx.guard.get_descriptor()
        if sd.attrs != INVALID_FILE_ATTRIBUTES:
            _validate_security_descriptor_attributes(sd)
        
        if _is_kernel_managed(ctx.path_str):
            raise UnsafePathError(f"Archivo crítico: {ctx.path.name}", SafetyValidationErrorCode.KERNEL_LOCKED_FILE, SafetyValidationCategory.PERMISSION)
        
        _validate_volume_integrity(ctx.path)
    except UnsafePathError:
        raise
    except Exception as e:
        raise UnsafePathError(f"Error accediendo a precondiciones: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY)

def _validate_boundary_conditions(target_path: Path, root_directory: Optional[PathLike]) -> None:
    """Valida los límites de la operación frente al alcance permitido por el usuario."""
    if root_directory:
        try:
            rd_path = Path(root_directory)
            if not rd_path.is_absolute():
                raise UnsafePathError("Directorio base debe ser absoluto.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED, SafetyValidationCategory.STRUCTURE)
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
    """Inspecciona recursivamente los reparse points en la cadena de padres de la ruta."""
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
    """Verifica todos los componentes de la ruta buscando ofuscaciones o ciclos referenciales."""
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
    """Valida la integridad y evita ciclos en cada componente individual de una ruta."""
    if _is_spoofing_detected(node.name):
        raise UnsafePathError(f"Spoofing de nombres: {node.name}", SafetyValidationErrorCode.INVALID_CHARS, SafetyValidationCategory.STRUCTURE)
    
    try:
        resolved = node.resolve()
        if resolved in visited:
            raise UnsafePathError(f"Referencia circular: {node}", SafetyValidationErrorCode.CIRCULAR_REFERENCE, SafetyValidationCategory.INTEGRITY)
        visited.add(resolved)
        
        if _is_system_directory_junction(str(node)):
            if _get_final_path_normalized(node) is None:
                raise UnsafePathError(f"Punto de reparse inaccesible: {node}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.INTEGRITY)
    except (OSError, RuntimeError, PermissionError, AttributeError):
        pass

def _validate_no_reparse_traversal(path: Path) -> None:
    """Asegura que el traversal de la ruta no resulte en un escape del volumen raíz."""
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
    """Inspección final de seguridad antes de autorizar cualquier escritura en disco."""
    _validate_access_permissions(ctx.path)
    if ctx.guard.is_locked():
        raise UnsafePathError(f"Archivo en uso: {ctx.path.name}", SafetyValidationErrorCode.FILE_IN_USE, SafetyValidationCategory.INTEGRITY)
    
    try:
        state = SafetyState(
            path=ctx.path,
            descriptor=ctx.guard.get_descriptor(),
            stat_result=_get_path_stat_robust(ctx.path),
            file_id=_get_file_id(ctx.path_str)
        )
        if os.name == 'nt': _validate_ntfs_reparse_redirection(ctx.path, base)
        _check_file_integrity(state)
    except (OSError, PermissionError, UnsafePathError, AttributeError, Exception) as e:
        if isinstance(e, UnsafePathError): raise
        raise UnsafePathError(f"Validación de seguridad fallida: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY)

def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> Path:
    """Valida que una ruta pueda ser modificada de forma segura."""
    result = _execute_safety_pipeline(path, allow_sensitive=allow_sensitive, base_dir=base_dir)
    if not result.is_valid:
        raise result.error
    return Path(path)

def _execute_safety_pipeline(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> PathValidationResult:
    """Ejecuta el pipeline centralizado de validación de seguridad."""
    if path is None:
        return PathValidationResult(False, UnsafePathError("Ruta nula.", SafetyValidationErrorCode.GENERIC, SafetyValidationCategory.STRUCTURE))
        
    try:
        path_str = str(path).strip()
        p = normalize(path_str)
        
        if not allow_sensitive and is_sensitive_file(p):
            return PathValidationResult(False, UnsafePathError(f"Extensión '{p.suffix}' bloqueada.", SafetyValidationErrorCode.SENSITIVE_EXTENSION, SafetyValidationCategory.STRUCTURE))
        
        ctx = PathContext(p, str(p), SafetyGuard(p))
        if _is_kernel_managed(ctx.path_str):
            return PathValidationResult(False, UnsafePathError("Protección: Archivo crítico bloqueado.", SafetyValidationErrorCode.KERNEL_LOCKED_FILE, SafetyValidationCategory.PERMISSION))
        
        _validate_path_components(p)
        _validate_structural_safety(p, ctx.path_str)
        _validate_boundary_conditions(p, base_dir)
        
        if os.name == 'nt' and _is_system_directory_junction(ctx.path_str):
             return PathValidationResult(False, UnsafePathError("Protección: Reparseo no permitido.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED, SafetyValidationCategory.INTEGRITY))
        
        _validate_preconditions(ctx)
        
        if p.exists():
            base = _normalize_resolved(str(base_dir)) if base_dir else None
            _validate_file_operations_security(ctx, allow_sensitive, base)
        elif p.parent.exists() and not os.access(p.parent, os.W_OK):
            return PathValidationResult(False, UnsafePathError("Contenedor sin permisos.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED, SafetyValidationCategory.PERMISSION))
        
        return PathValidationResult(True)
    except UnsafePathError as e:
        return PathValidationResult(False, e)
    except Exception as e:
        return PathValidationResult(False, UnsafePathError(f"Falla inesperada en pipeline: {e}", SafetyValidationErrorCode.IO_ERROR, SafetyValidationCategory.INTEGRITY))

def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> bool:
    """Retorna un booleano indicando si la operación de modificación es segura."""
    return _execute_safety_pipeline(path, allow_sensitive=allow_sensitive, base_dir=base_dir).is_valid

def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> list[Path]:
    """Filtra una colección de rutas, preservando solo las seguras."""
    results: list[Path] = []
    if paths is None: return results
    for p in paths:
        if p is None: continue
        try: results.append(ensure_safe_to_modify(p, allow_sensitive=allow_sensitive, base_dir=base_dir))
        except (UnsafePathError, ValueError, TypeError, OSError, PermissionError, AttributeError): continue
    return results

def describe_protection(path: PathLike) -> str:
    """Genera una descripción legible sobre el estado de seguridad de una ruta."""
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        guard = SafetyGuard(p)
    except (UnsafePathError, TypeError, ValueError, AttributeError): return "Ruta mal formada."
    
    if _is_unc_path(guard.path_str): return f"'{guard.path_str}' ruta de red."
    if _is_device_file(p): return f"'{guard.path_str}' es dispositivo."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(str(p)): return f"'{p}' protegida por sistema."
    
    try:
        if p.exists():
            sd = guard.get_descriptor()
            diagnostics = {
                "enlace simbólico": p.is_symlink(),
                "punto de reparse": sd.is_reparse,
                "punto de montaje": os.path.ismount(p),
                "unidad virtual": _is_virtual_drive(guard.path_str),
                "solo lectura": sd.is_readonly or _is_readonly(guard.path_str),
                "volumen solo lectura": _is_volume_readonly(guard.path_str),
                "volumen extraíble": _is_volume_removable_media(guard.path_str),
                "volumen cifrado": _is_volume_compressed_or_encrypted(guard.path_str),
                "archivo en uso": sd.is_in_use,
                "offline/nube": sd.has_flag(Win32Attr.OFFLINE),
                "oculto/sistema": sd.is_protected_system,
                "contiene ADS": _has_alternate_data_stream(p.name),
                "volumen VSS": _is_volume_shadow_copy(guard.path_str)
            }
            for reason, is_active in diagnostics.items():
                if is_active: return f"'{p}' es {reason}."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío crítico."
    except (OSError, FileNotFoundError, AttributeError): pass
    
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
