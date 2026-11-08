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
from functools import lru_cache, wraps

# Tipos definidos para mejorar la legibilidad de la firma de funciones
ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Valor de salud normalizado entre 0.0 (crítico) y 1.0 (óptimo)"]
MetricKey: TypeAlias = str

# --- Constantes y Límites ---
_ALLOWED_CHARS: Final[set[str]] = set(string.ascii_letters + string.digits + " .,:()-_%")
_BLOCKED_TERMS: Final[frozenset[str]] = frozenset([
    "system32", "windows", "admin", "c:\\", "system", "kernel", "path", 
    "root", "drive", "user", "profile", "appdata", "programdata", "exe", "dll",
    "boot", "windows", "users", "admin", "secret", "private"
])
_MAX_MSG_LEN: Final[int] = 128

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

_VALIDATION_KEYS: Final[Tuple[str, ...]] = (
    "junk_mb", "suspicious_count", "suspicious_warnings", 
    "memory_available_percent", "disk_free_percent", 
    "duplicate_mb", "startup_count"
)
_ALL_METRICS: Final[Tuple[str, ...]] = _VALIDATION_KEYS + ("quarantined_count",)

# --- Decoradores y Utilidades de Seguridad ---

def _is_finite_numeric(val: Any) -> bool:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    return math.isfinite(float(val))

def _guard_metric_access(func: Callable[[Any, str, Any], Any]) -> Callable[[Any, str, Any], Any]:
    @wraps(func)
    def wrapper(instance: Any, field_name: str, default: Any = 0.0) -> Any:
        if field_name not in _ALL_METRICS:
            return default
        return func(instance, field_name, default)
    return wrapper

def _is_sensitive_input(data: Any) -> bool:
    if not isinstance(data, str): return False
    d_low = data.lower()
    return any(term in d_low for term in _BLOCKED_TERMS)

def _sanitize_msg(msg: str | Any) -> str:
    if not isinstance(msg, str) or len(msg) > (_MAX_MSG_LEN * 4) or _is_sensitive_input(msg): 
        return ""
    clean = "".join(char for char in msg if char in _ALLOWED_CHARS)
    return clean.strip()[:_MAX_MSG_LEN]

def _safe_factory(factory: Callable[[SystemMetrics], str]) -> Callable[[SystemMetrics], str]:
    def wrapper(metrics: SystemMetrics) -> str:
        try:
            msg = factory(metrics)
            return _sanitize_msg(msg)
        except Exception:
            return ""
    return wrapper

# --- Protocolos y Estructuras de Datos ---

class Scorer(Protocol):
    def __call__(self, metrics: SystemMetrics) -> NormalizedRatio: ...

class Grade(Enum):
    A = "A"; B = "B"; C = "C"; D = "D"; F = "F"

    @classmethod
    def from_score(cls, score: float | int | None) -> str:
        if score is None or not _is_finite_numeric(score):
            return cls.F.value
        val = float(score)
        if val >= 90: return cls.A.value
        if val >= 80: return cls.B.value
        if val >= 65: return cls.C.value
        if val >= 50: return cls.D.value
        return cls.F.value

class RecommendationRule(NamedTuple):
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    area: MetricKey
    weight: int
    scorer: Callable[[SystemMetrics], NormalizedRatio]
    rules: Tuple[RecommendationRule, ...]
    formatted_label: str = ""

# --- Configuración del Pipeline ---

_LIMIT_JUNK_MB: Final[float] = 5000.0        
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0   
_LIMIT_STARTUP_COUNT: Final[int] = 20        
_LIMIT_RAM_PERCENT: Final[float] = 35.0      
_LIMIT_DISK_PERCENT: Final[float] = 25.0     

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    if not _is_finite_numeric(value): return min_val
    return max(min_val, min(max_val, float(value)))

def create_linear_scorer(limit: float, inverse: bool = True) -> Callable[[float], NormalizedRatio]:
    @lru_cache(maxsize=32)
    def scorer(val: float) -> NormalizedRatio:
        if limit <= 0.0 or not _is_finite_numeric(val): return 0.0
        try:
            ratio = val / limit
            return _clamp(1.0 - ratio if inverse else ratio)
        except ZeroDivisionError:
            return 0.0
    return scorer

WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30, "disco": 20, "memoria": 18, "basura": 14, "duplicados": 10, "arranque": 8,
}

_JUNK_SCORER: Final = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER: Final = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER: Final = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

_RULES: Final[Dict[str, Tuple[RecommendationRule, ...]]] = {
    "seguridad": (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, _safe_factory(lambda m: f"Riesgos de seguridad detectados: {m.safe_get('suspicious_count')}."), lambda m, r: r < WARN_THRESHOLD_HIGH),),
    "disco": (RecommendationRule("disco", WARN_THRESHOLD_LOW, _safe_factory(lambda m: f"Espacio libre en disco bajo: {m.safe_get('disk_free_percent'):.1f} porciento."), lambda m, r: r < WARN_THRESHOLD_LOW),),
    "memoria": (RecommendationRule("memoria", WARN_THRESHOLD_LOW, _safe_factory(lambda m: "Memoria disponible baja, optimice sus procesos."), lambda m, r: r < WARN_THRESHOLD_LOW),),
    "basura": (RecommendationRule("basura", WARN_THRESHOLD_MED, _safe_factory(lambda m: f"Acumulacion de archivos temporales: {m.safe_get('junk_mb'):.0f} MB."), lambda m, r: r < WARN_THRESHOLD_MED),),
    "duplicados": (RecommendationRule("duplicados", WARN_THRESHOLD_MED, _safe_factory(lambda m: f"Recuperacion posible: {m.safe_get('duplicate_mb'):.0f} MB en duplicados."), lambda m, r: r < WARN_THRESHOLD_MED),),
    "arranque": (RecommendationRule("arranque", WARN_THRESHOLD_LOW, _safe_factory(lambda m: f"Exceso de programas en arranque: {m.safe_get('startup_count')} entradas."), lambda m, r: r < WARN_THRESHOLD_LOW),),
}

PIPELINE_DEFINITIONS: Final[Tuple[PipelineEntry, ...]] = (
    PipelineEntry("seguridad", 30, lambda m: score_security(m.safe_get("suspicious_count"), m.safe_get("suspicious_warnings")), _RULES["seguridad"], "Seguridad  "),
    PipelineEntry("disco", 20, lambda m: score_disk(m.safe_get("disk_free_percent")), _RULES["disco"], "Disco      "),
    PipelineEntry("memoria", 18, lambda m: score_memory(m.safe_get("memory_available_percent")), _RULES["memoria"], "Memoria    "),
    PipelineEntry("basura", 14, lambda m: score_junk(m.safe_get("junk_mb")), _RULES["basura"], "Basura     "),
    PipelineEntry("duplicados", 10, lambda m: score_duplicates(m.safe_get("duplicate_mb")), _RULES["duplicados"], "Duplicados "),
    PipelineEntry("arranque", 8, lambda m: score_startup(m.safe_get("startup_count")), _RULES["arranque"], "Arranque   "),
)

# --- Funciones de Cálculo de Salud ---

@lru_cache(maxsize=16)
def score_junk(junk_mb: float | int) -> NormalizedRatio: return _JUNK_SCORER(float(junk_mb))

@lru_cache(maxsize=16)
def score_security(suspicious_count: int | float, warnings: int | float = 0) -> NormalizedRatio: 
    count = _validate_numeric(suspicious_count, 0.0, 0.0, 5000.0)
    warn = _validate_numeric(warnings, 0.0, 0.0, 5000.0)
    return _clamp(1.0 - ((count * 0.05) + (warn * 0.25)))

@lru_cache(maxsize=16)
def score_memory(available_percent: float | int) -> NormalizedRatio: 
    val = float(available_percent)
    return 0.0 if _LIMIT_RAM_PERCENT <= 0.0 else _clamp(val / _LIMIT_RAM_PERCENT)

@lru_cache(maxsize=16)
def score_disk(free_percent: float | int) -> NormalizedRatio: 
    val = float(free_percent)
    return 0.0 if _LIMIT_DISK_PERCENT <= 0.0 else _clamp(val / _LIMIT_DISK_PERCENT)

@lru_cache(maxsize=16)
def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: return _DUP_SCORER(float(duplicate_mb))

@lru_cache(maxsize=16)
def score_startup(startup_count: int | float) -> NormalizedRatio: return _STARTUP_SCORER(float(startup_count))

def _validate_numeric(value: Any, default: float, min_val: float, max_val: float) -> float:
    if not _is_finite_numeric(value): return default
    return float(max(min_val, min(max_val, float(value))))

# --- Modelos de Datos ---

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

    def __post_init__(self) -> None: self.validate()

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _ALL_METRICS:
            v = _validate_numeric(value, 0.0, *_METRIC_LIMITS.get(name, (0.0, 1e9)))
            super().__setattr__(name, v)
        else: super().__setattr__(name, value)

    @_guard_metric_access
    def safe_get(self, field_name: str, default: Any = 0.0) -> Any: return getattr(self, field_name, default)

    def get_clamped_metric(self, field_name: str) -> float:
        min_v, max_v = _METRIC_LIMITS.get(field_name, (0.0, 1e9))
        return _validate_numeric(self.safe_get(field_name, 0.0), 0.0, min_v, max_v)

    def validate(self) -> None:
        for f in _ALL_METRICS: super().__setattr__(f, self.get_clamped_metric(f))

    @property
    def is_finite(self) -> bool: return all(_is_finite_numeric(self.safe_get(f, 0.0)) for f in _ALL_METRICS)

@dataclass
class HealthResult:
    score: int = 0
    grade: str = "F"
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=lambda: ["Error: Datos no inicializados."])

    @property
    def is_healthy(self) -> bool: return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: return Grade.from_score(score)

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], normalized_ratio: NormalizedRatio) -> List[str]:
    if not isinstance(rules, tuple) or not _is_finite_numeric(normalized_ratio): return []
    results = []
    for rule in rules:
        if rule.check(metrics, normalized_ratio):
            msg = rule.message_factory(metrics)
            if msg: results.append(msg)
    return results

def _process_pipeline_entry(metrics: SystemMetrics, entry: PipelineEntry) -> Tuple[int, List[str]]:
    try:
        ratio = _clamp(entry.scorer(metrics))
        points = int(ratio * float(entry.weight))
        return points, _evaluate_rules(metrics, entry.rules, ratio)
    except Exception: return 0, []

# --- Orquestación Principal ---

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    if not isinstance(metrics, SystemMetrics) or not metrics.is_finite:
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Datos de entrada inválidos."])
    
    metric_breakdown, total, recs = {}, 0.0, []
    for entry in PIPELINE_DEFINITIONS:
        p, r = _process_pipeline_entry(metrics, entry)
        metric_breakdown[entry.area], total = p, total + p
        recs.extend(r)
    
    if metrics.safe_get("quarantined_count") > 0:
        msg = _sanitize_msg("Existen elementos en cuarentena.")
        if msg: recs.append(msg)
            
    final_score = int(_clamp(total, 0.0, 100.0))
    return HealthResult(final_score, grade_for_score(final_score), metric_breakdown, recs or ["El sistema está en buen estado."])

def _render_bar(points: int, max_val: int) -> str:
    if max_val <= 0: return ""
    p = points if 0 <= points <= max_val else max(0, min(points, max_val))
    return "#" * p + "." * (max_val - p)

def summarize(result: HealthResult | None) -> List[str]:
    if not isinstance(result, HealthResult): return ["Error: Informe no disponible."]
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for e in PIPELINE_DEFINITIONS:
        val = result.breakdown.get(e.area, 0)
        lines.append(f"  {e.formatted_label} {val:>2}/{e.weight:<2} [{_render_bar(val, e.weight)}]")
    lines.extend(("", "Recomendaciones:", *[f"  - {s}" for s in (result.recommendations or [])]))
    return lines
