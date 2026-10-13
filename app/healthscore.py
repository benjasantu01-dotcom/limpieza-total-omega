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
from pathlib import Path
import math
import logging
import string

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
        """
        Convierte una puntuación numérica a su categoría alfabética equivalente.
        
        Args:
            score: Valor numérico (int o float) a clasificar.
        Returns:
            String representando la nota (A-F). F si el input es inválido.
        """
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
        area: Identificador de la métrica analizada (ej: 'disco').
        threshold: Límite inferior de salud para activar la sugerencia.
        message_factory: Callable que recibe las métricas completas y retorna un string.
        check: Predicado (SystemMetrics, NormalizedRatio) -> bool que evalúa si activar alerta.
    """
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """
    Configuración completa de una unidad de análisis dentro del motor de salud.
    Incluye el peso relativo, el normalizador (scorer) y las reglas de diagnóstico.
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

# Umbrales críticos para la normalización de métricas
_LIMIT_JUNK_MB: Final[float] = 5000.0        # Límite superior de basura (MB) antes de puntaje 0
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0   # Límite superior de duplicados (MB) antes de puntaje 0
_LIMIT_STARTUP_COUNT: Final[int] = 20        # Cantidad máxima tolerada de programas de inicio
_LIMIT_RAM_PERCENT: Final[float] = 35.0      # Porcentaje de RAM libre base considerado "saludable"
_LIMIT_DISK_PERCENT: Final[float] = 25.0     # Porcentaje de espacio libre base considerado "saludable"

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Asegura que un valor se mantenga dentro del intervalo de normalización [0.0, 1.0]."""
    if not math.isfinite(value): return min_val
    return max(min_val, min(max_val, value))

def create_linear_scorer(limit: float, inverse: bool = True) -> Callable[[float], NormalizedRatio]:
    """
    Fábrica para crear normalizadores lineales de métricas.
    
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

# Pesos relativos de cada área en el score global (debe sumar 100)
WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

def _verify_weights(weights: Dict[str, int]) -> None:
    """Valida que la suma de pesos configurados sea exactamente 100."""
    if sum(weights.values()) != 100:
        raise ValueError("La suma de pesos en WEIGHTS debe ser estrictamente 100.")

_verify_weights(WEIGHTS)

_JUNK_SCORER = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

# Pipeline de procesamiento central: define cómo se computa cada métrica y sus reglas de alerta
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
    """Calcula salud: Normaliza el volumen de basura (MB) comparándolo contra el límite total."""
    return _JUNK_SCORER(float(junk_mb) if isinstance(junk_mb, (int, float)) else 0.0)

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio: 
    """Calcula salud: Penaliza el score según hallazgos sospechosos y advertencias de seguridad."""
    s_count = max(0, int(suspicious_count))
    w_count = max(0, int(warnings))
    penalization = (s_count * 0.05) + (w_count * 0.25)
    return _clamp(1.0 - penalization)

def score_memory(available_percent: float | int) -> NormalizedRatio: 
    """Calcula salud: Normaliza el porcentaje de RAM libre frente al umbral crítico definido."""
    val = float(available_percent) if isinstance(available_percent, (int, float)) else 0.0
    if _LIMIT_RAM_PERCENT <= 0 or not math.isfinite(val): return 0.0
    return _clamp(val / _LIMIT_RAM_PERCENT)

def score_disk(free_percent: float | int) -> NormalizedRatio: 
    """Calcula salud: Normaliza el porcentaje de disco libre frente al umbral de advertencia."""
    val = float(free_percent) if isinstance(free_percent, (int, float)) else 0.0
    if _LIMIT_DISK_PERCENT <= 0 or not math.isfinite(val): return 0.0
    return _clamp(val / _LIMIT_DISK_PERCENT)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    """Calcula salud: Normaliza el volumen de duplicados detectados frente a la capacidad de recuperación."""
    return _DUP_SCORER(float(duplicate_mb) if isinstance(duplicate_mb, (int, float)) else 0.0)

def score_startup(startup_count: int | float) -> NormalizedRatio: 
    """Calcula salud: Normaliza la cantidad de elementos de inicio contra el límite de tolerancia."""
    return _STARTUP_SCORER(float(startup_count) if isinstance(startup_count, (int, float)) else 0.0)

def _validate_numeric(value: Any, default: float, min_v: float, max_v: float) -> float:
    """Helper para sanitizar métricas numéricas entrantes y evitar valores fuera de rango."""
    try:
        if value is None or isinstance(value, bool):
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
        self.validate()

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
        return all(math.isfinite(float(getattr(self, f))) for f in self._FIELDS_TO_VALIDATE)

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
    """Wrapper de conveniencia para invocar el selector de grados según puntaje."""
    return Grade.from_score(score)

def _sanitize_msg(msg: str) -> str:
    """Limpia el mensaje usando una whitelist estricta de caracteres permitidos."""
    if not isinstance(msg, str): return ""
    
    # Whitelist: caracteres alfanuméricos básicos, espacios y puntuación mínima segura
    allowed = string.ascii_letters + string.digits + " .,:()-_%"
    
    sanitized = "".join(c for c in msg if c in allowed)
    
    # Bloqueo de palabras clave prohibidas independientemente de los caracteres
    blocked = ["system32", "windows", "admin", "c:\\"]
    if any(b in sanitized.lower() for b in blocked):
        return ""
        
    return sanitized.strip()[:200]

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], normalized_ratio: NormalizedRatio, findings: List[str]) -> None:
    """Ejecuta las reglas de diagnóstico para una métrica y agrega hallazgos si es necesario."""
    for rule in rules:
        try:
            if rule.check(metrics, normalized_ratio):
                clean_msg = _sanitize_msg(rule.message_factory(metrics))
                if clean_msg: findings.append(clean_msg)
        except Exception as e:
            logging.warning(f"Salteando regla de diagnóstico en {rule.area} por error: {e}")

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Calcula el puntaje global recorriendo el pipeline de métricas."""
    m = metrics if isinstance(metrics, SystemMetrics) else SystemMetrics()
    try:
        m.validate()
    except Exception as e:
        logging.error(f"Fallo en validación de métricas: {e}")
        return HealthResult(0, "F", {}, ["Error interno en validación de métricas."])
    
    if not m.is_finite:
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Métricas inestables detectadas."])
        
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    accumulated_score: float = 0.0
    
    for entry in _PIPELINE:
        try:
            area_ratio = _clamp(entry.scorer(m))
        except Exception as e:
            logging.error(f"Error procesando normalizador para {entry.area}: {e}")
            area_ratio = 0.0
            
        _evaluate_rules(m, entry.rules, area_ratio, recommendations)
        
        # Cálculo discreto
        points = area_ratio * float(entry.weight)
        val_int = int(round(_clamp(points, 0.0, float(entry.weight))))
        metric_breakdown[entry.area] = val_int
        accumulated_score += float(val_int)
                
    if m.quarantined_count > 0:
        recommendations.append(f"Tenés {m.quarantined_count} archivo(s) en cuarentena.")
        
    final_score = int(round(_clamp(accumulated_score, 0.0, 100.0)))
    return HealthResult(
        score=final_score,
        grade=grade_for_score(final_score),
        breakdown=metric_breakdown,
        recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

def _render_bar(points: int, max_val: int) -> str:
    """Genera representación visual (ASCII) de barras para la interfaz."""
    p = max(0, min(int(points), int(max_val)))
    return "#" * p + "." * (int(max_val) - p) if max_val > 0 else ".........."

def summarize(result: HealthResult | None) -> List[str]:
    """Genera una lista de líneas legible para el informe de estado final del sistema."""
    if not isinstance(result, HealthResult): 
        return ["Error: Informe de salud no disponible."]
        
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    
    # Iteración sobre breakdown pre-calculado, con fallback para claves faltantes
    for area in WEIGHTS:
        p = result.breakdown.get(area, 0)
        maximo = WEIGHTS[area]
        lines.append(f"  {area.capitalize():<12} {p:>2}/{maximo:<2} [{_render_bar(p, maximo)}]")
    
    lines.extend(("", "Recomendaciones:", *(f"  - {r}" for r in result.recommendations)))
    return lines
