"""
healthscore.py — El motor analítico de salud del sistema.

Este módulo implementa un motor de puntuación (scoring) basado en una arquitectura 
funcional de "Pipeline". La lógica separa la recolección de datos (SystemMetrics) 
de la evaluación (Scorer) y la generación de sugerencias (RecommendationRule).

El proceso sigue tres pasos:
1. Normalización: Convierte métricas crudas a un ratio [0.0, 1.0].
2. Ponderación: Aplica pesos configurables (WEIGHTS) al ratio normalizado.
3. Evaluación: Ejecuta reglas condicionales si el ratio cae bajo umbrales críticos.
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

# Tipos definidos para mejorar la legibilidad de la firma de funciones
ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Valor de salud normalizado entre 0.0 (crítico) y 1.0 (óptimo)"]
MetricKey: TypeAlias = str

# Constantes de rendimiento para validación
_ALLOWED_CHARS: Final[set[str]] = set(string.ascii_letters + string.digits + " .,:()-_%")
_BLOCKED_TERMS: Final[frozenset[str]] = frozenset([
    "system32", "windows", "admin", "c:\\", "system", "kernel", "path", 
    "root", "drive", "user", "profile", "appdata", "programdata", "exe", "dll"
])
_MAX_MSG_LEN: Final[int] = 128
_MAX_RECURSION_DEPTH: Final[int] = 5

# Límites de validación para métricas del sistema
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

def _guard_metric_access(func: Callable) -> Callable:
    """Decorador de seguridad para validar el acceso a campos del sistema."""
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if args and isinstance(args[0], str) and args[0] not in _ALL_METRICS:
            return 0.0
        return func(*args, **kwargs)
    return wrapper

def _is_sensitive_input(data: Any) -> bool:
    """Verifica si la entrada contiene rutas o términos bloqueados por seguridad."""
    if not isinstance(data, str): return False
    d_low = data.lower()
    return any(term in d_low for term in _BLOCKED_TERMS)

def _sanitize_msg(msg: str | Any) -> str:
    """Limpia caracteres no permitidos y bloquea términos sensibles."""
    if not isinstance(msg, str) or len(msg) > (_MAX_MSG_LEN * 4) or _is_sensitive_input(msg): 
        return ""
    clean = "".join(char for char in msg if char in _ALLOWED_CHARS)
    return clean.strip()[:_MAX_MSG_LEN]

def _safe_factory(factory: Callable[[SystemMetrics], str]) -> Callable[[SystemMetrics], str]:
    """Crea un envoltorio de seguridad que garantiza una salida limpia desde una factoría."""
    def wrapper(metrics: SystemMetrics) -> str:
        try:
            msg = factory(metrics)
            return _sanitize_msg(msg)
        except Exception:
            return ""
    return wrapper

class Scorer(Protocol):
    """Protocolo funcional para transformar SystemMetrics en un ratio de salud."""
    def __call__(self, metrics: SystemMetrics) -> NormalizedRatio: ...

class Grade(Enum):
    """Representación semántica de los rangos de puntaje (0-100)."""
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"

    @classmethod
    def from_score(cls, score: float | int | None) -> str:
        """Convierte una puntuación numérica a su categoría alfabética equivalente."""
        if score is None:
            return cls.F.value
        try:
            val = float(score)
            if not math.isfinite(val): return cls.F.value
        except (TypeError, ValueError):
            return cls.F.value
        if val >= 90: return cls.A.value
        if val >= 80: return cls.B.value
        if val >= 65: return cls.C.value
        if val >= 50: return cls.D.value
        return cls.F.value

class RecommendationRule(NamedTuple):
    """Define una lógica de diagnóstico reactiva basada en métricas."""
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """Vincula una métrica con su normalizador y sus reglas de advertencia asociadas."""
    area: MetricKey
    weight: int
    scorer: Callable[[SystemMetrics], NormalizedRatio]
    rules: Tuple[RecommendationRule, ...]

__all__ = [
    "SystemMetrics",
    "HealthResult",
    "WEIGHTS",
    "compute_score",
    "grade_for_score",
    "score_junk",
    "score_security",
    "score_memory",
    "score_disk",
    "score_duplicates",
    "score_startup",
    "summarize",
]

_LIMIT_JUNK_MB: Final[float] = 5000.0        
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0   
_LIMIT_STARTUP_COUNT: Final[int] = 20        
_LIMIT_RAM_PERCENT: Final[float] = 35.0      
_LIMIT_DISK_PERCENT: Final[float] = 25.0     

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Limita un valor numérico al rango definido [min_val, max_val]."""
    try:
        val = float(value)
        if not math.isfinite(val): return min_val
        return max(min_val, min(max_val, val))
    except (TypeError, ValueError):
        return min_val

def create_linear_scorer(limit: float, inverse: bool = True) -> Callable[[float], NormalizedRatio]:
    """Genera funciones de normalización lineal."""
    def scorer(val: float) -> NormalizedRatio:
        if limit <= 0.0: return 0.0
        try:
            ratio = val / limit
            return _clamp(1.0 - ratio if inverse else ratio)
        except ZeroDivisionError:
            return 0.0
    return scorer

WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

def _verify_pipeline_integrity(weights: Dict[str, int], pipeline: Tuple[PipelineEntry, ...]) -> None:
    """Valida que los pesos definidos en la configuración sumen exactamente 100%."""
    if not isinstance(weights, dict) or not isinstance(pipeline, tuple):
        return
    try:
        if sum(weights.values()) != 100:
            logging.error("Integridad de Pipeline fallida: Los pesos no suman 100.")
            return
        pipeline_areas = {entry.area for entry in pipeline}
        if set(weights.keys()) != pipeline_areas:
            logging.error("Integridad de Pipeline fallida: Desajuste de áreas.")
    except Exception as error:
        logging.error(f"Error crítico validando integridad del pipeline: {error}")

_JUNK_SCORER: Final = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER: Final = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER: Final = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

_RULES: Final[Dict[str, Tuple[RecommendationRule, ...]]] = {
    "seguridad": (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, _safe_factory(lambda m: f"Riesgos de seguridad detectados: {m.suspicious_count}."), lambda m, r: r < WARN_THRESHOLD_HIGH),),
    "disco": (RecommendationRule("disco", WARN_THRESHOLD_LOW, _safe_factory(lambda m: f"Espacio libre en disco bajo: {m.disk_free_percent:.1f} porciento."), lambda m, r: r < WARN_THRESHOLD_LOW),),
    "memoria": (RecommendationRule("memoria", WARN_THRESHOLD_LOW, _safe_factory(lambda m: "Memoria disponible baja, optimice sus procesos."), lambda m, r: r < WARN_THRESHOLD_LOW),),
    "basura": (RecommendationRule("basura", WARN_THRESHOLD_MED, _safe_factory(lambda m: f"Acumulacion de archivos temporales: {m.junk_mb:.0f} MB."), lambda m, r: r < WARN_THRESHOLD_MED),),
    "duplicados": (RecommendationRule("duplicados", WARN_THRESHOLD_MED, _safe_factory(lambda m: f"Recuperacion posible: {m.duplicate_mb:.0f} MB en duplicados."), lambda m, r: r < WARN_THRESHOLD_MED),),
    "arranque": (RecommendationRule("arranque", WARN_THRESHOLD_LOW, _safe_factory(lambda m: f"Exceso de programas en arranque: {m.startup_count} entradas."), lambda m, r: r < WARN_THRESHOLD_LOW),),
}

PIPELINE_DEFINITIONS: Final[Tuple[PipelineEntry, ...]] = (
    PipelineEntry("seguridad", 30, lambda m: score_security(m.suspicious_count, m.suspicious_warnings), _RULES["seguridad"]),
    PipelineEntry("disco", 20, lambda m: score_disk(m.disk_free_percent), _RULES["disco"]),
    PipelineEntry("memoria", 18, lambda m: score_memory(m.memory_available_percent), _RULES["memoria"]),
    PipelineEntry("basura", 14, lambda m: score_junk(m.junk_mb), _RULES["basura"]),
    PipelineEntry("duplicados", 10, lambda m: score_duplicates(m.duplicate_mb), _RULES["duplicados"]),
    PipelineEntry("arranque", 8, lambda m: score_startup(m.startup_count), _RULES["arranque"]),
)

_verify_pipeline_integrity(WEIGHTS, PIPELINE_DEFINITIONS)

def score_junk(junk_mb: float | int) -> NormalizedRatio: 
    return _JUNK_SCORER(float(junk_mb))

def score_security(suspicious_count: int | float, warnings: int | float = 0) -> NormalizedRatio: 
    count = _validate_numeric(suspicious_count, 0.0, 0.0, 5000.0)
    warn = _validate_numeric(warnings, 0.0, 0.0, 5000.0)
    penalization = (count * 0.05) + (warn * 0.25)
    return _clamp(1.0 - penalization)

def score_memory(available_percent: float | int) -> NormalizedRatio: 
    if _LIMIT_RAM_PERCENT <= 0.0: return 0.0
    try:
        return _clamp(float(available_percent) / _LIMIT_RAM_PERCENT)
    except ZeroDivisionError:
        return 0.0

def score_disk(free_percent: float | int) -> NormalizedRatio: 
    if _LIMIT_DISK_PERCENT <= 0.0: return 0.0
    try:
        return _clamp(float(free_percent) / _LIMIT_DISK_PERCENT)
    except ZeroDivisionError:
        return 0.0

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    return _DUP_SCORER(float(duplicate_mb))

def score_startup(startup_count: int | float) -> NormalizedRatio: 
    return _STARTUP_SCORER(float(startup_count))

def _validate_numeric(value: Any, default: float, min_val: float, max_val: float) -> float:
    """Sanitiza entradas asegurando que sean numéricas, finitas y dentro de un rango dado."""
    try:
        if value is None or not isinstance(value, (int, float)) or isinstance(value, bool):
            return default
        val = float(value)
        if not math.isfinite(val):
            return default
        return float(max(min_val, min(max_val, val)))
    except (ValueError, TypeError):
        return default

@dataclass
class SystemMetrics:
    """Contenedor de datos inmutable para el análisis de salud."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def __post_init__(self) -> None:
        self.validate()

    @_guard_metric_access
    def safe_get(self, field_name: str, default: Any = 0.0) -> Any:
        """Acceso restringido a atributos validados del sistema."""
        val = getattr(self, field_name, default)
        return val if isinstance(val, (int, float)) else default

    def validate(self) -> None:
        """Aplica límites de rango definidos en _METRIC_LIMITS a cada atributo."""
        for field_name in _ALL_METRICS:
            raw_val = getattr(self, field_name, 0.0)
            min_v, max_v = _METRIC_LIMITS.get(field_name, (0.0, 1e9))
            sanitized = _validate_numeric(raw_val, 0.0, min_v, max_v)
            setattr(self, field_name, sanitized)

    @property
    def is_finite(self) -> bool:
        """Verifica validez numérica (finitud) de todas las métricas."""
        return all(math.isfinite(float(getattr(self, f, 0.0))) for f in _ALL_METRICS)

@dataclass
class HealthResult:
    """Resultado consolidado del análisis de salud."""
    score: int = 0
    grade: str = "F"
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=lambda: ["Error: Datos no inicializados."])

    @property
    def is_healthy(self) -> bool: 
        return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: 
    return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], normalized_ratio: NormalizedRatio) -> List[str]:
    """Evalúa reglas de advertencia según la métrica recibida."""
    if not isinstance(rules, tuple) or metrics is None: return []
    findings: List[str] = []
    for rule in rules:
        try:
            if rule.check(metrics, normalized_ratio):
                msg = rule.message_factory(metrics)
                if msg: findings.append(msg)
        except Exception:
            continue
    return findings

@lru_cache(maxsize=128)
def _compute_cached(metric_tuple: Tuple[float, ...], quarantined: float) -> HealthResult:
    """Orquestador central del cálculo de salud mediante pipeline (cacheado)."""
    if metric_tuple is None or len(metric_tuple) != len(_VALIDATION_KEYS):
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Datos de entrada corruptos."])

    metrics = SystemMetrics(
        **dict(zip(_VALIDATION_KEYS, metric_tuple)),
        quarantined_count=_validate_numeric(quarantined, 0.0, 0.0, 10000.0)
    )
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    total_points = 0.0
    
    for entry in PIPELINE_DEFINITIONS:
        try:
            area_ratio = entry.scorer(metrics)
            recs = _evaluate_rules(metrics, entry.rules or (), area_ratio)
            recommendations.extend([_sanitize_msg(r) for r in recs if _sanitize_msg(r)])
            weighted_val = int(_clamp(area_ratio * entry.weight, 0.0, float(entry.weight)))
            metric_breakdown[entry.area] = weighted_val
            total_points += float(weighted_val)
        except Exception:
            continue
            
    if metrics.quarantined_count > 0:
        quarantine_msg = _sanitize_msg("Existen elementos en cuarentena.")
        if quarantine_msg:
            recommendations.append(quarantine_msg)
        
    final_score = int(_clamp(total_points, 0.0, 100.0))
    
    return HealthResult(
        score=final_score,
        grade=grade_for_score(final_score),
        breakdown=metric_breakdown,
        recommendations=recommendations if recommendations else ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Punto de entrada principal para el cálculo del score de salud."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Formato de métricas inválido."])
    
    try:
        metrics.validate()
        metric_tuple = tuple(float(getattr(metrics, f, 0.0)) for f in _VALIDATION_KEYS)
        return _compute_cached(metric_tuple, float(metrics.quarantined_count))
    except (TypeError, ValueError, AttributeError, MemoryError) as e:
        logging.error(f"Fallo crítico al calcular score: {e}")
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Fallo al procesar métricas."])

@lru_cache(maxsize=128)
def _render_bar(points: int, max_val: int) -> str:
    """Genera representación visual en texto de los puntajes."""
    return "#" * points + "." * (max(1, max_val) - points)

def summarize(result: HealthResult | None) -> List[str]:
    """Genera un reporte legible a partir del resultado de salud."""
    if not isinstance(result, HealthResult): 
        return ["Error: Informe de salud no disponible."]
    
    try:
        lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
        for entry in PIPELINE_DEFINITIONS:
            val = result.breakdown.get(entry.area, 0)
            bar = _render_bar(val, entry.weight)
            lines.append(f"  {entry.area.capitalize():<12} {val:>2}/{entry.weight:<2} [{bar}]")
        
        recs = [_sanitize_msg(s) for s in result.recommendations if _sanitize_msg(s)]
        lines.extend(("", "Recomendaciones:", *[f"  - {s}" for s in (recs if recs else ["No hay advertencias activas."])]))
        return lines
    except Exception:
        return ["Error: Fallo crítico al generar resumen de informe."]

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.info("Módulo healthscore cargado. Motor analítico inicializado.")
