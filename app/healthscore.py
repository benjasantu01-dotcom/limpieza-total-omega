"""
healthscore.py — El motor analítico de salud del sistema.

Implementa un motor de puntuación (scoring) basado en una arquitectura funcional de 
"Pipeline". La lógica separa la recolección de datos (SystemMetrics) de la 
evaluación (Scorer) y la generación de sugerencias (RecommendationRule).

El proceso de cálculo sigue tres pasos fundamentales:
1. Normalización: Conversión de métricas crudas (ej: MB, %) a un ratio [0.0, 1.0].
2. Ponderación: Aplicación de pesos definidos en WEIGHTS al ratio normalizado.
3. Evaluación: Ejecución de reglas condicionales para emitir recomendaciones.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, NamedTuple, Annotated, Callable, TypeAlias, Protocol, Tuple
from enum import Enum
from pathlib import Path
import math
import logging
import string
import sys
from functools import lru_cache, wraps

# Tipos definidos para mejorar la legibilidad de las firmas de funciones
ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Rango [0.0 (crítico), 1.0 (óptimo)]"]
MetricKey: TypeAlias = str

# --- Constantes y Límites ---
_ALLOWED_CHARS: Final[set[str]] = set(string.ascii_letters + string.digits + " .,:()-_%")
_BLOCKED_TERMS: Final[frozenset[str]] = frozenset([
    "system32", "windows", "admin", "c:\\", "system", "kernel", "path", 
    "root", "drive", "user", "profile", "appdata", "programdata", "exe", "dll",
    "boot", "windows", "users", "admin", "secret", "private"
])
_MAX_MSG_LEN: Final[int] = 128

_METRIC_LIMITS: Final[Dict[str, Tuple[float, float]]] = {
    "junk_mb": (0.0, 1048576.0),
    "duplicate_mb": (0.0, 1048576.0),
    "suspicious_count": (0.0, 5000.0),
    "suspicious_warnings": (0.0, 5000.0),
    "startup_count": (0.0, 1000.0),
    "memory_available_percent": (0.0, 100.0),
    "disk_free_percent": (0.0, 100.0),
    "quarantined_count": (0.0, 10000.0)
}

_VALIDATION_KEYS: Final[Tuple[str, ...]] = (
    "junk_mb", "suspicious_count", "suspicious_warnings", 
    "memory_available_percent", "disk_free_percent", 
    "duplicate_mb", "startup_count"
)
_ALL_METRICS: Final[Tuple[str, ...]] = _VALIDATION_KEYS + ("quarantined_count",)

# --- Utilidades de Validación ---

def _is_finite_numeric(val: Any) -> bool:
    """Valida si un valor es un número real finito, descartando booleanos."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    return math.isfinite(float(val))

def _validate_numeric(value: Any, default: float, min_val: float, max_val: float) -> float:
    """Asegura que un valor sea numérico, finito y contenido en un rango dado."""
    try:
        if value is None or not _is_finite_numeric(value): 
            return float(default)
        return float(max(min_val, min(max_val, float(value))))
    except (ValueError, TypeError):
        return float(default)

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Ajusta un valor numérico al rango [min_val, max_val]."""
    if not _is_finite_numeric(value): return float(min_val)
    return float(max(min_val, min(max_val, float(value))))

# --- Decoradores de Seguridad ---

def _guard_metric_access(func: Callable[[Any, str, Any], Any]) -> Callable[[Any, str, Any], Any]:
    """Decorador para restringir el acceso a métricas definidas en _ALL_METRICS."""
    @wraps(func)
    def wrapper(instance: Any, field_name: str, default: Any = 0.0) -> Any:
        if field_name not in _ALL_METRICS:
            return default
        return func(instance, field_name, default)
    return wrapper

def _is_sensitive_input(data: Any) -> bool:
    """Detecta si un mensaje contiene términos bloqueados para prevenir fugas de datos."""
    if not isinstance(data, str): return False
    d_low = data.lower()
    return any(term in d_low for term in _BLOCKED_TERMS)

def _sanitize_msg(msg: str | Any) -> str:
    """Limpia y valida mensajes contra inyecciones y caracteres no permitidos."""
    if not isinstance(msg, str): return ""
    if len(msg) > _MAX_MSG_LEN: msg = msg[:_MAX_MSG_LEN]
    if _is_sensitive_input(msg): return ""
    
    clean = "".join(char for char in msg if char in _ALLOWED_CHARS)
    return clean.strip()

def _safe_factory(factory: Callable[[SystemMetrics], str]) -> Callable[[SystemMetrics], str]:
    """Wrappea una factoría de mensajes para garantizar que siempre devuelva texto seguro."""
    def wrapper(metrics: SystemMetrics) -> str:
        try:
            return _sanitize_msg(factory(metrics))
        except Exception:
            return ""
    return wrapper

# --- Estructuras y Protocolos ---

class Scorer(Protocol):
    """Interfaz para funciones que transforman métricas en un ratio [0.0, 1.0]."""
    def __call__(self, metrics: SystemMetrics) -> NormalizedRatio: ...

class Grade(Enum):
    """Calificaciones asociadas a los rangos de salud."""
    A = "A"; B = "B"; C = "C"; D = "D"; F = "F"

    @classmethod
    def from_score(cls, score: float | int | None) -> str:
        """Mapea una puntuación numérica (0-100) a una calificación de letra."""
        if score is None or not _is_finite_numeric(score):
            return cls.F.value
        val = float(score)
        if val >= 90: return cls.A.value
        if val >= 80: return cls.B.value
        if val >= 65: return cls.C.value
        if val >= 50: return cls.D.value
        return cls.F.value

class RecommendationRule(NamedTuple):
    """Define una lógica de advertencia basada en el estado de una métrica."""
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """Configuración para cada etapa del cálculo de salud integral."""
    area: MetricKey
    weight: int
    scorer: Callable[[SystemMetrics], NormalizedRatio]
    rules: Tuple[RecommendationRule, ...]
    formatted_label: str = ""

# --- Constantes de Configuración ---

_LIMIT_JUNK_MB: Final[float] = 5000.0        
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0   
_LIMIT_STARTUP_COUNT: Final[int] = 20        
_LIMIT_RAM_PERCENT: Final[float] = 35.0      
_LIMIT_DISK_PERCENT: Final[float] = 25.0     

WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30, "disco": 20, "memoria": 18, "basura": 14, "duplicados": 10, "arranque": 8,
}

# --- Factorías de Scorers ---

def create_linear_scorer(limit: float, inverse: bool = True) -> Callable[[float], NormalizedRatio]:
    """Genera una función de scoring lineal basada en límites predefinidos."""
    @lru_cache(maxsize=32)
    def scorer(val: float) -> NormalizedRatio:
        if limit <= 0.0 or not _is_finite_numeric(val): return 0.0
        try:
            ratio = float(val) / float(limit)
            return _clamp(1.0 - ratio if inverse else ratio)
        except ZeroDivisionError:
            return 0.0
    return scorer

_JUNK_SCORER: Final = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER: Final = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER: Final = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

# --- Funciones de Recomendación (Reglas de Negocio) ---

def _msg_security(m: SystemMetrics) -> str: return f"Riesgos de seguridad detectados: {m.safe_get('suspicious_count')}."
def _msg_disk(m: SystemMetrics) -> str: return f"Espacio libre en disco bajo: {m.safe_get('disk_free_percent'):.1f} porciento."
def _msg_memory(m: SystemMetrics) -> str: return "Memoria disponible baja, optimice sus procesos."
def _msg_junk(m: SystemMetrics) -> str: return f"Acumulacion de archivos temporales: {m.safe_get('junk_mb'):.0f} MB."
def _msg_duplicates(m: SystemMetrics) -> str: return f"Recuperacion posible: {m.safe_get('duplicate_mb'):.0f} MB en duplicados."
def _msg_startup(m: SystemMetrics) -> str: return f"Exceso de programas en arranque: {m.safe_get('startup_count')} entradas."

def _check_low(m: SystemMetrics, r: NormalizedRatio) -> bool: return r < WARN_THRESHOLD_LOW
def _check_med(m: SystemMetrics, r: NormalizedRatio) -> bool: return r < WARN_THRESHOLD_MED
def _check_high(m: SystemMetrics, r: NormalizedRatio) -> bool: return r < WARN_THRESHOLD_HIGH

_RULES: Final[Dict[str, Tuple[RecommendationRule, ...]]] = {
    "seguridad": (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, _safe_factory(_msg_security), _check_high),),
    "disco": (RecommendationRule("disco", WARN_THRESHOLD_LOW, _safe_factory(_msg_disk), _check_low),),
    "memoria": (RecommendationRule("memoria", WARN_THRESHOLD_LOW, _safe_factory(_msg_memory), _check_low),),
    "basura": (RecommendationRule("basura", WARN_THRESHOLD_MED, _safe_factory(_msg_junk), _check_med),),
    "duplicados": (RecommendationRule("duplicados", WARN_THRESHOLD_MED, _safe_factory(_msg_duplicates), _check_med),),
    "arranque": (RecommendationRule("arranque", WARN_THRESHOLD_LOW, _safe_factory(_msg_startup), _check_low),),
}

# --- Funciones de Extracción de Pipeline ---

def _get_security_ratio(m: SystemMetrics) -> NormalizedRatio: return score_security(m.safe_get("suspicious_count"), m.safe_get("suspicious_warnings"))
def _get_disk_ratio(m: SystemMetrics) -> NormalizedRatio: return score_disk(m.safe_get("disk_free_percent"))
def _get_memory_ratio(m: SystemMetrics) -> NormalizedRatio: return score_memory(m.safe_get("memory_available_percent"))
def _get_junk_ratio(m: SystemMetrics) -> NormalizedRatio: return score_junk(m.safe_get("junk_mb"))
def _get_dup_ratio(m: SystemMetrics) -> NormalizedRatio: return score_duplicates(m.safe_get("duplicate_mb"))
def _get_startup_ratio(m: SystemMetrics) -> NormalizedRatio: return score_startup(m.safe_get("startup_count"))

PIPELINE_DEFINITIONS: Final[Tuple[PipelineEntry, ...]] = (
    PipelineEntry("seguridad", 30, _get_security_ratio, _RULES["seguridad"], "Seguridad  "),
    PipelineEntry("disco", 20, _get_disk_ratio, _RULES["disco"], "Disco      "),
    PipelineEntry("memoria", 18, _get_memory_ratio, _RULES["memoria"], "Memoria    "),
    PipelineEntry("basura", 14, _get_junk_ratio, _RULES["basura"], "Basura     "),
    PipelineEntry("duplicados", 10, _get_dup_ratio, _RULES["duplicados"], "Duplicados "),
    PipelineEntry("arranque", 8, _get_startup_ratio, _RULES["arranque"], "Arranque   "),
)

# --- Funciones de Cálculo ---

@lru_cache(maxsize=16)
def score_junk(junk_mb: float | int) -> NormalizedRatio: 
    return _JUNK_SCORER(_validate_numeric(junk_mb, 0.0, 0.0, float('inf')))

@lru_cache(maxsize=16)
def score_security(suspicious_count: int | float, warnings: int | float = 0) -> NormalizedRatio: 
    count = _validate_numeric(suspicious_count, 0.0, 0.0, 5000.0)
    warn = _validate_numeric(warnings, 0.0, 0.0, 5000.0)
    return _clamp(1.0 - ((count * 0.05) + (warn * 0.25)))

@lru_cache(maxsize=16)
def score_memory(available_percent: float | int) -> NormalizedRatio: 
    val = _validate_numeric(available_percent, 0.0, 0.0, 100.0)
    return _clamp(val / _LIMIT_RAM_PERCENT)

@lru_cache(maxsize=16)
def score_disk(free_percent: float | int) -> NormalizedRatio: 
    val = _validate_numeric(free_percent, 0.0, 0.0, 100.0)
    return _clamp(val / _LIMIT_DISK_PERCENT)

@lru_cache(maxsize=16)
def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    return _DUP_SCORER(_validate_numeric(duplicate_mb, 0.0, 0.0, float('inf')))

@lru_cache(maxsize=16)
def score_startup(startup_count: int | float) -> NormalizedRatio: 
    return _STARTUP_SCORER(_validate_numeric(startup_count, 0.0, 0.0, 1000.0))

# --- Modelos de Datos ---

@dataclass
class SystemMetrics:
    """Contenedor de métricas crudas con validación automática de rangos."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def __post_init__(self) -> None: 
        for f in _ALL_METRICS:
            val = getattr(self, f)
            self._set_validated_field(f, val)

    def _set_validated_field(self, name: str, value: Any) -> None:
        """Centraliza la validación de campos para evitar estados inconsistentes."""
        limits = _METRIC_LIMITS.get(name, (0.0, 1e9))
        clean_val = _validate_numeric(value, 0.0, *limits)
        object.__setattr__(self, name, clean_val)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _ALL_METRICS:
            self._set_validated_field(name, value)
        else:
            object.__setattr__(self, name, value)

    @_guard_metric_access
    def safe_get(self, field_name: str, default: Any = 0.0) -> Any: return getattr(self, field_name, default)

    def get_clamped_metric(self, field_name: str) -> float:
        min_v, max_v = _METRIC_LIMITS.get(field_name, (0.0, 1e9))
        return _validate_numeric(getattr(self, field_name, 0.0), 0.0, min_v, max_v)

    def validate(self) -> None:
        pass

    @property
    def is_finite(self) -> bool: 
        return all(_is_finite_numeric(getattr(self, f, 0.0)) for f in _ALL_METRICS)

@dataclass
class HealthResult:
    """Resultado procesado del análisis de salud del sistema."""
    score: int = 0
    grade: str = "F"
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=lambda: ["Error: Datos no inicializados."])

    @property
    def is_healthy(self) -> bool: return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], normalized_ratio: NormalizedRatio) -> List[str]:
    """Ejecuta un conjunto de reglas y devuelve las recomendaciones activas."""
    results = []
    if not isinstance(metrics, SystemMetrics) or not isinstance(rules, tuple):
        return results
    for rule in rules:
        try:
            if rule.check(metrics, normalized_ratio):
                msg = rule.message_factory(metrics)
                if msg: results.append(msg)
        except (AttributeError, TypeError, ValueError):
            continue
    return results

def _process_pipeline_entry(metrics: SystemMetrics, entry: PipelineEntry) -> Tuple[int, List[str]]:
    """Procesa una etapa del pipeline devolviendo puntos y sugerencias."""
    if not isinstance(metrics, SystemMetrics) or not isinstance(entry, PipelineEntry): 
        return 0, []
    try:
        ratio = entry.scorer(metrics)
        final_ratio = _clamp(float(ratio))
        return int(final_ratio * entry.weight), _evaluate_rules(metrics, entry.rules, final_ratio)
    except (Exception, ValueError, TypeError):
        return 0, []

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Calcula la puntuación total de salud ejecutando el pipeline completo."""
    if metrics is None or not isinstance(metrics, SystemMetrics) or not metrics.is_finite:
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Datos de entrada inválidos."])
    
    metric_breakdown, total, recs = {}, 0, []
    
    for entry in PIPELINE_DEFINITIONS:
        try:
            p, r = _process_pipeline_entry(metrics, entry)
            metric_breakdown[entry.area] = p
            total += p
            recs.extend(r)
        except Exception:
            continue
    
    if metrics.safe_get("quarantined_count") > 0:
        msg = _sanitize_msg("Existen elementos en cuarentena.")
        if msg: recs.append(msg)
            
    final_score = int(_clamp(float(total), 0.0, 100.0))
    return HealthResult(final_score, grade_for_score(final_score), metric_breakdown, recs or ["El sistema está en buen estado."])

def _render_bar(points: int, max_val: int) -> str:
    """Genera una representación visual de barra para los resultados."""
    if not isinstance(points, int) or not isinstance(max_val, int) or max_val <= 0: return ""
    p = max(0, min(points, max_val))
    return "#" * p + "." * (max_val - p)

def summarize(result: HealthResult | None) -> List[str]:
    """Convierte el resultado de salud en un informe textual legible."""
    if not isinstance(result, HealthResult): return ["Error: Informe no disponible."]
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for e in PIPELINE_DEFINITIONS:
        val = result.breakdown.get(e.area, 0)
        lines.append(f"  {e.formatted_label} {val:>2}/{e.weight:<2} [{_render_bar(val, e.weight)}]")
    lines.extend(("", "Recomendaciones:", *[f"  - {s}" for s in (result.recommendations or [])]))
    return lines
