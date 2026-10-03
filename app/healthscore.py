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
        """Determina la calificación alfabética según el puntaje numérico recibido."""
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

# Límites críticos para la normalización de métricas
_LIMIT_JUNK_MB: Final[float] = 5000.0
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0
_LIMIT_STARTUP_COUNT: Final[int] = 20
_LIMIT_RAM_PERCENT: Final[float] = 35.0
_LIMIT_DISK_PERCENT: Final[float] = 25.0

# Inversos precalculados para optimizar el cálculo de ratios
def _safe_inv(val: float, fallback: float = 1.0) -> float:
    """Calcula el inverso multiplicativo de forma segura para evitar divisiones por cero."""
    return 1.0 / val if (math.isfinite(val) and val != 0) else fallback

_INV_JUNK: Final[float] = _safe_inv(_LIMIT_JUNK_MB)
_INV_DUP: Final[float] = _safe_inv(_LIMIT_DUPLICATE_MB)
_INV_STARTUP: Final[float] = _safe_inv(float(_LIMIT_STARTUP_COUNT))
_INV_RAM: Final[float] = _safe_inv(_LIMIT_RAM_PERCENT, 0.01)
_INV_DISK: Final[float] = _safe_inv(_LIMIT_DISK_PERCENT, 0.01)

# Umbrales para disparar recomendaciones
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

_PIPELINE: Final[List[PipelineEntry]] = [
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
]

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Limita un valor numérico al rango [min_val, max_val]."""
    try:
        val = float(value)
        if not math.isfinite(val): return min_val
        return max(min_val, min(val, max_val))
    except (ValueError, TypeError):
        return min_val

def score_junk(junk_mb: float | int) -> NormalizedRatio: 
    """Calcula el ratio de basura: 1.0 es limpio, 0.0 es el límite crítico (_LIMIT_JUNK_MB)."""
    return _clamp(1.0 - (float(junk_mb) * _INV_JUNK))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio: 
    """Calcula el ratio de seguridad penalizando amenazas detectadas y advertencias."""
    return _clamp(1.0 - _clamp((float(suspicious_count) * 0.05) + (float(warnings) * 0.25), 0.0, 1.0))

def score_memory(available_percent: float | int) -> NormalizedRatio: 
    """Calcula el ratio de memoria, donde valores altos de RAM disponible puntúan mejor."""
    return _clamp(float(available_percent) * _INV_RAM)

def score_disk(free_percent: float | int) -> NormalizedRatio: 
    """Calcula el ratio de disco basándose en el espacio libre porcentual."""
    return _clamp(float(free_percent) * _INV_DISK)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    """Calcula el ratio de duplicados según el tamaño en MB encontrado respecto al límite."""
    return _clamp(1.0 - (float(duplicate_mb) * _INV_DUP))

def score_startup(startup_count: int | float) -> NormalizedRatio: 
    """Calcula el ratio de arranque: menor cantidad de programas implica mayor salud."""
    return _clamp(1.0 - (float(startup_count) * _INV_STARTUP))

@dataclass
class SystemMetrics:
    """
    Contenedor de datos estructurado que agrupa las métricas recolectadas del sistema.
    Realiza una validación y limpieza automática tras la inicialización.
    """
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def __post_init__(self) -> None:
        """Invoca la validación de integridad al instanciar el objeto."""
        self.validate()

    def validate(self) -> None:
        """Asegura que todos los campos tengan tipos y rangos aceptables para el motor de score."""
        def _to_clean(val: Any, default: float, min_v: float = 0.0, max_v: float = float('inf')) -> float:
            try:
                f = float(val) if val is not None else default
                if not math.isfinite(f): return default
                return max(min_v, min(f, max_v))
            except (ValueError, TypeError):
                return default
        
        self.junk_mb = _to_clean(self.junk_mb, 0.0)
        self.duplicate_mb = _to_clean(self.duplicate_mb, 0.0)
        self.suspicious_count = int(_to_clean(self.suspicious_count, 0.0))
        self.suspicious_warnings = int(_to_clean(self.suspicious_warnings, 0.0))
        self.startup_count = int(_to_clean(self.startup_count, 0.0))
        self.quarantined_count = int(_to_clean(self.quarantined_count, 0.0))
        self.memory_available_percent = _to_clean(self.memory_available_percent, 100.0, 0.0, 100.0)
        self.disk_free_percent = _to_clean(self.disk_free_percent, 100.0, 0.0, 100.0)

    @property
    def is_finite(self) -> bool:
        """Valida que los parámetros numéricos críticos no sean infinitos o NaN."""
        return all(math.isfinite(float(v)) for v in [self.junk_mb, self.suspicious_count, self.memory_available_percent, self.disk_free_percent])

@dataclass
class HealthResult:
    """Resultado consolidado del cálculo de salud que incluye puntaje y recomendaciones."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool: 
        """Retorna True si el sistema está en un estado óptimo según el puntaje (>= 80)."""
        return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: 
    """Mapea un puntaje numérico a una calificación alfabética de salud."""
    return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], ratio: NormalizedRatio, findings: List[str]) -> None:
    """Evalúa reglas de recomendación y formatea mensajes de salida de forma segura."""
    for rule in rules:
        try:
            if rule.check(metrics, ratio):
                msg = rule.message_factory(metrics)
                if not isinstance(msg, str): continue
                # Sanitización robusta: asegurar que el texto sea imprimible y truncado
                clean_msg = "".join(filter(str.isprintable, msg)).strip()
                if clean_msg: 
                    findings.append(clean_msg[:200])
        except (Exception, TypeError, ValueError):
            continue

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """
    Ejecuta el pipeline de evaluación: normalización, ponderación y generación de recomendaciones.
    
    Args:
        metrics: Objeto SystemMetrics opcional. Si no se provee, se usa uno por defecto.
        
    Returns:
        HealthResult: Objeto con el puntaje final, grado y desglose de las áreas evaluadas.
    """
    m = metrics if isinstance(metrics, SystemMetrics) else SystemMetrics()
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    accumulated_score: float = 0.0
    
    for entry in _PIPELINE:
        try:
            area_ratio = _clamp(entry.scorer(m))
            _evaluate_rules(m, entry.rules, area_ratio, recommendations)
            
            points = area_ratio * entry.weight
            metric_breakdown[entry.area] = int(round(points))
            accumulated_score += points
        except (Exception, TypeError, ValueError):
            metric_breakdown[entry.area] = 0
            
    if m.quarantined_count > 0:
        recommendations.append(f"Tenés {m.quarantined_count} archivo(s) en cuarentena.")
    
    final_score = int(round(_clamp(accumulated_score, 0.0, 100.0)))
    final_grade = grade_for_score(final_score)
    
    return HealthResult(
        score=final_score,
        grade=final_grade,
        breakdown=metric_breakdown,
        recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

def _render_bar(points: int, max_val: int) -> str:
    """Genera una cadena ASCII (barra de progreso) para representar el puntaje parcial."""
    limit = max(1, max_val)
    p = max(0, min(points, limit))
    return "#" * p + "." * (limit - p)

def summarize(result: HealthResult | None) -> List[str]:
    """Crea una representación textual del reporte para visualización en la interfaz."""
    if not isinstance(result, HealthResult): 
        return ["Error: Informe de salud no disponible."]
        
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in WEIGHTS.items():
        points = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {points:>2}/{maximo:<2} [{_render_bar(points, maximo)}]")
    
    recs = result.recommendations if result.recommendations else ["Sin recomendaciones."]
    lines.extend(("", "Recomendaciones:", *(f"  - {r}" for r in recs)))
    return lines
