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
        check: Predicado (SystemMetrics, NormalizedRatio) -> bool para decidir si alertar.
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

# Umbrales máximos tolerables antes de penalizar totalmente el score
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

# Niveles de ratio de salud para disparar advertencias (0.0 a 1.0)
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

_WEIGHTS_LIST: Final[Tuple[Tuple[MetricKey, int], ...]] = tuple(WEIGHTS.items())

def _verify_weights(weights: Dict[str, int]) -> None:
    """Valida la integridad de la configuración de pesos."""
    if sum(weights.values()) != 100:
        raise ValueError("La suma de pesos en WEIGHTS debe ser estrictamente 100.")

_verify_weights(WEIGHTS)

_JUNK_SCORER = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

_PIPELINE: Final[Tuple[PipelineEntry, ...]] = (
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
)

def score_junk(junk_mb: float | int) -> NormalizedRatio: 
    """Calcula salud: Normaliza el volumen de basura (MB) vs el límite definido."""
    return _JUNK_SCORER(float(junk_mb))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio: 
    """Calcula salud: Aplica penalización incremental basada en cantidad de hallazgos sospechosos."""
    try:
        s_count = float(suspicious_count) if isinstance(suspicious_count, (int, float)) else 0.0
        w_count = float(warnings) if isinstance(warnings, (int, float)) else 0.0
        penalization = (s_count * 0.05) + (w_count * 0.25)
        return _clamp(1.0 - penalization)
    except (TypeError, ValueError, OverflowError):
        return 0.0

def score_memory(available_percent: float | int) -> NormalizedRatio: 
    """Calcula salud: Normaliza el porcentaje de memoria libre sobre el umbral mínimo aceptable."""
    return _clamp(float(available_percent) / _LIMIT_RAM_PERCENT)

def score_disk(free_percent: float | int) -> NormalizedRatio: 
    """Calcula salud: Normaliza el porcentaje de disco libre sobre el umbral de advertencia."""
    return _clamp(float(free_percent) / _LIMIT_DISK_PERCENT)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    """Calcula salud: Normaliza el peso total de duplicados frente a la capacidad de recuperación."""
    return _DUP_SCORER(float(duplicate_mb))

def score_startup(startup_count: int | float) -> NormalizedRatio: 
    """Calcula salud: Normaliza la carga del inicio de Windows comparado con el límite de tolerancia."""
    return _STARTUP_SCORER(float(startup_count))

def _validate_numeric(value: Any, default: float, min_v: float, max_v: float) -> float:
    """Helper para sanitizar métricas numéricas entrantes y evitar valores fuera de rango."""
    try:
        if value is None or not isinstance(value, (int, float)):
            return default
        val = float(value)
        if not math.isfinite(val) or val < min_v or val > max_v:
            return default
        return val
    except (ValueError, TypeError):
        return default

@dataclass
class SystemMetrics:
    """Contenedor de datos centralizado para las métricas del sistema."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    _FIELDS_TO_VALIDATE: Final[Tuple[str, ...]] = (
        "junk_mb", "suspicious_count", "suspicious_warnings", 
        "memory_available_percent", "disk_free_percent", 
        "duplicate_mb", "startup_count", "quarantined_count"
    )

    def __post_init__(self) -> None:
        try:
            self.validate()
        except Exception as e:
            logging.error(f"Error crítico en validación de métricas: {e}")
            for field_name in self._FIELDS_TO_VALIDATE:
                setattr(self, field_name, 0.0)

    def safe_get(self, field_name: str, default: Any = 0) -> Any:
        """Acceso defensivo a los campos del contenedor."""
        return getattr(self, field_name, default)

    def validate(self) -> None:
        """Asegura rangos aceptables para todas las métricas, evitando inyección de datos fuera de escala."""
        self.junk_mb = _validate_numeric(self.junk_mb, 0.0, 0.0, 1048576.0)
        self.duplicate_mb = _validate_numeric(self.duplicate_mb, 0.0, 0.0, 1048576.0)
        self.suspicious_count = int(_validate_numeric(self.suspicious_count, 0, 0, 5000))
        self.suspicious_warnings = int(_validate_numeric(self.suspicious_warnings, 0, 0, 5000))
        self.startup_count = int(_validate_numeric(self.startup_count, 0, 0, 1000))
        self.quarantined_count = int(_validate_numeric(self.quarantined_count, 0, 0, 1000))
        self.memory_available_percent = _validate_numeric(self.memory_available_percent, 100.0, 0.0, 100.0)
        self.disk_free_percent = _validate_numeric(self.disk_free_percent, 100.0, 0.0, 100.0)

    @property
    def is_finite(self) -> bool:
        """Verifica que ninguna métrica numérica sea infinita o NaN."""
        return all(math.isfinite(getattr(self, f)) for f in self._FIELDS_TO_VALIDATE if isinstance(getattr(self, f), (int, float)))

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

def _sanitize_msg(msg: str) -> str:
    """Elimina caracteres no imprimibles y trunca el mensaje."""
    if not isinstance(msg, str): return ""
    sanitized = "".join(c for c in msg if c.isprintable() and c not in "\r\n\t").strip()
    return sanitized[:200] if sanitized else ""

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], normalized_ratio: NormalizedRatio, findings: List[str]) -> None:
    """Ejecuta las reglas de diagnóstico."""
    for rule in rules:
        try:
            if rule.check(metrics, normalized_ratio):
                clean_msg = _sanitize_msg(rule.message_factory(metrics))
                if clean_msg: findings.append(clean_msg)
        except Exception as e:
            logging.error(f"Falla en evaluación de regla {rule.area}: {e}")

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """
    Calcula el puntaje global mediante la ejecución del pipeline con manejo estricto de errores.
    """
    try:
        m = metrics if isinstance(metrics, SystemMetrics) else SystemMetrics()
        if not m.is_finite:
            m.validate()
            
        recommendations: List[str] = []
        metric_breakdown: Dict[MetricKey, int] = {}
        accumulated_score: float = 0.0
        
        for entry in _PIPELINE:
            try:
                area_ratio = _clamp(float(entry.scorer(m)))
                _evaluate_rules(m, entry.rules, area_ratio, recommendations)
                points = area_ratio * entry.weight
                val = int(round(_clamp(points, 0.0, float(entry.weight))))
                metric_breakdown[entry.area] = val
                accumulated_score += float(val)
            except Exception as e:
                logging.error(f"Falla crítica en pipeline {entry.area}: {e}")
                metric_breakdown[entry.area] = 0
                    
        if m.quarantined_count > 0:
            recommendations.append(f"Tenés {m.quarantined_count} archivo(s) en cuarentena.")
            
        final_score = int(round(_clamp(accumulated_score, 0.0, 100.0)))
        return HealthResult(
            score=final_score,
            grade=grade_for_score(final_score),
            breakdown=metric_breakdown,
            recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
        )
    except Exception as e:
        logging.critical(f"Error fatal al calcular puntaje: {e}")
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error interno al calcular salud del sistema."])

def _render_bar(points: int, max_val: int) -> str:
    """Representación visual: barra de caracteres ASCII para la interfaz con manejo de errores."""
    try:
        if not isinstance(points, int) or not isinstance(max_val, int) or max_val <= 0:
            return ".........."
        p = max(0, min(points, max_val))
        return "#" * p + "." * (max_val - p)
    except Exception:
        return ".........."

def summarize(result: HealthResult | None) -> List[str]:
    """Genera una lista de cadenas legible para el informe de estado final."""
    if not isinstance(result, HealthResult): 
        return ["Error: Informe de salud no disponible."]
        
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    
    for area, maximo in _WEIGHTS_LIST:
        p = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {p:>2}/{maximo:<2} [{_render_bar(p, maximo)}]")
    
    lines.extend(("", "Recomendaciones:", *(f"  - {r}" for r in result.recommendations)))
    return lines
