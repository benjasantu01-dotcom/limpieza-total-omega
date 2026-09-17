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

# Umbrales críticos que definen el punto donde la salud comienza a degradarse
_LIMIT_JUNK_MB: Final[float] = 5000.0          
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0     
_LIMIT_STARTUP_COUNT: Final[int] = 20          
_LIMIT_RAM_PERCENT: Final[float] = 35.0        
_LIMIT_DISK_PERCENT: Final[float] = 25.0       

def _safe_inv(val: float, fallback: float = 1.0) -> float:
    """Calcula el inverso para normalización evitando divisiones por cero."""
    return 1.0 / val if (math.isfinite(val) and val != 0) else fallback

# Factores de escalado precalculados para convertir métricas crudas a ratios [0, 1]
_INV_JUNK: Final[float] = _safe_inv(_LIMIT_JUNK_MB)
_INV_DUP: Final[float] = _safe_inv(_LIMIT_DUPLICATE_MB)
_INV_STARTUP: Final[float] = _safe_inv(float(_LIMIT_STARTUP_COUNT))
_INV_RAM: Final[float] = _safe_inv(_LIMIT_RAM_PERCENT, 0.01)
_INV_DISK: Final[float] = _safe_inv(_LIMIT_DISK_PERCENT, 0.01)

# Umbrales de severidad para disparar recomendaciones en la interfaz
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# Pesos relativos por área. La suma debe totalizar exactamente 100 puntos.
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
    """Calcula ratio: 1.0 es 0MB basura, 0.0 es el límite crítico definido."""
    return _clamp(1.0 - (_to_float(junk_mb) * _INV_JUNK))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Evalúa seguridad: penalización fija por hallazgos y advertencias."""
    return _clamp(1.0 - ((_to_float(suspicious_count) * 0.05) + (_to_float(warnings) * 0.25)))

def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Calcula ratio: evalúa la disponibilidad de RAM frente a niveles críticos."""
    return _clamp(_to_float(available_percent) * _INV_RAM)

def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Calcula ratio: evalúa el espacio libre porcentual frente a niveles críticos."""
    return _clamp(_to_float(free_percent) * _INV_DISK)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Calcula ratio: basado en volumen de redundancia respecto al límite."""
    return _clamp(1.0 - (_to_float(duplicate_mb) * _INV_DUP))

def score_startup(startup_count: int | float) -> NormalizedRatio:
    """Calcula ratio: evalúa sobrecarga del proceso de inicio del sistema."""
    return _clamp(1.0 - (_to_float(startup_count) * _INV_STARTUP))

_RULES_LIST: Final[Tuple[RecommendationRule, ...]] = (
    RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, lambda m: f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad.", lambda m, r: r < WARN_THRESHOLD_HIGH),
    RecommendationRule("disco", WARN_THRESHOLD_LOW, lambda m: f"Queda {m.disk_free_percent:.1f}% de disco libre.", lambda m, r: r < WARN_THRESHOLD_LOW),
    RecommendationRule("memoria", WARN_THRESHOLD_LOW, lambda m: "Memoria disponible baja: cerrá procesos innecesarios.", lambda m, r: r < WARN_THRESHOLD_LOW),
    RecommendationRule("basura", WARN_THRESHOLD_MED, lambda m: f"Hay {m.junk_mb:.0f} MB de archivos temporales.", lambda m, r: r < WARN_THRESHOLD_MED),
    RecommendationRule("duplicados", WARN_THRESHOLD_MED, lambda m: f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados.", lambda m, r: r < WARN_THRESHOLD_MED),
    RecommendationRule("arranque", WARN_THRESHOLD_LOW, lambda m: f"{m.startup_count} programas arrancan con Windows.", lambda m, r: r < WARN_THRESHOLD_LOW),
)

_RULES_BY_AREA: Final[Dict[MetricKey, List[RecommendationRule]]] = {}
for r in _RULES_LIST:
    _RULES_BY_AREA.setdefault(r.area, []).append(r)

_PIPELINE: Final[List[PipelineEntry]] = [
    PipelineEntry("seguridad", 30, lambda m: score_security(m.suspicious_count, m.suspicious_warnings), _RULES_BY_AREA.get("seguridad", [])),
    PipelineEntry("disco", 20, lambda m: score_disk(m.disk_free_percent), _RULES_BY_AREA.get("disco", [])),
    PipelineEntry("memoria", 18, lambda m: score_memory(m.memory_available_percent), _RULES_BY_AREA.get("memoria", [])),
    PipelineEntry("basura", 14, lambda m: score_junk(m.junk_mb), _RULES_BY_AREA.get("basura", [])),
    PipelineEntry("duplicados", 10, lambda m: score_duplicates(m.duplicate_mb), _RULES_BY_AREA.get("duplicados", [])),
    PipelineEntry("arranque", 8, lambda m: score_startup(m.startup_count), _RULES_BY_AREA.get("arranque", [])),
]

@dataclass
class SystemMetrics:
    """Contenedor de datos inyectables que representan el estado actual del sistema."""
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
        """Limpia y normaliza métricas crudas para garantizar integridad en el cálculo."""
        try:
            self.junk_mb = max(0.0, _to_float(self.junk_mb))
            self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
            self.suspicious_count = int(max(0, _to_float(self.suspicious_count)))
            self.suspicious_warnings = int(max(0, _to_float(self.suspicious_warnings)))
            self.startup_count = int(max(0, _to_float(self.startup_count)))
            self.quarantined_count = int(max(0, _to_float(self.quarantined_count)))
            self.memory_available_percent = _clamp(_to_float(self.memory_available_percent, 100.0), 0.0, 100.0)
            self.disk_free_percent = _clamp(_to_float(self.disk_free_percent, 100.0), 0.0, 100.0)
        except (TypeError, ValueError):
            self.junk_mb = self.duplicate_mb = 0.0
            self.suspicious_count = self.suspicious_warnings = 0
            self.startup_count = self.quarantined_count = 0
            self.memory_available_percent = self.disk_free_percent = 100.0

    @property
    def is_finite(self) -> bool:
        """Verifica que todos los campos contengan valores numéricos procesables."""
        return (math.isfinite(self.junk_mb) and math.isfinite(self.suspicious_count) and 
                math.isfinite(self.suspicious_warnings) and math.isfinite(self.memory_available_percent) and
                math.isfinite(self.disk_free_percent) and math.isfinite(self.duplicate_mb) and
                math.isfinite(self.startup_count) and math.isfinite(self.quarantined_count))

@dataclass
class HealthResult:
    """Objeto inmutable con el resumen final del diagnóstico de salud."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Retorna True si el sistema está dentro del rango operativo óptimo."""
        return 80 <= self.score <= 100

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Restringe un valor numérico a un rango específico."""
    return float(max(min_val, min(max_val, value)))

def _to_float(value: Any, default: float = 0.0) -> float:
    """Convierte cualquier entrada a float, descartando valores no numéricos."""
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError, OverflowError): return default

def grade_for_score(score: float | int) -> str:
    """Interfaz externa para obtener la calificación alfabética de un score."""
    return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: List[RecommendationRule], ratio: NormalizedRatio, findings: List[str]) -> None:
    """Itera sobre el conjunto de reglas y agrega hallazgos sanitizados al reporte."""
    for rule in rules:
        try:
            if rule.check(metrics, ratio):
                msg = str(rule.message_factory(metrics))
                # Sanitización: caracteres imprimibles y límite de longitud
                clean_msg = "".join(c for c in msg if c.isprintable()).strip()
                if clean_msg:
                    findings.append(clean_msg[:200])
        except (Exception, TypeError, ValueError):
            continue

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Ejecuta el pipeline de evaluación: normaliza métricas, suma puntos y genera hallazgos."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Instancia de métricas no válida."])
    
    metrics.validate()
    if not metrics.is_finite:
        return HealthResult(0, "F", {}, ["Error: Inconsistencia numérica detectada."])
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    accumulated_score: float = 0.0
    
    for entry in _PIPELINE:
        try:
            val = entry.scorer(metrics)
            area_ratio = _clamp(float(val)) if math.isfinite(val) else 0.0
            
            if entry.rules:
                _evaluate_rules(metrics, entry.rules, area_ratio, recommendations)
            
            weighted_points = _clamp(round(area_ratio * entry.weight), 0, entry.weight)
            metric_breakdown[entry.area] = int(weighted_points)
            accumulated_score += weighted_points
        except (Exception, TypeError, ValueError):
            metric_breakdown[entry.area] = 0
            recommendations.append(f"Error al analizar el área: {entry.area}.")
            
    final_score = int(_clamp(round(accumulated_score), 0.0, 100.0)) if math.isfinite(accumulated_score) else 0
    
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {int(metrics.quarantined_count)} archivo(s) en cuarentena.")
    
    return HealthResult(
        score=final_score, 
        grade=grade_for_score(final_score), 
        breakdown=metric_breakdown, 
        recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

def _render_bar(points: int, max_val: int) -> str:
    """Genera una representación visual de texto para las barras de progreso."""
    if max_val <= 0: return ""
    puntos_norm = int(_clamp(float(points), 0.0, float(max_val)))
    return ('#' * puntos_norm) + ('.' * (max_val - puntos_norm))

def summarize(result: HealthResult | None) -> List[str]:
    """Genera el informe final legible para el usuario en formato de texto."""
    if not isinstance(result, HealthResult):
        return ["Error: Informe de salud no disponible."]
    
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in WEIGHTS.items():
        puntos = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{_render_bar(puntos, maximo)}]")
    
    recs = result.recommendations if result.recommendations else ["Sin recomendaciones."]
    lines.extend(["", "Recomendaciones:", *(f"  - {r}" for r in recs)])
    return lines
