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
        """
        Convierte un puntaje numérico al grado alfabético correspondiente.
        
        Usa escalas estándar: A (>=90), B (>=80), C (>=65), D (>=50), F (<50).
        """
        s = float(score)
        if s >= 90: return cls.A.value
        if s >= 80: return cls.B.value
        if s >= 65: return cls.C.value
        if s >= 50: return cls.D.value
        return cls.F.value

class RecommendationRule(NamedTuple):
    """
    Define una regla lógica para generar advertencias al usuario.
    
    Attributes:
        area: Dominio afectado (seguridad, disco, etc.).
        threshold: Ratio debajo del cual la regla se vuelve activa.
        message_factory: Callable que recibe las métricas y retorna el string de aviso.
        check: Predicado (metrics, ratio) que determina si la regla debe aplicarse.
    """
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """
    Representa una etapa de procesamiento dentro del motor de salud.
    
    Attributes:
        area: Identificador del tipo de métrica.
        weight: Peso porcentual en el puntaje total (suma debe ser 100).
        scorer: Función de normalización aplicada a este dominio.
        rules: Colección de reglas de recomendación a evaluar.
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

_LIMIT_JUNK_MB: Final[float] = 5000.0
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0
_LIMIT_STARTUP_COUNT: Final[int] = 20
_LIMIT_RAM_PERCENT: Final[float] = 35.0
_LIMIT_DISK_PERCENT: Final[float] = 25.0

def _safe_inv(val: float, fallback: float = 1.0) -> float:
    """Calcula 1/val asegurando que no ocurran divisiones por cero o valores no finitos."""
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
    """Normaliza el volumen de basura: puntaje decreciente linealmente según el umbral."""
    return _clamp(1.0 - (float(junk_mb) * _INV_JUNK))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Calcula salud de seguridad: penaliza hallazgos y advertencias con pesos ponderados."""
    penalization = (float(suspicious_count) * 0.05) + (float(warnings) * 0.25)
    return _clamp(1.0 - _clamp(penalization, 0.0, 1.0))

def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Normaliza salud de memoria: puntaje basado en porcentaje de RAM libre disponible."""
    return _clamp(float(available_percent) * _INV_RAM)

def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Normaliza salud de disco: puntaje basado en el porcentaje de espacio libre disponible."""
    return _clamp(float(free_percent) * _INV_DISK)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Normaliza salud de duplicados: penaliza el almacenamiento redundante detectado."""
    return _clamp(1.0 - (float(duplicate_mb) * _INV_DUP))

def score_startup(startup_count: int | float) -> NormalizedRatio:
    """Normaliza salud de arranque: penaliza linealmente el número de programas en inicio."""
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

if len(_PIPELINE) != len(WEIGHTS):
    raise RuntimeError("Desalineación crítica entre el Pipeline de evaluación y los pesos definidos.")

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
        """Valida y ajusta las métricas tras la inicialización del objeto."""
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
        """Verifica que todos los campos numéricos sean números finitos y válidos."""
        values = (self.junk_mb, self.suspicious_count, self.suspicious_warnings, 
                  self.memory_available_percent, self.disk_free_percent, self.duplicate_mb, 
                  self.startup_count, self.quarantined_count)
        return all(math.isfinite(v) for v in values)

@dataclass
class HealthResult:
    """Resultado final del motor de salud."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Verifica si el puntaje global alcanza un estándar de salud aceptable (>= 80)."""
        return 80 <= self.score <= 100

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Acota un valor numérico a un rango definido [min_val, max_val]."""
    val = float(value)
    if not math.isfinite(val) or math.isnan(val): return min_val
    return max(min_val, min(val, max_val))

def _to_float(value: Any, default: float = 0.0) -> float:
    """Conversor defensivo a float, retornando default ante entradas corruptas."""
    try:
        val = float(value)
        return val if (math.isfinite(val) and not math.isnan(val)) else default
    except (TypeError, ValueError, OverflowError): return default

def grade_for_score(score: float | int) -> str:
    """Helper para obtener el grado alfabético mediante la clase Grade."""
    return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: List[RecommendationRule], ratio: NormalizedRatio, findings: List[str]) -> None:
    """Ejecuta reglas de recomendación para un área, sanitizando los mensajes resultantes."""
    if not isinstance(metrics, SystemMetrics) or not isinstance(rules, list):
        return

    for rule in rules:
        if not isinstance(rule, RecommendationRule):
            continue
        try:
            if rule.check(metrics, ratio):
                raw_msg = rule.message_factory(metrics)
                if isinstance(raw_msg, str):
                    clean_msg = "".join(c for c in raw_msg if c.isprintable()).strip()
                    if clean_msg:
                        findings.append(clean_msg[:200])
        except (AttributeError, TypeError, ValueError, ZeroDivisionError, ArithmeticError):
            continue

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Ejecuta el Pipeline de salud sobre las métricas y devuelve el resultado unificado."""
    if metrics is None or not isinstance(metrics, SystemMetrics) or not metrics.is_finite:
        return HealthResult(0, "F", {k: 0 for k in WEIGHTS}, ["Error: Configuración o métricas no válidas."])
    
    metrics.validate()
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    accumulated_score: int = 0
    
    for entry in _PIPELINE:
        try:
            area_ratio = _clamp(entry.scorer(metrics), 0.0, 1.0)
        except (ValueError, TypeError, ZeroDivisionError, ArithmeticError):
            area_ratio = 0.0
            
        if entry.rules:
            _evaluate_rules(metrics, entry.rules, area_ratio, recommendations)
        
        weighted_points = int(round(area_ratio * entry.weight))
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
    """Genera una barra de progreso visual (ASCII) basada en puntos y un máximo."""
    safe_max = max(1, max_val)
    p = max(0, min(points, safe_max))
    return ('#' * p) + ('.' * (safe_max - p))

def summarize(result: HealthResult | None) -> List[str]:
    """Genera una lista de líneas formateadas para el reporte de texto en la UI."""
    if not isinstance(result, HealthResult):
        return ["Error: Informe de salud no disponible."]
    
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    bd = result.breakdown
    for area, maximo in WEIGHTS.items():
        points = bd.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {points:>2}/{maximo:<2} [{_render_bar(points, maximo)}]")
    
    recs = result.recommendations or ["Sin recomendaciones."]
    lines.extend(("", "Recomendaciones:", *(f"  - {r}" for r in recs)))
    return lines
