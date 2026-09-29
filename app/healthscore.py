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

ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Valor de salud normalizado entre 0.0 (crítico) y 1.0 (óptimo)"]
MetricKey: TypeAlias = str

class Scorer(Protocol):
    """Interfaz para funciones que normalizan métricas crudas a ratios [0.0, 1.0]."""
    def __call__(self, metrics: SystemMetrics) -> NormalizedRatio: ...

class Grade(Enum):
    """Calificaciones alfabéticas basadas en rangos de puntaje (0-100)."""
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"

    @classmethod
    def from_score(cls, score: float | int) -> str:
        s = float(score)
        if s >= 90: return cls.A.value
        if s >= 80: return cls.B.value
        if s >= 65: return cls.C.value
        if s >= 50: return cls.D.value
        return cls.F.value

class RecommendationRule(NamedTuple):
    """Regla lógica que determina si una métrica requiere una acción correctiva."""
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """Define una etapa en el proceso de evaluación de salud."""
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

# Límites superiores para normalización: valores por encima de estos disparan la degradación
_LIMIT_JUNK_MB: Final[float] = 5000.0
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0
_LIMIT_STARTUP_COUNT: Final[int] = 20
_LIMIT_RAM_PERCENT: Final[float] = 35.0
_LIMIT_DISK_PERCENT: Final[float] = 25.0

def _safe_inv(val: float, fallback: float = 1.0) -> float:
    return 1.0 / val if (math.isfinite(val) and val != 0) else fallback

# Factores de inversión pre-calculados para normalización lineal rápida
_INV_JUNK: Final[float] = _safe_inv(_LIMIT_JUNK_MB)
_INV_DUP: Final[float] = _safe_inv(_LIMIT_DUPLICATE_MB)
_INV_STARTUP: Final[float] = _safe_inv(float(_LIMIT_STARTUP_COUNT))
_INV_RAM: Final[float] = _safe_inv(_LIMIT_RAM_PERCENT, 0.01)
_INV_DISK: Final[float] = _safe_inv(_LIMIT_DISK_PERCENT, 0.01)

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

def score_junk(junk_mb: float | int) -> NormalizedRatio: return _clamp(1.0 - (float(junk_mb) * _INV_JUNK))
def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio: return _clamp(1.0 - _clamp((float(suspicious_count) * 0.05) + (float(warnings) * 0.25), 0.0, 1.0))
def score_memory(available_percent: float | int) -> NormalizedRatio: return _clamp(float(available_percent) * _INV_RAM)
def score_disk(free_percent: float | int) -> NormalizedRatio: return _clamp(float(free_percent) * _INV_DISK)
def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: return _clamp(1.0 - (float(duplicate_mb) * _INV_DUP))
def score_startup(startup_count: int | float) -> NormalizedRatio: return _clamp(1.0 - (float(startup_count) * _INV_STARTUP))

# Definición de reglas por área para desacoplar lógica de la ejecución
_RULES: Final[Dict[MetricKey, Tuple[RecommendationRule, ...]]] = {
    "seguridad": (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, lambda m: f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad.", lambda m, r: r < WARN_THRESHOLD_HIGH),),
    "disco": (RecommendationRule("disco", WARN_THRESHOLD_LOW, lambda m: f"Queda {m.disk_free_percent:.1f}% de disco libre.", lambda m, r: r < WARN_THRESHOLD_LOW),),
    "memoria": (RecommendationRule("memoria", WARN_THRESHOLD_LOW, lambda m: "Memoria disponible baja: cerrá procesos innecesarios.", lambda m, r: r < WARN_THRESHOLD_LOW),),
    "basura": (RecommendationRule("basura", WARN_THRESHOLD_MED, lambda m: f"Hay {m.junk_mb:.0f} MB de archivos temporales.", lambda m, r: r < WARN_THRESHOLD_MED),),
    "duplicados": (RecommendationRule("duplicados", WARN_THRESHOLD_MED, lambda m: f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados.", lambda m, r: r < WARN_THRESHOLD_MED),),
    "arranque": (RecommendationRule("arranque", WARN_THRESHOLD_LOW, lambda m: f"{m.startup_count} programas arrancan con Windows.", lambda m, r: r < WARN_THRESHOLD_LOW),),
}

_PIPELINE_MAP: Final[Dict[MetricKey, PipelineEntry]] = {
    "seguridad": PipelineEntry("seguridad", 30, lambda m: score_security(m.suspicious_count, m.suspicious_warnings), _RULES["seguridad"]),
    "disco": PipelineEntry("disco", 20, lambda m: score_disk(m.disk_free_percent), _RULES["disco"]),
    "memoria": PipelineEntry("memoria", 18, lambda m: score_memory(m.memory_available_percent), _RULES["memoria"]),
    "basura": PipelineEntry("basura", 14, lambda m: score_junk(m.junk_mb), _RULES["basura"]),
    "duplicados": PipelineEntry("duplicados", 10, lambda m: score_duplicates(m.duplicate_mb), _RULES["duplicados"]),
    "arranque": PipelineEntry("arranque", 8, lambda m: score_startup(m.startup_count), _RULES["arranque"]),
}

@dataclass
class SystemMetrics:
    """Contenedor de datos crudos del sistema para el cálculo del score."""
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

    def validate(self) -> None:
        """Asegura que los valores sean finitos, positivos y estén dentro de rangos lógicos."""
        def _v(v: Any) -> float:
            val = float(v) if isinstance(v, (int, float)) else 0.0
            return val if math.isfinite(val) else 0.0
        
        self.junk_mb = max(0.0, _v(self.junk_mb))
        self.duplicate_mb = max(0.0, _v(self.duplicate_mb))
        self.suspicious_count = int(max(0, int(_v(self.suspicious_count))))
        self.suspicious_warnings = int(max(0, int(_v(self.suspicious_warnings))))
        self.startup_count = int(max(0, int(_v(self.startup_count))))
        self.quarantined_count = int(max(0, int(_v(self.quarantined_count))))
        self.memory_available_percent = _clamp(_v(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_v(self.disk_free_percent), 0.0, 100.0)

    @property
    def is_finite(self) -> bool:
        """Verifica que todos los campos numéricos sean números finitos válidos."""
        vals = (self.junk_mb, self.suspicious_count, self.suspicious_warnings, self.memory_available_percent, self.disk_free_percent, self.duplicate_mb, self.startup_count, self.quarantined_count)
        return all(math.isfinite(float(v)) for v in vals)

@dataclass
class HealthResult:
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool: return 80 <= self.score <= 100

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Recorta un valor numérico entre min_val y max_val."""
    val = float(value)
    if not math.isfinite(val) or math.isnan(val): return min_val
    return max(min_val, min(val, max_val))

def grade_for_score(score: float | int) -> str: return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], ratio: NormalizedRatio, findings: List[str]) -> None:
    for rule in rules:
        try:
            if rule.check(metrics, ratio):
                raw_msg = rule.message_factory(metrics)
                if isinstance(raw_msg, str):
                    clean_msg = "".join(c for c in raw_msg if c.isprintable()).strip()
                    if clean_msg: findings.append(clean_msg[:200])
        except Exception:
            continue

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    if metrics is None or not metrics.is_finite:
        metrics = SystemMetrics()
    metrics.validate()
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    accumulated_score: int = 0
    
    for area, weight in WEIGHTS.items():
        try:
            entry = _PIPELINE_MAP[area]
            area_ratio = entry.scorer(metrics)
            _evaluate_rules(metrics, entry.rules, area_ratio, recommendations)
            points = int(round(area_ratio * weight))
            metric_breakdown[area] = max(0, min(points, weight))
            accumulated_score += metric_breakdown[area]
        except Exception:
            metric_breakdown[area] = 0
            
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {int(metrics.quarantined_count)} archivo(s) en cuarentena.")
    
    final_score = max(0, min(accumulated_score, 100))
    return HealthResult(final_score, grade_for_score(final_score), metric_breakdown, recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."])

def _render_bar(points: int, max_val: int) -> str:
    limit = max(1, max_val)
    p = max(0, min(points, limit))
    return ('#' * p) + ('.' * (limit - p))

def summarize(result: HealthResult | None) -> List[str]:
    if result is None: return ["Error: Informe de salud no disponible."]
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in WEIGHTS.items():
        points = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {points:>2}/{maximo:<2} [{_render_bar(points, maximo)}]")
    lines.extend(("", "Recomendaciones:", *(f"  - {r}" for r in (result.recommendations or ["Sin recomendaciones."]))))
    return lines
