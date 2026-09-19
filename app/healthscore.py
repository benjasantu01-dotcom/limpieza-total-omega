"""
healthscore.py — El motor analítico de salud del sistema.

Este módulo implementa un motor de puntuación (scoring) de arquitectura funcional.
Toma un objeto 'SystemMetrics' y, mediante un pipeline de normalización y 
ponderación, lo transforma en un 'HealthResult' comprensible para el usuario.

DISEÑO DEL PIPELINE:
- `compute_score`: Función central que procesa las métricas a través de `_PIPELINE`.
- Cada entrada del pipeline (`PipelineEntry`) define:
    1. Un área de evaluación (ej. 'seguridad').
    2. Un peso relativo (influencia en el score total de 0 a 100).
    3. Un conjunto de reglas que generan recomendaciones si el ratio es bajo.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, Tuple, TypeAlias, NamedTuple, Annotated, Callable
from enum import Enum
import math

ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Valor de salud normalizado entre 0.0 (crítico) y 1.0 (óptimo)"]
MetricKey: TypeAlias = str

class Grade(Enum):
    """Calificaciones alfabéticas basadas en rangos de puntaje (0-100)."""
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"

    @classmethod
    def from_score(cls, score: float | int) -> str:
        """Determina la letra de calificación para un puntaje dado."""
        s = float(score)
        if s >= 90: return cls.A.value
        if s >= 80: return cls.B.value
        if s >= 65: return cls.C.value
        if s >= 50: return cls.D.value
        return cls.F.value

class RecommendationRule(NamedTuple):
    """Lógica condicional para generar sugerencias al detectar degradación en un área."""
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, float], bool]

class PipelineEntry(NamedTuple):
    """Configuración operativa de una etapa del cálculo: qué medir y cómo reportar."""
    area: MetricKey
    weight: int
    scorer: Callable[[SystemMetrics], NormalizedRatio]
    rules: List[RecommendationRule]

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

def _safe_inv(val: float, fallback: float = 1.0) -> float:
    """Calcula el inverso multiplicativo para normalización, protegiendo contra divisiones por cero."""
    return 1.0 / val if (math.isfinite(val) and val != 0) else fallback

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

def score_junk(junk_mb: float | int) -> NormalizedRatio:
    """Calcula el ratio de salud según el volumen de archivos temporales (junk) encontrados."""
    return _clamp(1.0 - (_to_float(junk_mb) * _INV_JUNK))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Calcula el ratio de seguridad penalizando hallazgos sospechosos y advertencias."""
    penalization = (_to_float(suspicious_count) * 0.05) + (_to_float(warnings) * 0.25)
    return _clamp(1.0 - _clamp(penalization, 0.0, 1.0))

def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Calcula el ratio de salud basado en el porcentaje de memoria RAM disponible."""
    return _clamp(_to_float(available_percent) * _INV_RAM)

def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Calcula el ratio de salud según el porcentaje de espacio libre en disco."""
    return _clamp(_to_float(free_percent) * _INV_DISK)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Calcula el ratio de salud basado en el espacio ocupado por archivos duplicados."""
    return _clamp(1.0 - (_to_float(duplicate_mb) * _INV_DUP))

def score_startup(startup_count: int | float) -> NormalizedRatio:
    """Calcula el ratio de salud considerando la cantidad de programas que inician con el sistema."""
    return _clamp(1.0 - (_to_float(startup_count) * _INV_STARTUP))

_PIPELINE: Final[List[PipelineEntry]] = [
    PipelineEntry("seguridad", 30, lambda m: score_security(m.suspicious_count, m.suspicious_warnings), [RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, lambda m: f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad.", lambda m, r: r < WARN_THRESHOLD_HIGH)]),
    PipelineEntry("disco", 20, lambda m: score_disk(m.disk_free_percent), [RecommendationRule("disco", WARN_THRESHOLD_LOW, lambda m: f"Queda {m.disk_free_percent:.1f}% de disco libre.", lambda m, r: r < WARN_THRESHOLD_LOW)]),
    PipelineEntry("memoria", 18, lambda m: score_memory(m.memory_available_percent), [RecommendationRule("memoria", WARN_THRESHOLD_LOW, lambda m: "Memoria disponible baja: cerrá procesos innecesarios.", lambda m, r: r < WARN_THRESHOLD_LOW)]),
    PipelineEntry("basura", 14, lambda m: score_junk(m.junk_mb), [RecommendationRule("basura", WARN_THRESHOLD_MED, lambda m: f"Hay {m.junk_mb:.0f} MB de archivos temporales.", lambda m, r: r < WARN_THRESHOLD_MED)]),
    PipelineEntry("duplicados", 10, lambda m: score_duplicates(m.duplicate_mb), [RecommendationRule("duplicados", WARN_THRESHOLD_MED, lambda m: f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados.", lambda m, r: r < WARN_THRESHOLD_MED)]),
    PipelineEntry("arranque", 8, lambda m: score_startup(m.startup_count), [RecommendationRule("arranque", WARN_THRESHOLD_LOW, lambda m: f"{m.startup_count} programas arrancan con Windows.", lambda m, r: r < WARN_THRESHOLD_LOW)]),
]

@dataclass
class SystemMetrics:
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
        try:
            self.junk_mb = float(max(0.0, _to_float(self.junk_mb)))
            self.duplicate_mb = float(max(0.0, _to_float(self.duplicate_mb)))
            self.suspicious_count = int(max(0, int(_to_float(self.suspicious_count))))
            self.suspicious_warnings = int(max(0, int(_to_float(self.suspicious_warnings))))
            self.startup_count = int(max(0, int(_to_float(self.startup_count))))
            self.quarantined_count = int(max(0, int(_to_float(self.quarantined_count))))
            self.memory_available_percent = _clamp(_to_float(self.memory_available_percent, 100.0), 0.0, 100.0)
            self.disk_free_percent = _clamp(_to_float(self.disk_free_percent, 100.0), 0.0, 100.0)
        except (TypeError, ValueError, OverflowError):
            self.junk_mb = self.duplicate_mb = 0.0
            self.suspicious_count = self.suspicious_warnings = 0
            self.startup_count = self.quarantined_count = 0
            self.memory_available_percent = self.disk_free_percent = 100.0

    @property
    def is_finite(self) -> bool:
        return all(math.isfinite(float(getattr(self, f.name))) for f in self.__dataclass_fields__.values())

@dataclass
class HealthResult:
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return 80 <= self.score <= 100

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    try:
        if not math.isfinite(value): return min_val
        return float(max(min_val, min(max_val, value)))
    except (TypeError, ValueError):
        return min_val

def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None: return default
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError, OverflowError): return default

def grade_for_score(score: float | int) -> str:
    return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: List[RecommendationRule], ratio: NormalizedRatio, findings: List[str]) -> None:
    for rule in rules:
        try:
            if rule.check(metrics, ratio):
                msg = rule.message_factory(metrics)
                if isinstance(msg, str):
                    clean_msg = "".join(char for char in msg if char.isprintable()).strip()
                    if clean_msg:
                        findings.append(clean_msg[:200])
        except Exception:
            continue

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Ejecuta el pipeline de evaluación para generar un objeto HealthResult consolidado."""
    if not isinstance(metrics, SystemMetrics) or not metrics.is_finite:
        return HealthResult(0, "F", {}, ["Error: Instancia de métricas no válida."])
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {k: 0 for k in WEIGHTS.keys()}
    accumulated_score: float = 0.0
    
    for entry in _PIPELINE:
        try:
            area_ratio = entry.scorer(metrics)
            if entry.rules:
                _evaluate_rules(metrics, entry.rules, area_ratio, recommendations)
            
            weighted_points = _clamp(round(area_ratio * entry.weight), 0.0, float(entry.weight))
            metric_breakdown[entry.area] = int(weighted_points)
            accumulated_score += weighted_points
        except (TypeError, ValueError, ZeroDivisionError, AttributeError):
            continue
            
    final_score = int(_clamp(round(accumulated_score), 0.0, 100.0))
    
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {int(metrics.quarantined_count)} archivo(s) en cuarentena.")
    
    return HealthResult(
        score=final_score, 
        grade=grade_for_score(final_score), 
        breakdown=metric_breakdown, 
        recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

def _render_bar(points: int, max_val: int) -> str:
    m = max(1, int(max_val))
    p = max(0, min(int(points), m))
    return ('#' * p) + ('.' * (m - p))

def summarize(result: HealthResult | None) -> List[str]:
    if not isinstance(result, HealthResult) or not (0 <= result.score <= 100):
        return ["Error: Informe de salud no disponible."]
    
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in WEIGHTS.items():
        val = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {val:>2}/{maximo:<2} [{_render_bar(val, maximo)}]")
    
    recs = result.recommendations if result.recommendations else ["Sin recomendaciones."]
    lines.extend(["", "Recomendaciones:", *(f"  - {r}" for r in recs)])
    return lines
