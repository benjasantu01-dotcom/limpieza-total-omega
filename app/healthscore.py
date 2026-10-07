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
import math
import logging

ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Valor de salud normalizado entre 0.0 (crítico) y 1.0 (óptimo)"]
MetricKey: TypeAlias = str

class Scorer(Protocol):
    """
    Protocolo funcional para transformar SystemMetrics en un ratio de salud [0.0, 1.0].
    
    Implementaciones deben ser puras (sin I/O), manejar valores atípicos y 
    garantizar consistencia en la salida (siempre devuelve float).
    """
    def __call__(self, metrics: SystemMetrics) -> NormalizedRatio: ...

class Grade(Enum):
    """Representación semántica de los rangos de puntaje (0-100)."""
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"

    @classmethod
    def from_score(cls, score: float | int) -> str:
        """Convierte una puntuación numérica a su categoría alfabética equivalente."""
        try:
            s = float(score)
        except (TypeError, ValueError):
            return cls.F.value
        if s >= 90: return cls.A.value
        if s >= 80: return cls.B.value
        if s >= 65: return cls.C.value
        if s >= 50: return cls.D.value
        return cls.F.value

class RecommendationRule(NamedTuple):
    """
    Regla de diagnóstico ejecutable para detectar anomalías en una métrica específica.
    
    Attributes:
        area: Identificador de la métrica (ej: 'disco').
        threshold: Límite inferior de salud para activar la sugerencia.
        message_factory: Función que genera un mensaje contextual al usuario.
        check: Predicado booleano que evalúa si la métrica actual requiere atención.
    """
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """
    Configuración completa de una unidad de análisis dentro del motor de salud.
    """
    area: MetricKey
    weight: int
    scorer: Scorer
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
    """Asegura que un valor se mantenga dentro del intervalo de normalización [0.0, 1.0]."""
    if not math.isfinite(value): return min_val
    return max(min_val, min(max_val, value))

def create_linear_scorer(limit: float, inverse: bool = True) -> Callable[[float], NormalizedRatio]:
    """
    Fábrica para crear normalizadores lineales.
    
    Args:
        limit: Valor umbral para el cálculo.
        inverse: Si True, un valor mayor al límite reduce el ratio (ej: basura acumulada).
                 Si False, un valor mayor al límite aumenta el ratio (ej: espacio libre).
    """
    def scorer(val: float) -> NormalizedRatio:
        if not math.isfinite(val) or limit <= 0.0: return 0.0
        ratio = val / limit
        return _clamp(1.0 - ratio if inverse else ratio)
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

if sum(WEIGHTS.values()) != 100:
    raise ValueError("La suma de pesos en WEIGHTS debe ser estrictamente 100.")

_JUNK_SCORER = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

_PIPELINE: Final[List[PipelineEntry]] = [
    PipelineEntry(
        "seguridad", 30, 
        lambda m: score_security(m.suspicious_count, m.suspicious_warnings), 
        (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, lambda m: f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad.", lambda m, r: r < WARN_THRESHOLD_HIGH),)
    ),
    PipelineEntry(
        "disco", 20, 
        lambda m: score_disk(m.disk_free_percent), 
        (RecommendationRule("disco", WARN_THRESHOLD_LOW, lambda m: f"Queda {m.disk_free_percent:.1f}% de disco libre.", lambda m, r: r < WARN_THRESHOLD_LOW),)
    ),
    PipelineEntry(
        "memoria", 18, 
        lambda m: score_memory(m.memory_available_percent), 
        (RecommendationRule("memoria", WARN_THRESHOLD_LOW, lambda m: "Memoria disponible baja: cerrá procesos innecesarios.", lambda m, r: r < WARN_THRESHOLD_LOW),)
    ),
    PipelineEntry(
        "basura", 14, 
        lambda m: score_junk(m.junk_mb), 
        (RecommendationRule("basura", WARN_THRESHOLD_MED, lambda m: f"Hay {m.junk_mb:.0f} MB de archivos temporales.", lambda m, r: r < WARN_THRESHOLD_MED),)
    ),
    PipelineEntry(
        "duplicados", 10, 
        lambda m: score_duplicates(m.duplicate_mb), 
        (RecommendationRule("duplicados", WARN_THRESHOLD_MED, lambda m: f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados.", lambda m, r: r < WARN_THRESHOLD_MED),)
    ),
    PipelineEntry(
        "arranque", 8, 
        lambda m: score_startup(m.startup_count), 
        (RecommendationRule("arranque", WARN_THRESHOLD_LOW, lambda m: f"{m.startup_count} programas arrancan con Windows.", lambda m, r: r < WARN_THRESHOLD_LOW),)
    ),
]

def score_junk(junk_mb: float | int) -> NormalizedRatio: 
    return _JUNK_SCORER(float(junk_mb))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio: 
    """Calcula el ratio de seguridad penalizando hallazgos y advertencias con validación."""
    try:
        c = float(suspicious_count) if isinstance(suspicious_count, (int, float)) else 0.0
        w = float(warnings) if isinstance(warnings, (int, float)) else 0.0
        if not math.isfinite(c) or not math.isfinite(w): return 0.0
        penalization = (max(0.0, c) * 0.05) + (max(0.0, w) * 0.25)
        return _clamp(1.0 - penalization)
    except (ValueError, TypeError):
        return 0.0

def score_memory(available_percent: float | int) -> NormalizedRatio: 
    return _clamp(float(available_percent) / _LIMIT_RAM_PERCENT)

def score_disk(free_percent: float | int) -> NormalizedRatio: 
    return _clamp(float(free_percent) / _LIMIT_DISK_PERCENT)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    return _DUP_SCORER(float(duplicate_mb))

def score_startup(startup_count: int | float) -> NormalizedRatio: 
    return _STARTUP_SCORER(float(startup_count))

def _validate_numeric(value: Any, default: float, min_v: float, max_v: float) -> float:
    """Helper interno para sanitizar métricas numéricas entrantes."""
    try:
        val = float(value)
        if not math.isfinite(val) or val < min_v or val > max_v:
            return default
        return val
    except (ValueError, TypeError):
        return default

@dataclass
class SystemMetrics:
    """Contenedor de datos estructurado que agrupa las métricas recolectadas del sistema."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    _CHECK_FIELDS: Final[Tuple[str, ...]] = (
        "junk_mb", "suspicious_count", "suspicious_warnings", 
        "memory_available_percent", "disk_free_percent", 
        "duplicate_mb", "startup_count", "quarantined_count"
    )

    def __post_init__(self) -> None:
        self.validate()

    def safe_get(self, field_name: str, default: Any = 0) -> Any:
        """Acceso defensivo a los campos del contenedor."""
        return getattr(self, field_name, default)

    def validate(self) -> None:
        """Asegura que los datos recibidos tengan tipos y rangos válidos de forma defensiva."""
        self.junk_mb = _validate_numeric(self.junk_mb, 0.0, 0.0, 1e9)
        self.duplicate_mb = _validate_numeric(self.duplicate_mb, 0.0, 0.0, 1e9)
        self.suspicious_count = int(_validate_numeric(self.suspicious_count, 0, 0, 1e6))
        self.suspicious_warnings = int(_validate_numeric(self.suspicious_warnings, 0, 0, 1e6))
        self.startup_count = int(_validate_numeric(self.startup_count, 0, 0, 1e4))
        self.quarantined_count = int(_validate_numeric(self.quarantined_count, 0, 0, 1e4))
        self.memory_available_percent = _validate_numeric(self.memory_available_percent, 100.0, 0.0, 100.0)
        self.disk_free_percent = _validate_numeric(self.disk_free_percent, 100.0, 0.0, 100.0)

    @property
    def is_finite(self) -> bool:
        """Verifica que ninguna métrica numérica sea infinita o no-numérica."""
        return all(math.isfinite(getattr(self, f)) for f in self._CHECK_FIELDS)

@dataclass
class HealthResult:
    """Resultado final consolidado: puntaje, nota, desglose y recomendaciones."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool: 
        return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: 
    return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], normalized_ratio: NormalizedRatio, findings: List[str]) -> None:
    """Ejecuta las reglas asociadas a una métrica y sanitiza el texto de los resultados."""
    for rule in rules:
        try:
            if rule.check(metrics, normalized_ratio):
                raw_msg = rule.message_factory(metrics)
                if not isinstance(raw_msg, str): 
                    logging.warning(f"Regla en {rule.area} devolvió tipo inesperado: {type(raw_msg)}")
                    continue
                clean_msg = "".join(c for c in raw_msg if c.isprintable() and c not in "\r\n\t").strip()
                if clean_msg: findings.append(clean_msg[:200])
        except Exception as e:
            logging.error(f"Falla en evaluación de regla {rule.area}: {e}")

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """
    Calcula el puntaje global mediante la ejecución del pipeline con manejo estricto de errores.
    """
    if not isinstance(metrics, SystemMetrics):
        metrics = SystemMetrics()
    
    metrics.validate()
    if not metrics.is_finite:
        metrics = SystemMetrics()
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    accumulated_score: float = 0.0
    
    for entry in _PIPELINE:
        try:
            area_ratio = _clamp(entry.scorer(metrics))
            if entry.rules:
                _evaluate_rules(metrics, entry.rules, area_ratio, recommendations)
            points = area_ratio * entry.weight
            metric_breakdown[entry.area] = int(round(points))
            accumulated_score += points
        except Exception as e:
            logging.error(f"Falla crítica en pipeline {entry.area}: {e}")
            metric_breakdown[entry.area] = 0
            
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {int(metrics.quarantined_count)} archivo(s) en cuarentena.")
    
    final_score = int(round(_clamp(accumulated_score, 0.0, 100.0)))
    return HealthResult(
        score=final_score,
        grade=grade_for_score(final_score),
        breakdown=metric_breakdown,
        recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

def _render_bar(points: int, max_val: int) -> str:
    """Representación visual: barra de caracteres ASCII para la interfaz."""
    try:
        limit = max(1, int(max_val))
        p = max(0, min(int(points), limit))
        return "#" * p + "." * (limit - p)
    except (ValueError, TypeError):
        return "." * max(1, max_val)

def summarize(result: HealthResult | None) -> List[str]:
    """Genera una lista de cadenas legible para el informe de estado final."""
    if not isinstance(result, HealthResult): 
        return ["Error: Informe de salud no disponible."]
        
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    bd = result.breakdown
    for area, maximo in WEIGHTS.items():
        points = bd.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {points:>2}/{maximo:<2} [{_render_bar(points, maximo)}]")
    
    recs = result.recommendations if result.recommendations else ["Sin recomendaciones."]
    lines.extend(("", "Recomendaciones:", *(f"  - {r}" for r in recs)))
    return lines
