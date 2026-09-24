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
    """
    Reglas de recomendación: factory de mensajes activada por validación de métricas.
    
    Attributes:
        area: Identificador del dominio (ej. 'disco').
        threshold: Ratio límite de salud para considerar necesaria la regla.
        message_factory: Función que genera un mensaje dinámico basado en métricas.
        check: Predicado que recibe métricas y ratio para decidir si disparar la regla.
    """
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """
    Configuración de una etapa de evaluación del Pipeline.
    
    Attributes:
        area: Nombre de la métrica a evaluar.
        weight: Valor porcentual (0-100) del impacto en el puntaje total.
        scorer: Función normalizadora para convertir datos a [0.0, 1.0].
        rules: Lista de reglas de recomendación asociadas a esta área.
    """
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

# Umbrales base para normalizar métricas a un rango de salud [0, 1]
_LIMIT_JUNK_MB: Final[float] = 5000.0          
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0     
_LIMIT_STARTUP_COUNT: Final[int] = 20          
_LIMIT_RAM_PERCENT: Final[float] = 35.0        
_LIMIT_DISK_PERCENT: Final[float] = 25.0       

def _safe_inv(val: float, fallback: float = 1.0) -> float:
    """Calcula el inverso multiplicativo para normalización, evitando divisiones por cero."""
    return 1.0 / val if (math.isfinite(val) and val != 0) else fallback

_INV_JUNK: Final[float] = _safe_inv(_LIMIT_JUNK_MB)
_INV_DUP: Final[float] = _safe_inv(_LIMIT_DUPLICATE_MB)
_INV_STARTUP: Final[float] = _safe_inv(float(_LIMIT_STARTUP_COUNT))
_INV_RAM: Final[float] = _safe_inv(_LIMIT_RAM_PERCENT, 0.01)
_INV_DISK: Final[float] = _safe_inv(_LIMIT_DISK_PERCENT, 0.01)

# Umbrales de advertencia global para los ratios normalizados
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
    """Calcula salud de archivos basura: penaliza volúmenes superiores al límite."""
    return _clamp(1.0 - (float(junk_mb) * _INV_JUNK))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Calcula salud de seguridad: evalúa amenazas directas y advertencias heurísticas."""
    penalization = (float(suspicious_count) * 0.05) + (float(warnings) * 0.25)
    return _clamp(1.0 - _clamp(penalization, 0.0, 1.0))

def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Calcula salud de memoria: normaliza según el % de RAM libre disponible."""
    return _clamp(float(available_percent) * _INV_RAM)

def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Calcula salud de disco: normaliza según el % de espacio libre disponible."""
    return _clamp(float(free_percent) * _INV_DISK)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Calcula salud de almacenamiento: penaliza el tamaño de archivos redundantes."""
    return _clamp(1.0 - (float(duplicate_mb) * _INV_DUP))

def score_startup(startup_count: int | float) -> NormalizedRatio:
    """Calcula salud de arranque: penaliza programas excesivos en el inicio."""
    return _clamp(1.0 - (float(startup_count) * _INV_STARTUP))

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
        """Asegura la integridad de los datos, forzando rangos positivos y sanitización."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.suspicious_count = int(max(0, int(_to_float(self.suspicious_count))))
        self.suspicious_warnings = int(max(0, int(_to_float(self.suspicious_warnings))))
        self.startup_count = int(max(0, int(_to_float(self.startup_count))))
        self.quarantined_count = int(max(0, int(_to_float(self.quarantined_count))))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent, 100.0), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent, 100.0), 0.0, 100.0)

    @property
    def is_finite(self) -> bool:
        """Verifica que no existan valores no numéricos o infinitos en las métricas."""
        return (math.isfinite(self.junk_mb) and math.isfinite(self.suspicious_count) and 
                math.isfinite(self.suspicious_warnings) and math.isfinite(self.memory_available_percent) and 
                math.isfinite(self.disk_free_percent) and math.isfinite(self.duplicate_mb) and 
                math.isfinite(self.startup_count) and math.isfinite(self.quarantined_count))

@dataclass
class HealthResult:
    """Resultado final: puntaje global, grado y recomendaciones recolectadas."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Valida si el puntaje global alcanza el estándar de salud (>= 80)."""
        return 80 <= self.score <= 100

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Restringe un valor numérico a un rango acotado, devolviendo min_val en caso de error."""
    val = float(value)
    if not math.isfinite(val): return min_val
    return max(min_val, min(val, max_val))

def _to_float(value: Any, default: float = 0.0) -> float:
    """Convierte cualquier entrada a float de forma segura."""
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError, OverflowError): return default

def grade_for_score(score: float | int) -> str:
    """Mapea puntaje numérico a escala alfabética."""
    return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: List[RecommendationRule], ratio: NormalizedRatio, findings: List[str]) -> None:
    """Ejecuta reglas heurísticas, sanitizando mensajes antes de añadirlos al reporte."""
    for rule in rules:
        if rule.check(metrics, ratio):
            try:
                msg = str(rule.message_factory(metrics))
                clean_msg = "".join(c for c in msg if c.isprintable()).strip()
                if clean_msg:
                    findings.append(clean_msg[:200])
            except (Exception, ValueError, TypeError):
                continue

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Pipeline de evaluación: procesa métricas, calcula pesos y genera recomendaciones."""
    if metrics is None or not isinstance(metrics, SystemMetrics) or not metrics.is_finite:
        return HealthResult(0, "F", {k: 0 for k in WEIGHTS}, ["Error: Configuración o métricas no válidas."])
    
    metrics.validate()
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    accumulated_score: int = 0
    
    for entry in _PIPELINE:
        try:
            area_ratio = entry.scorer(metrics)
        except (ValueError, ZeroDivisionError, TypeError):
            area_ratio = 0.0
            
        if entry.rules:
            _evaluate_rules(metrics, entry.rules, area_ratio, recommendations)
        
        weighted_points = int(_clamp(area_ratio, 0.0, 1.0) * entry.weight + 0.5)
        metric_breakdown[entry.area] = weighted_points
        accumulated_score += weighted_points
            
    final_score = int(_clamp(float(accumulated_score), 0.0, 100.0))
    
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {int(metrics.quarantined_count)} archivo(s) en cuarentena.")
    
    return HealthResult(
        score=final_score, 
        grade=grade_for_score(final_score), 
        breakdown=metric_breakdown, 
        recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

def _render_bar(points: int, max_val: int) -> str:
    """Visualización de barra de progreso ASCII para la interfaz."""
    p = max(0, min(points, max_val))
    return ('#' * p) + ('.' * (max_val - p))

def summarize(result: HealthResult | None) -> List[str]:
    """Genera informe legible para la UI a partir del HealthResult."""
    if not isinstance(result, HealthResult):
        return ["Error: Informe de salud no disponible."]
    
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in WEIGHTS.items():
        val = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {val:>2}/{maximo:<2} [{_render_bar(val, maximo)}]")
    
    recs = result.recommendations if result.recommendations else ["Sin recomendaciones."]
    lines.extend(("", "Recomendaciones:", *(f"  - {r}" for r in recs)))
    return lines
