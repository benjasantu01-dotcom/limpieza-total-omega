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
    """Define la configuración de una etapa: peso asignado, la función de cálculo y sus reglas de alerta."""
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

def _safe_inv(val: float, fallback: float = 1.0) -> float:
    """Calcula el inverso multiplicativo de forma segura para evitar divisiones por cero."""
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

_PIPELINE_ORDERED: Final[Tuple[PipelineEntry, ...]] = (
    PipelineEntry("seguridad", 30, lambda m: score_security(m.suspicious_count, m.suspicious_warnings), 
                  (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, lambda m: f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad.", lambda m, r: r < WARN_THRESHOLD_HIGH),)),
    PipelineEntry("disco", 20, lambda m: score_disk(m.disk_free_percent), 
                  (RecommendationRule("disco", WARN_THRESHOLD_LOW, lambda m: f"Queda {m.disk_free_percent:.1f}% de disco libre.", lambda m, r: r < WARN_THRESHOLD_LOW),)),
    PipelineEntry("memoria", 18, lambda m: score_memory(m.memory_available_percent), 
                  (RecommendationRule("memoria", WARN_THRESHOLD_LOW, lambda m: "Memoria disponible baja: cerrá procesos innecesarios.", lambda m, r: r < WARN_THRESHOLD_LOW),)),
    PipelineEntry("basura", 14, lambda m: score_junk(m.junk_mb), 
                  (RecommendationRule("basura", WARN_THRESHOLD_MED, lambda m: f"Hay {m.junk_mb:.0f} MB de archivos temporales.", lambda m, r: r < WARN_THRESHOLD_MED),)),
    PipelineEntry("duplicados", 10, lambda m: score_duplicates(m.duplicate_mb), 
                  (RecommendationRule("duplicados", WARN_THRESHOLD_MED, lambda m: f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados.", lambda m, r: r < WARN_THRESHOLD_MED),)),
    PipelineEntry("arranque", 8, lambda m: score_startup(m.startup_count), 
                  (RecommendationRule("arranque", WARN_THRESHOLD_LOW, lambda m: f"{m.startup_count} programas arrancan con Windows.", lambda m, r: r < WARN_THRESHOLD_LOW),)),
)

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Asegura que un valor numérico se mantenga dentro de los límites [min_val, max_val]."""
    val = float(value)
    if not math.isfinite(val): return min_val
    return max(min_val, min(val, max_val))

def score_junk(junk_mb: float | int) -> NormalizedRatio: 
    """Convierte MB de basura a un score [0.0, 1.0] basado en límites globales."""
    return _clamp(1.0 - (float(junk_mb) * _INV_JUNK))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio: 
    """Convierte conteo de amenazas y alertas en un score [0.0, 1.0]."""
    return _clamp(1.0 - _clamp((float(suspicious_count) * 0.05) + (float(warnings) * 0.25), 0.0, 1.0))

def score_memory(available_percent: float | int) -> NormalizedRatio: 
    """Convierte el % de RAM libre en un score [0.0, 1.0]."""
    return _clamp(float(available_percent) * _INV_RAM)

def score_disk(free_percent: float | int) -> NormalizedRatio: 
    """Convierte el % de espacio en disco libre en un score [0.0, 1.0]."""
    return _clamp(float(free_percent) * _INV_DISK)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    """Convierte el tamaño en MB de archivos duplicados en un score [0.0, 1.0]."""
    return _clamp(1.0 - (float(duplicate_mb) * _INV_DUP))

def score_startup(startup_count: int | float) -> NormalizedRatio: 
    """Convierte la cantidad de programas en arranque en un score [0.0, 1.0]."""
    return _clamp(1.0 - (float(startup_count) * _INV_STARTUP))

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
        """Limpia y valida las métricas, forzando tipos numéricos y rangos positivos."""
        def _to_finite_float(val: Any, default: float = 0.0) -> float:
            try:
                num = float(val)
                return num if math.isfinite(num) else default
            except (TypeError, ValueError):
                return default
        
        self.junk_mb = max(0.0, _to_finite_float(self.junk_mb))
        self.duplicate_mb = max(0.0, _to_finite_float(self.duplicate_mb))
        self.suspicious_count = int(max(0, _to_finite_float(self.suspicious_count)))
        self.suspicious_warnings = int(max(0, _to_finite_float(self.suspicious_warnings)))
        self.startup_count = int(max(0, _to_finite_float(self.startup_count)))
        self.quarantined_count = int(max(0, _to_finite_float(self.quarantined_count)))
        self.memory_available_percent = _clamp(_to_finite_float(self.memory_available_percent, 100.0), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_finite_float(self.disk_free_percent, 100.0), 0.0, 100.0)

    @property
    def is_finite(self) -> bool:
        """Verifica que todas las métricas críticas sean numéricamente válidas."""
        return all(isinstance(v, (int, float)) and math.isfinite(float(v)) 
                   for v in [self.junk_mb, self.suspicious_count, self.memory_available_percent, self.disk_free_percent])

@dataclass
class HealthResult:
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool: return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], ratio: NormalizedRatio, findings: List[str]) -> None:
    """Evalúa reglas de recomendación y sanitiza los mensajes de salida."""
    for rule in rules:
        try:
            if rule.check(metrics, ratio):
                msg = rule.message_factory(metrics)
                # Sanitización defensiva eficiente
                clean_msg = "".join(filter(str.isprintable, msg)).strip()
                if clean_msg: 
                    findings.append(clean_msg[:200])
        except Exception:
            continue

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """
    Pipeline principal: normaliza métricas, calcula pesos parciales y recolecta recomendaciones.
    Retorna un objeto HealthResult consolidado.
    """
    m = metrics if isinstance(metrics, SystemMetrics) else SystemMetrics()
    m.validate()
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    accumulated_score: float = 0.0
    
    for entry in _PIPELINE_ORDERED:
        try:
            area_ratio = _clamp(entry.scorer(m))
            _evaluate_rules(m, entry.rules, area_ratio, recommendations)
            
            points = area_ratio * entry.weight
            metric_breakdown[entry.area] = int(round(points))
            accumulated_score += points
        except Exception:
            metric_breakdown[entry.area] = 0
            
    if m.quarantined_count > 0:
        recommendations.append(f"Tenés {m.quarantined_count} archivo(s) en cuarentena.")
    
    final_score = int(round(_clamp(accumulated_score, 0.0, 100.0)))
    return HealthResult(final_score, grade_for_score(final_score), metric_breakdown, recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."])

def _render_bar(points: int, max_val: int) -> str:
    """Crea una representación visual (ASCII) de barra de progreso."""
    limit = max(1, max_val)
    p = max(0, min(points, limit))
    return "#" * p + "." * (limit - p)

def summarize(result: HealthResult | None) -> List[str]:
    """Genera una lista de strings legibles representando el reporte de salud."""
    if not isinstance(result, HealthResult): 
        return ["Error: Informe de salud no disponible."]
        
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in WEIGHTS.items():
        points = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {points:>2}/{maximo:<2} [{_render_bar(points, maximo)}]")
    
    recs = result.recommendations if result.recommendations else ["Sin recomendaciones."]
    lines.extend(("", "Recomendaciones:", *(f"  - {r}" for r in recs)))
    return lines
