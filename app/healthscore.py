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
import sys
from functools import lru_cache

ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Valor de salud normalizado entre 0.0 (crítico) y 1.0 (óptimo)"]
MetricKey: TypeAlias = str

# Constantes de rendimiento para validación
_ALLOWED_CHARS: Final[set[str]] = set(string.ascii_letters + string.digits + " .,:()-_%")
_BLOCKED_TERMS: Final[frozenset[str]] = frozenset([
    "system32", "windows", "admin", "c:\\", "system", "kernel", "path", 
    "root", "drive", "user", "profile", "appdata", "programdata", "exe", "dll"
])
_MAX_MSG_LEN: Final[int] = 128
_MAX_RECURSION_DEPTH: Final[int] = 5

# Límites de validación para métricas del sistema
_METRIC_LIMITS: Final[Dict[str, Tuple[float, float]]] = {
    "junk_mb": (0.0, 1048576.0),
    "duplicate_mb": (0.0, 1048576.0),
    "suspicious_count": (0.0, 5000.0),
    "suspicious_warnings": (0.0, 5000.0),
    "startup_count": (0.0, 1000.0),
    "memory_available_percent": (0.0, 100.0),
    "disk_free_percent": (0.0, 100.0),
    "quarantined_count": (0.0, 10000.0)
}

class Scorer(Protocol):
    """
    Protocolo funcional para transformar SystemMetrics en un ratio de salud [0.0, 1.0].
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
    def from_score(cls, score: float | int | None) -> str:
        """
        Convierte una puntuación numérica a su categoría alfabética equivalente.
        
        Args:
            score: Valor numérico de 0 a 100.
        Returns:
            str: Categoría de 'A' (excelente) a 'F' (crítico).
        """
        if score is None:
            return cls.F.value
        try:
            val = float(score)
            if not math.isfinite(val): return cls.F.value
        except (TypeError, ValueError):
            return cls.F.value
        if val >= 90: return cls.A.value
        if val >= 80: return cls.B.value
        if val >= 65: return cls.C.value
        if val >= 50: return cls.D.value
        return cls.F.value

class RecommendationRule(NamedTuple):
    """
    Define una lógica de diagnóstico reactiva: 
    - area: Identificador del componente (ej. 'disco', 'seguridad').
    - threshold: Valor de ratio [0, 1] que activa la advertencia.
    - message_factory: Función que extrae datos de SystemMetrics para redactar el mensaje.
    - check: Predicado que evalúa la métrica frente al ratio para decidir el disparo.
    """
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """
    Estructura de procesamiento para cada métrica en el sistema, vinculando 
    la métrica con su función de cálculo y sus reglas asociadas.
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

# Umbrales críticos para la normalización de métricas
_LIMIT_JUNK_MB: Final[float] = 5000.0        
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0   
_LIMIT_STARTUP_COUNT: Final[int] = 20        
_LIMIT_RAM_PERCENT: Final[float] = 35.0      
_LIMIT_DISK_PERCENT: Final[float] = 25.0     

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """
    Asegura que un valor numérico se encuentre dentro del rango [min_val, max_val].
    Maneja entradas no finitas devolviendo el valor mínimo por seguridad.
    """
    if not isinstance(value, (int, float)) or not math.isfinite(value): return min_val
    return float(max(min_val, min(max_val, value)))

def create_linear_scorer(limit: float, inverse: bool = True) -> Callable[[float], NormalizedRatio]:
    """
    Fábrica que genera funciones de normalización lineal.
    Si inverse es True, un valor mayor al límite resulta en una salud menor (penalización).
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

def _verify_pipeline_integrity(weights: Dict[str, int], pipeline: Tuple[PipelineEntry, ...]) -> None:
    """
    Validación interna para asegurar que la configuración de pesos y el pipeline
    estén sincronizados y sumen 100%. Se ejecuta al cargar el módulo.
    """
    if not isinstance(weights, dict) or not isinstance(pipeline, tuple):
        return
    try:
        if sum(weights.values()) != 100:
            logging.error("Integridad de Pipeline fallida: Los pesos no suman 100.")
            return
        pipeline_areas = {entry.area for entry in pipeline}
        if set(weights.keys()) != pipeline_areas:
            logging.error("Integridad de Pipeline fallida: Desajuste de áreas.")
    except Exception as error:
        logging.error(f"Error crítico validando integridad del pipeline: {error}")

_JUNK_SCORER = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

def _msg_seguridad(metrics: SystemMetrics) -> str: return f"Riesgos de seguridad detectados: {metrics.suspicious_count}."
def _msg_disco(metrics: SystemMetrics) -> str: return f"Espacio libre en disco bajo: {metrics.disk_free_percent:.1f} porciento."
def _msg_memoria(metrics: SystemMetrics) -> str: return "Memoria disponible baja, optimice sus procesos."
def _msg_basura(metrics: SystemMetrics) -> str: return f"Acumulacion de archivos temporales: {metrics.junk_mb:.0f} MB."
def _msg_duplicados(metrics: SystemMetrics) -> str: return f"Recuperacion posible: {metrics.duplicate_mb:.0f} MB en duplicados."
def _msg_arranque(metrics: SystemMetrics) -> str: return f"Exceso de programas en arranque: {metrics.startup_count} entradas."

_RULES: Final[Dict[str, Tuple[RecommendationRule, ...]]] = {
    "seguridad": (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, _msg_seguridad, lambda m, r: r < WARN_THRESHOLD_HIGH),),
    "disco": (RecommendationRule("disco", WARN_THRESHOLD_LOW, _msg_disco, lambda m, r: r < WARN_THRESHOLD_LOW),),
    "memoria": (RecommendationRule("memoria", WARN_THRESHOLD_LOW, _msg_memoria, lambda m, r: r < WARN_THRESHOLD_LOW),),
    "basura": (RecommendationRule("basura", WARN_THRESHOLD_MED, _msg_basura, lambda m, r: r < WARN_THRESHOLD_MED),),
    "duplicados": (RecommendationRule("duplicados", WARN_THRESHOLD_MED, _msg_duplicados, lambda m, r: r < WARN_THRESHOLD_MED),),
    "arranque": (RecommendationRule("arranque", WARN_THRESHOLD_LOW, _msg_arranque, lambda m, r: r < WARN_THRESHOLD_LOW),),
}

PIPELINE_DEFINITIONS: Final[Tuple[PipelineEntry, ...]] = (
    PipelineEntry("seguridad", 30, lambda m: score_security(m.suspicious_count, m.suspicious_warnings), _RULES["seguridad"]),
    PipelineEntry("disco", 20, lambda m: score_disk(m.disk_free_percent), _RULES["disco"]),
    PipelineEntry("memoria", 18, lambda m: score_memory(m.memory_available_percent), _RULES["memoria"]),
    PipelineEntry("basura", 14, lambda m: score_junk(m.junk_mb), _RULES["basura"]),
    PipelineEntry("duplicados", 10, lambda m: score_duplicates(m.duplicate_mb), _RULES["duplicados"]),
    PipelineEntry("arranque", 8, lambda m: score_startup(m.startup_count), _RULES["arranque"]),
)

_verify_pipeline_integrity(WEIGHTS, PIPELINE_DEFINITIONS)

def score_junk(junk_mb: float | int) -> NormalizedRatio: 
    """Calcula salud: [0.0, 1.0] basado en archivos temporales (inverso: >5GB es 0.0)."""
    return _JUNK_SCORER(float(junk_mb) if isinstance(junk_mb, (int, float)) else 0.0)

def score_security(suspicious_count: int | float, warnings: int | float = 0) -> NormalizedRatio: 
    """Calcula salud: [0.0, 1.0] penalizando conteo de archivos sospechosos y advertencias de sistema."""
    s_count = float(suspicious_count) if isinstance(suspicious_count, (int, float)) else 0.0
    w_count = float(warnings) if isinstance(warnings, (int, float)) else 0.0
    penalization = (max(0.0, s_count) * 0.05) + (max(0.0, w_count) * 0.25)
    return _clamp(1.0 - penalization)

def score_memory(available_percent: float | int) -> NormalizedRatio: 
    """Calcula salud: [0.0, 1.0] basado en porcentaje de RAM libre disponible."""
    val = float(available_percent) if isinstance(available_percent, (int, float)) else 0.0
    if _LIMIT_RAM_PERCENT <= 0.0 or not math.isfinite(val): return 0.0
    return _clamp(val / _LIMIT_RAM_PERCENT)

def score_disk(free_percent: float | int) -> NormalizedRatio: 
    """Calcula salud: [0.0, 1.0] basado en porcentaje de espacio libre en disco."""
    val = float(free_percent) if isinstance(free_percent, (int, float)) else 0.0
    if _LIMIT_DISK_PERCENT <= 0.0 or not math.isfinite(val): return 0.0
    return _clamp(val / _LIMIT_DISK_PERCENT)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    """Calcula salud: [0.0, 1.0] basado en volumen de duplicados (inverso: >2GB es 0.0)."""
    return _DUP_SCORER(float(duplicate_mb) if isinstance(duplicate_mb, (int, float)) else 0.0)

def score_startup(startup_count: int | float) -> NormalizedRatio: 
    """Calcula salud: [0.0, 1.0] basado en cantidad de apps en inicio (inverso: >20 es 0.0)."""
    return _STARTUP_SCORER(float(startup_count) if isinstance(startup_count, (int, float)) else 0.0)

def _validate_numeric(value: Any, default: float, min_v: float, max_v: float) -> float:
    """Sanitiza métricas numéricas evitando valores fuera de rango o NaN."""
    try:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return default
        val = float(value)
        if not math.isfinite(val):
            return default
        return float(max(min_v, min(max_v, val)))
    except (ValueError, TypeError):
        return default

@dataclass
class SystemMetrics:
    """
    Contenedor de datos inmutable utilizado como entrada para el análisis.
    Realiza una validación automática post-inicialización mediante `validate()`.
    """
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
        """Asegura integridad post-instanciación."""
        self.validate()

    def safe_get(self, field_name: str, default: Any = 0.0) -> Any:
        """Acceso restringido para el motor de scoring usando lista blanca interna."""
        if field_name in self._FIELDS_TO_VALIDATE:
            val = getattr(self, field_name, default)
            return val if isinstance(val, (int, float)) else default
        return default

    def validate(self) -> None:
        """Normaliza todas las métricas según los rangos definidos en _METRIC_LIMITS."""
        for field_name in self._FIELDS_TO_VALIDATE:
            val = getattr(self, field_name, 0.0)
            min_v, max_v = _METRIC_LIMITS.get(field_name, (0.0, 1e9))
            sanitized = _validate_numeric(val, 0.0, min_v, max_v)
            object.__setattr__(self, field_name, sanitized)

    @property
    def is_finite(self) -> bool:
        return all(math.isfinite(float(getattr(self, f, 0.0))) for f in self._FIELDS_TO_VALIDATE)

@dataclass
class HealthResult:
    """Resultado consolidado del análisis de salud, inmutable tras su creación."""
    score: int = 0
    grade: str = "F"
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=lambda: ["Error: Datos no inicializados."])

    @property
    def is_healthy(self) -> bool: 
        return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: 
    """Wrapper para obtener la categoría de nota según el puntaje."""
    return Grade.from_score(score)

def _is_safe_string_for_report(text: str) -> bool:
    """Verifica si el mensaje contiene términos protegidos o rutas sensibles."""
    t_low = text.lower()
    return not any(term in t_low for term in _BLOCKED_TERMS)

def _sanitize_msg(msg: str | Any) -> str:
    """Filtro de seguridad que limpia caracteres no permitidos y bloquea términos sensibles."""
    if not isinstance(msg, str) or len(msg) > (_MAX_MSG_LEN * 4): 
        return ""
    clean = "".join(char for char in msg if char in _ALLOWED_CHARS)
    if not _is_safe_string_for_report(clean):
        return ""
    return clean.strip()[:_MAX_MSG_LEN]

def _apply_rule_safely(metrics: SystemMetrics, rule: RecommendationRule) -> str:
    """Ejecutor de reglas con aislamiento (sandbox) para evitar fugas de información."""
    try:
        raw_res = rule.message_factory(metrics)
        if not _is_safe_string_for_report(raw_res):
            return ""
        return _sanitize_msg(raw_res)
    except Exception: return ""

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], normalized_ratio: NormalizedRatio) -> List[str]:
    """Evalúa las reglas registradas y retorna solo las que disparan advertencias."""
    if not isinstance(rules, tuple): return []
    ratio = _clamp(normalized_ratio)
    findings: List[str] = []
    for rule in rules:
        try:
            if rule.check(metrics, ratio):
                clean_msg = _apply_rule_safely(metrics, rule)
                if clean_msg: findings.append(clean_msg)
        except Exception:
            continue
    return findings

@lru_cache(maxsize=128)
def _compute_cached(metric_tuple: Tuple[float, ...], quarantined: int) -> HealthResult:
    """
    Motor central que orquestra el cálculo de métricas. 
    Usa caché LRU para optimizar el rendimiento al recibir métricas idénticas.
    """
    keys = SystemMetrics._FIELDS_TO_VALIDATE[:-1]
    
    # Reconstrucción segura de métricas desde tuplas validadas
    try:
        m_args = {k: v for k, v in zip(keys, metric_tuple)}
        metrics = SystemMetrics(**m_args, quarantined_count=float(quarantined))
    except Exception:
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Formato de métricas inconsistente."])
    
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    total_points = 0.0
    
    for entry in PIPELINE_DEFINITIONS:
        try:
            area_ratio = entry.scorer(metrics)
            recs = _evaluate_rules(metrics, entry.rules, area_ratio)
            recommendations.extend(recs)
            points = area_ratio * float(entry.weight)
            val_int = int(_clamp(round(points), 0.0, float(entry.weight)))
            metric_breakdown[entry.area] = val_int
            total_points += float(val_int)
        except Exception:
            continue
            
    if metrics.quarantined_count > 0:
        recommendations.append("Existen elementos en cuarentena.")
        
    final_score = int(_clamp(round(total_points), 0.0, 100.0))
    
    return HealthResult(
        score=final_score,
        grade=grade_for_score(final_score),
        breakdown=metric_breakdown,
        recommendations=recommendations if recommendations else ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Punto de entrada principal para el análisis del sistema."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Formato de métricas inválido."])
    
    metrics.validate()
    try:
        # Extraer mediante lista blanca para asegurar integridad absoluta en caché
        raw_vals = tuple(float(metrics.safe_get(f)) for f in SystemMetrics._FIELDS_TO_VALIDATE[:-1])
        return _compute_cached(raw_vals, int(metrics.safe_get("quarantined_count")))
    except (TypeError, ValueError, AttributeError):
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Fallo al procesar métricas."])

@lru_cache(maxsize=128)
def _render_bar(points: Any, max_val: Any) -> str:
    """Generador visual de barras de progreso para reportes en texto."""
    if not isinstance(points, (int, float)) or not isinstance(max_val, (int, float)):
        return "." * 10
    
    safe_max = int(max(1, int(max_val)))
    safe_points = int(max(0, min(int(points), safe_max)))
    return "#" * safe_points + "." * (safe_max - safe_points)

def summarize(result: HealthResult | None) -> List[str]:
    """Serializa el resultado de salud en una lista de líneas para informe legible."""
    if not isinstance(result, HealthResult): 
        return ["Error: Informe de salud no disponible."]
    
    try:
        lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
        for entry in PIPELINE_DEFINITIONS:
            val = result.breakdown.get(entry.area, 0)
            lines.append(f"  {entry.area.capitalize():<12} {val:>2}/{entry.weight:<2} [{_render_bar(val, entry.weight)}]")
        
        recs = result.recommendations if isinstance(result.recommendations, list) else []
        if not recs:
            recs = ["No hay advertencias activas."]
            
        lines.extend(("", "Recomendaciones:", *[f"  - {s}" for s in recs]))
        return lines
    except Exception:
        return ["Error: Fallo crítico al generar resumen de informe."]

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.info("Módulo healthscore cargado. Motor analítico inicializado.")
