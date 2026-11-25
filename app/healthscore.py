"""
healthscore.py — El motor analítico de salud del sistema.

Implementa un motor de puntuación (scoring) basado en una arquitectura funcional de 
"Pipeline". La lógica separa la recolección de datos (SystemMetrics) de la 
evaluación (Scorer) y la generación de sugerencias (RecommendationRule).

El proceso de cálculo sigue tres pasos fundamentales:
1. Normalización: Conversión de métricas crudas (ej: MB, %) a un ratio [0.0, 1.0].
2. Ponderación: Aplicación de pesos definidos en WEIGHTS al ratio normalizado.
3. Evaluación: Ejecución de reglas condicionales para emitir recomendaciones.

Seguridad: Todas las entradas son validadas contra inyecciones y términos sensibles.
Los cálculos utilizan decoradores para asegurar que solo se procesen métricas permitidas.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, NamedTuple, Annotated, Callable, TypeAlias, Protocol, Tuple
from types import MappingProxyType
from enum import Enum
from pathlib import Path
import math
import string
import sys
import unicodedata
from functools import lru_cache, wraps

# --- Tipos y Alias de Legibilidad ---
ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Rango [0.0 (crítico), 1.0 (óptimo)]"]
MetricKey: TypeAlias = str

# --- Constantes de Seguridad y Validación ---
_ALLOWED_CHARS: Final[set[str]] = set(string.ascii_letters + string.digits + " .,:()-_%")
_BLOCKED_TERMS: Final[frozenset[str]] = frozenset([
    "system32", "windows", "admin", "c:\\", "system", "kernel", "path", 
    "root", "drive", "user", "profile", "appdata", "programdata", "exe", "dll",
    "boot", "users", "admin", "secret", "private"
])
_MAX_MSG_LEN: Final[int] = 128

_METRIC_LIMITS: Final[MappingProxyType[str, Tuple[float, float]]] = MappingProxyType({
    "junk_mb": (0.0, 1048576.0),
    "duplicate_mb": (0.0, 1048576.0),
    "suspicious_count": (0.0, 5000.0),
    "suspicious_warnings": (0.0, 5000.0),
    "startup_count": (0.0, 1000.0),
    "memory_available_percent": (0.0, 100.0),
    "disk_free_percent": (0.0, 100.0),
    "quarantined_count": (0.0, 10000.0)
})

_VALIDATION_KEYS: Final[Tuple[str, ...]] = (
    "junk_mb", "suspicious_count", "suspicious_warnings", 
    "memory_available_percent", "disk_free_percent", 
    "duplicate_mb", "startup_count"
)
_ALL_METRICS: Final[frozenset[str]] = frozenset(_VALIDATION_KEYS + ("quarantined_count",))

# --- Utilidades de Validación ---

def _is_finite_numeric(val: Any) -> bool:
    """Valida que un objeto sea un número real finito, excluyendo booleanos."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    return math.isfinite(float(val))

def _validate_numeric(value: Any, default: float, min_val: float, max_val: float) -> float:
    """Valida y recorta un valor numérico dentro de los límites físicos definidos."""
    try:
        if value is None or not _is_finite_numeric(value): 
            return float(default)
        return float(max(min_val, min(max_val, float(value))))
    except (ValueError, TypeError):
        return float(default)

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Limita un valor numérico al rango [min_val, max_val] de forma segura."""
    if not _is_finite_numeric(value): return float(min_val)
    return float(max(min_val, min(max_val, float(value))))

def _is_potentially_unsafe(text: str) -> bool:
    """Detecta caracteres de control Unicode potencialmente maliciosos."""
    return any(unicodedata.category(c).startswith('C') for c in text)

# --- Decoradores de Seguridad ---

def _guard_calculation(func: Callable[..., float]) -> Callable[..., float]:
    """Interceptor para funciones de cálculo que asegura un resultado float [0.0, 1.0]."""
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> float:
        try:
            res = func(*args, **kwargs)
            return _clamp(float(res)) if _is_finite_numeric(res) else 0.0
        except (ValueError, ZeroDivisionError, TypeError):
            return 0.0
    return wrapper

def _guard_metric_access(func: Callable[[Any, str, Any], Any]) -> Callable[[Any, str, Any], Any]:
    """Interceptor que limita el acceso a atributos de SystemMetrics según _ALL_METRICS."""
    @wraps(func)
    def wrapper(instance: Any, field_name: str, default: Any = 0.0) -> Any:
        if field_name not in _ALL_METRICS:
            return default
        return func(instance, field_name, default)
    return wrapper

def _is_sensitive_input(data: Any) -> bool:
    """Verifica si la cadena contiene términos prohibidos (prevención de fuga)."""
    if not isinstance(data, str): return False
    d_low = data.lower()
    return any(term in d_low for term in _BLOCKED_TERMS)

def _sanitize_msg(msg: str | Any) -> str:
    """Limpia strings para evitar inyecciones, normaliza y trunca al límite."""
    if not isinstance(msg, str) or _is_potentially_unsafe(msg): return ""
    normalized = unicodedata.normalize("NFKC", msg)
    if len(normalized) > _MAX_MSG_LEN: normalized = normalized[:_MAX_MSG_LEN]
    if _is_sensitive_input(normalized): return ""
    
    clean = "".join(char for char in normalized if char in _ALLOWED_CHARS)
    return clean.strip()

# --- Estructuras y Protocolos ---

class Scorer(Protocol):
    """Interfaz que define el comportamiento para transformar métricas a ratios."""
    def __call__(self, metrics: SystemMetrics) -> NormalizedRatio: ...

class Grade(Enum):
    """Escala de calificación (A-F) basada en la puntuación porcentual final."""
    A = "A"; B = "B"; C = "C"; D = "D"; F = "F"

    @staticmethod
    def _get_letter(score: float) -> str:
        if score >= 90: return Grade.A.value
        if score >= 80: return Grade.B.value
        if score >= 65: return Grade.C.value
        if score >= 50: return Grade.D.value
        return Grade.F.value

    @classmethod
    def from_score(cls, score: float | int | None) -> str:
        """Convierte una puntuación numérica [0-100] a su etiqueta de grado."""
        if score is None or not _is_finite_numeric(score):
            return cls.F.value
        return cls._get_letter(float(score))

class RecommendationRule(NamedTuple):
    """Lógica de negocio: define cuándo y qué sugerir basado en una métrica."""
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """Configuración de un paso en el pipeline de evaluación de salud."""
    area: MetricKey
    weight: int
    scorer: Callable[[SystemMetrics], NormalizedRatio]
    rules: Tuple[RecommendationRule, ...]
    formatted_label: str = ""

# --- Parámetros de Configuración de Salud ---

_LIMIT_JUNK_MB: Final[float] = 5000.0        
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0   
_LIMIT_STARTUP_COUNT: Final[int] = 20        
_LIMIT_RAM_PERCENT: Final[float] = 35.0      
_LIMIT_DISK_PERCENT: Final[float] = 25.0     

WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

WEIGHTS: Final[MappingProxyType[str, int]] = MappingProxyType({
    "seguridad": 30, "disco": 20, "memoria": 18, "basura": 14, "duplicados": 10, "arranque": 8,
})

# --- Factorías y Scorers ---

def create_linear_scorer(limit: float, inverse: bool = True) -> Callable[[float], NormalizedRatio]:
    """Crea un evaluador lineal contra un límite físico de referencia."""
    safe_limit = float(limit) if limit > 0 else 1.0
    
    @lru_cache(maxsize=32)
    @_guard_calculation
    def scorer(val: float) -> NormalizedRatio:
        ratio = float(val) / safe_limit
        return 1.0 - ratio if inverse else ratio
    return scorer

_JUNK_SCORER: Final = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER: Final = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER: Final = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

@lru_cache(maxsize=16)
def score_junk(junk_mb: float | int) -> NormalizedRatio: return _JUNK_SCORER(float(junk_mb))

@lru_cache(maxsize=16)
@_guard_calculation
def score_security(suspicious_count: int | float, warnings: int | float = 0) -> NormalizedRatio: 
    count = _validate_numeric(suspicious_count, 0.0, 0.0, 5000.0)
    warn = _validate_numeric(warnings, 0.0, 0.0, 5000.0)
    penalty = (count * 0.05) + (warn * 0.25)
    return 1.0 - penalty

@lru_cache(maxsize=16)
@_guard_calculation
def score_memory(available_percent: float | int) -> NormalizedRatio: 
    if _LIMIT_RAM_PERCENT <= 0.0: return 0.0
    return float(available_percent) / _LIMIT_RAM_PERCENT

@lru_cache(maxsize=16)
@_guard_calculation
def score_disk(free_percent: float | int) -> NormalizedRatio: 
    if _LIMIT_DISK_PERCENT <= 0.0: return 0.0
    return float(free_percent) / _LIMIT_DISK_PERCENT

@lru_cache(maxsize=16)
def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: return _DUP_SCORER(float(duplicate_mb))

@lru_cache(maxsize=16)
def score_startup(startup_count: int | float) -> NormalizedRatio: return _STARTUP_SCORER(float(startup_count))

# --- Implementación del Pipeline ---

def _msg_security(m: SystemMetrics) -> str: return _sanitize_msg(f"Riesgos de seguridad detectados: {m.suspicious_count}.")
def _msg_disk(m: SystemMetrics) -> str: return _sanitize_msg(f"Espacio libre en disco bajo: {m.disk_free_percent:.1f} porciento.")
def _msg_memory(m: SystemMetrics) -> str: return _sanitize_msg("Memoria disponible baja, optimice sus procesos.")
def _msg_junk(m: SystemMetrics) -> str: return _sanitize_msg(f"Acumulacion de archivos temporales: {m.junk_mb:.0f} MB.")
def _msg_duplicates(m: SystemMetrics) -> str: return _sanitize_msg(f"Recuperacion posible: {m.duplicate_mb:.0f} MB en duplicados.")
def _msg_startup(m: SystemMetrics) -> str: return _sanitize_msg(f"Exceso de programas en arranque: {m.startup_count} entradas.")

def _check_low(m: SystemMetrics, r: NormalizedRatio) -> bool: return r < WARN_THRESHOLD_LOW
def _check_med(m: SystemMetrics, r: NormalizedRatio) -> bool: return r < WARN_THRESHOLD_MED
def _check_high(m: SystemMetrics, r: NormalizedRatio) -> bool: return r < WARN_THRESHOLD_HIGH

def _pipe_security(m: SystemMetrics) -> NormalizedRatio: return score_security(m.suspicious_count, m.suspicious_warnings)
def _pipe_disk(m: SystemMetrics) -> NormalizedRatio: return score_disk(m.disk_free_percent)
def _pipe_memory(m: SystemMetrics) -> NormalizedRatio: return score_memory(m.memory_available_percent)
def _pipe_junk(m: SystemMetrics) -> NormalizedRatio: return score_junk(m.junk_mb)
def _pipe_duplicates(m: SystemMetrics) -> NormalizedRatio: return score_duplicates(m.duplicate_mb)
def _pipe_startup(m: SystemMetrics) -> NormalizedRatio: return score_startup(m.startup_count)

PIPELINE_DEFINITIONS: Final[Tuple[PipelineEntry, ...]] = (
    PipelineEntry("seguridad", 30, _pipe_security, (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, _msg_security, _check_high),), "Seguridad  "),
    PipelineEntry("disco", 20, _pipe_disk, (RecommendationRule("disco", WARN_THRESHOLD_LOW, _msg_disk, _check_low),), "Disco      "),
    PipelineEntry("memoria", 18, _pipe_memory, (RecommendationRule("memoria", WARN_THRESHOLD_LOW, _msg_memory, _check_low),), "Memoria    "),
    PipelineEntry("basura", 14, _pipe_junk, (RecommendationRule("basura", WARN_THRESHOLD_MED, _msg_junk, _check_med),), "Basura     "),
    PipelineEntry("duplicados", 10, _pipe_duplicates, (RecommendationRule("duplicados", WARN_THRESHOLD_MED, _msg_duplicates, _check_med),), "Duplicados "),
    PipelineEntry("arranque", 8, _pipe_startup, (RecommendationRule("arranque", WARN_THRESHOLD_LOW, _msg_startup, _check_low),), "Arranque   "),
)

_NORMALIZATION_BASE: Final[int] = max(1, sum(max(0, p.weight) for p in PIPELINE_DEFINITIONS))

@dataclass
class SystemMetrics:
    """
    Contenedor de telemetría del sistema. 
    
    Asegura la integridad de los datos mediante validación automática contra 
    `_METRIC_LIMITS` y bloqueo de acceso a atributos no permitidos.
    """
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0
    _is_valid: bool = field(init=False, default=True)

    def __post_init__(self) -> None: 
        """Valida que todas las métricas iniciales cumplan los límites físicos."""
        for f in _ALL_METRICS:
            val = getattr(self, f, 0.0)
            self._set_validated_field(f, val)
        self.validate()

    def _set_validated_field(self, name: str, value: Any) -> None:
        """Aplica validación de límites físicos antes de persistir la métrica."""
        limits = _METRIC_LIMITS.get(name, (0.0, 1e9))
        clean_val = _validate_numeric(value, 0.0, *limits)
        object.__setattr__(self, name, clean_val)

    def __setattr__(self, name: str, value: Any) -> None:
        """Controlador de acceso seguro: permite solo métricas de _ALL_METRICS."""
        if name in _ALL_METRICS:
            self._set_validated_field(name, value)
            self.validate()
        elif name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            raise AttributeError(f"Modificación denegada: {name} no es una métrica permitida.")

    @_guard_metric_access
    def safe_get(self, field_name: str, default: Any = 0.0) -> Any: 
        """Lectura protegida que retorna un default si la métrica no existe."""
        return getattr(self, field_name, default)

    def get_clamped_metric(self, field_name: str) -> float:
        """Recupera una métrica validada contra límites físicos definidos."""
        limits = _METRIC_LIMITS.get(field_name, (0.0, 0.0))
        val = getattr(self, field_name, 0.0)
        return _validate_numeric(val, 0.0, *limits)

    def validate(self) -> None:
        """Verifica que todos los valores almacenados sean numéricos finitos."""
        valid = all(_is_finite_numeric(getattr(self, f, 0.0)) for f in _ALL_METRICS)
        object.__setattr__(self, "_is_valid", valid)

    @property
    def is_finite(self) -> bool: return self._is_valid

@dataclass
class HealthResult:
    """Resultado consolidado del cálculo de salud con desglose y recomendaciones."""
    score: int = 0
    grade: str = "F"
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=lambda: ["Error: Datos no inicializados."])

    @property
    def is_healthy(self) -> bool: return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: return Grade.from_score(score)

def _evaluate_pipeline_entry(entry: PipelineEntry, metrics: SystemMetrics) -> Tuple[int, List[str]]:
    """Ejecuta un paso del pipeline, normalizando, ponderando y aplicando reglas."""
    if entry.weight <= 0 or not metrics.is_finite: return 0, []
    try:
        ratio = entry.scorer(metrics)
        contribution = int(_clamp(ratio) * entry.weight)
        recs: List[str] = []
        for rule in entry.rules:
            if callable(rule.check) and rule.check(metrics, ratio):
                msg = rule.message_factory(metrics)
                if isinstance(msg, str) and msg:
                    recs.append(msg)
        return contribution, recs
    except (ValueError, TypeError, ZeroDivisionError):
        return 0, []

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Motor principal: calcula la salud global procesando todas las entradas."""
    if not isinstance(metrics, SystemMetrics) or not metrics.is_finite:
        return HealthResult(0, "F", {}, ["Error: Datos de entrada corruptos."])
    
    metric_breakdown: Dict[MetricKey, int] = {}
    total_score = 0
    recommendations: List[str] = []
    
    for entry in PIPELINE_DEFINITIONS:
        points, rules_recs = _evaluate_pipeline_entry(entry, metrics)
        metric_breakdown[entry.area] = points
        total_score += points
        recommendations.extend(rules_recs)
    
    if metrics.quarantined_count > 0:
        if (msg := _sanitize_msg("Existen elementos en cuarentena.")): 
            recommendations.append(msg)
        
    raw_score = (float(total_score) * 100.0) / float(_NORMALIZATION_BASE)
    final_score = int(_clamp(raw_score, 0.0, 100.0)) if _is_finite_numeric(raw_score) else 0
            
    return HealthResult(
        score=final_score, grade=grade_for_score(final_score), 
        breakdown=metric_breakdown, recommendations=recommendations or ["El sistema está en buen estado."]
    )

@lru_cache(maxsize=128)
def _render_bar(points: int, max_val: int) -> str:
    """Representación gráfica de progreso mediante caracteres ASCII."""
    if max_val <= 0: return ""
    progress = max(0, min(points, max_val))
    return "#" * progress + "." * (max_val - progress)

def summarize(result: HealthResult | None) -> List[str]:
    """Genera informe textual legible del estado de salud detectado."""
    if not isinstance(result, HealthResult): return ["Error: Informe no disponible."]
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for entry in PIPELINE_DEFINITIONS:
        val = result.breakdown.get(entry.area, 0)
        lines.append(f"  {entry.formatted_label} {val:>2}/{entry.weight:<2} [{_render_bar(val, entry.weight)}]")
    lines.extend(("", "Recomendaciones:"))
    if not result.recommendations: lines.append("  - El sistema está en buen estado.")
    else: lines.extend([f"  - {s}" for s in result.recommendations if s])
    return lines
