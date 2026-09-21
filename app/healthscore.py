"""
healthscore.py — El motor analítico de salud del sistema.

Este módulo implementa un motor de puntuación (scoring) de arquitectura funcional.
Toma un objeto 'SystemMetrics' y, mediante un pipeline de normalización y 
ponderación, lo transforma en un 'HealthResult' comprensible para el usuario.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, NamedTuple, Annotated, Callable, TypeAlias, Protocol
from enum import Enum
import math

ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Valor de salud normalizado entre 0.0 (crítico) y 1.0 (óptimo)"]
MetricKey: TypeAlias = str

class Scorer(Protocol):
    """Interfaz para las funciones que normalizan métricas crudas a ratios [0.0, 1.0]."""
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
        """Determina la letra de calificación para un puntaje dado [0-100]."""
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
    scorer: Scorer
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
    """Calcula ratio [0,1] donde el máximo deseable es _LIMIT_JUNK_MB."""
    return _clamp(1.0 - (_to_float(junk_mb) * _INV_JUNK))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Calcula ratio [0,1] penalizando hallazgos de seguridad encontrados por el escáner."""
    penalization = (_to_float(suspicious_count) * 0.05) + (_to_float(warnings) * 0.25)
    return _clamp(1.0 - _clamp(penalization, 0.0, 1.0))

def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Calcula ratio [0,1] usando el porcentaje de RAM disponible frente al umbral crítico."""
    return _clamp(_to_float(available_percent) * _INV_RAM)

def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Calcula ratio [0,1] usando el porcentaje de disco libre frente al umbral crítico."""
    return _clamp(_to_float(free_percent) * _INV_DISK)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Calcula ratio [0,1] comparando MB de duplicados detectados vs _LIMIT_DUPLICATE_MB."""
    return _clamp(1.0 - (_to_float(duplicate_mb) * _INV_DUP))

def score_startup(startup_count: int | float) -> NormalizedRatio:
    """Calcula ratio [0,1] comparando cantidad de programas en inicio vs _LIMIT_STARTUP_COUNT."""
    return _clamp(1.0 - (_to_float(startup_count) * _INV_STARTUP))

_PIPELINE: Final[List[PipelineEntry]] = [
    PipelineEntry("seguridad", 30, lambda m: score_security(m.suspicious_count, m.suspicious_warnings), 
                  [RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, lambda m: f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad.", lambda m, r: r < WARN_THRESHOLD_HIGH)]),
    PipelineEntry("disco", 20, lambda m: score_disk(m.disk_free_percent), 
                  [RecommendationRule("disco", WARN_THRESHOLD_LOW, lambda m: f"Queda {m.disk_free_percent:.1f}% de disco libre.", lambda m, r: r < WARN_THRESHOLD_LOW)]),
    PipelineEntry("memoria", 18, lambda m: score_memory(m.memory_available_percent), 
                  [RecommendationRule("memoria", WARN_THRESHOLD_LOW, lambda m: "Memoria disponible baja: cerrá procesos innecesarios.", lambda m, r: r < WARN_THRESHOLD_LOW)]),
    PipelineEntry("basura", 14, lambda m: score_junk(m.junk_mb), 
                  [RecommendationRule("basura", WARN_THRESHOLD_MED, lambda m: f"Hay {m.junk_mb:.0f} MB de archivos temporales.", lambda m, r: r < WARN_THRESHOLD_MED)]),
    PipelineEntry("duplicados", 10, lambda m: score_duplicates(m.duplicate_mb), 
                  [RecommendationRule("duplicados", WARN_THRESHOLD_MED, lambda m: f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados.", lambda m, r: r < WARN_THRESHOLD_MED)]),
    PipelineEntry("arranque", 8, lambda m: score_startup(m.startup_count), 
                  [RecommendationRule("arranque", WARN_THRESHOLD_LOW, lambda m: f"{m.startup_count} programas arrancan con Windows.", lambda m, r: r < WARN_THRESHOLD_LOW)]),
]

@dataclass
class SystemMetrics:
    """Contenedor de datos crudos (inputs) para el motor de salud."""
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
        """Asegura la integridad de los datos, forzando rangos positivos y limpieza de NaNs."""
        self.junk_mb = float(max(0.0, _to_float(self.junk_mb)))
        self.duplicate_mb = float(max(0.0, _to_float(self.duplicate_mb)))
        self.suspicious_count = int(max(0, int(_to_float(self.suspicious_count))))
        self.suspicious_warnings = int(max(0, int(_to_float(self.suspicious_warnings))))
        self.startup_count = int(max(0, int(_to_float(self.startup_count))))
        self.quarantined_count = int(max(0, int(_to_float(self.quarantined_count))))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent, 100.0), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent, 100.0), 0.0, 100.0)

    @property
    def is_finite(self) -> bool:
        """Verifica que no existan valores infinitos o corruptos en las métricas."""
        return all(math.isfinite(v) for v in (self.junk_mb, self.suspicious_count, self.suspicious_warnings, 
                                            self.memory_available_percent, self.disk_free_percent, 
                                            self.duplicate_mb, self.startup_count, self.quarantined_count))

@dataclass
class HealthResult:
    """Resultado final del motor de evaluación: puntaje, grado y recomendaciones."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Indica si el sistema está en un estado óptimo (Score >= 80)."""
        return 80 <= self.score <= 100

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Fuerza un valor numérico a estar dentro de [min_val, max_val]."""
    try:
        if not math.isfinite(value): return min_val
        if value < min_val: return min_val
        if value > max_val: return max_val
        return float(value)
    except (TypeError, ValueError):
        return min_val

def _to_float(value: Any, default: float = 0.0) -> float:
    """Conversión segura a float manejando valores nulos o tipos no numéricos."""
    try:
        if value is None: return default
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError, OverflowError): return default

def grade_for_score(score: float | int) -> str:
    """Convierte un puntaje numérico a una calificación (A-F)."""
    return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: List[RecommendationRule], ratio: NormalizedRatio, findings: List[str]) -> None:
    """Ejecuta reglas heurísticas con aislamiento de excepciones."""
    for rule in rules:
        try:
            if rule.check(metrics, ratio):
                msg = rule.message_factory(metrics)
                if isinstance(msg, str) and msg:
                    clean_msg = "".join(c for c in msg if c.isprintable()).strip()
                    if clean_msg:
                        findings.append(clean_msg[:200])
        except Exception:
            continue

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Pipeline de evaluación: mapea SystemMetrics -> HealthResult."""
    if not isinstance(metrics, SystemMetrics) or not metrics.is_finite:
        return HealthResult(0, "F", {k: 0 for k in WEIGHTS}, ["Error: Configuración o métricas no válidas."])
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    accumulated_score: float = 0.0
    
    for entry in _PIPELINE:
        try:
            area_ratio = entry.scorer(metrics)
            if not math.isfinite(area_ratio):
                metric_breakdown[entry.area] = 0
                continue
                
            if entry.rules:
                _evaluate_rules(metrics, entry.rules, area_ratio, recommendations)
            
            weighted_points = int(_clamp(round(area_ratio * entry.weight), 0.0, float(entry.weight)))
            metric_breakdown[entry.area] = weighted_points
            accumulated_score += weighted_points
        except (Exception, TypeError, ValueError, ZeroDivisionError):
            metric_breakdown[entry.area] = 0
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
    """Visualización en texto de una barra de progreso de caracteres ASCII."""
    m = max(1, int(max_val))
    p = max(0, min(int(points), m))
    return ('#' * p) + ('.' * (m - p))

def summarize(result: HealthResult | None) -> List[str]:
    """Genera una representación en formato lista (texto) del HealthResult."""
    if not isinstance(result, HealthResult) or not (0 <= result.score <= 100):
        return ["Error: Informe de salud no disponible."]
    
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in WEIGHTS.items():
        val = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {val:>2}/{maximo:<2} [{_render_bar(val, maximo)}]")
    
    recs = result.recommendations if result.recommendations else ["Sin recomendaciones."]
    lines.extend(["", "Recomendaciones:", *(f"  - {r}" for r in recs)])
    return lines
