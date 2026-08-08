"""
healthscore.py — el panel que combina todos los módulos en un solo número.

Toma las mediciones de limpieza, seguridad, memoria, disco, duplicados y
arranque, y las convierte en un puntaje de 0 a 100 con una nota de A a F y
recomendaciones concretas.

DECISIÓN DE DISEÑO: `compute_score` es una función pura — recibe un objeto
con las métricas y no toca el disco ni el sistema. Eso permite testear
todos los casos límite (sistema impecable, sistema desastre, datos
faltantes) sin necesitar una PC sucia de verdad. La recolección de datos
vive en los otros módulos; acá solo se puntúa.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, Tuple, TypeAlias
import math

# Tipos para mejorar la claridad en el flujo de datos
ScoreMap: TypeAlias = Dict[str, float]

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

# --- UMBRALES DE NORMALIZACIÓN ---
# Definen el punto de saturación donde un aspecto alcanza su peor nota (0.0).
# Todo valor superior a estos límites (o igual a cero para porcentajes) 
# resulta en la penalización máxima.

JUNK_LIMIT_MB: Final[float] = 5000.0          
DUPLICATE_LIMIT_MB: Final[float] = 2000.0     
STARTUP_LIMIT_COUNT: Final[int] = 20          
RAM_IDEAL_PERCENT: Final[float] = 35.0        
DISK_IDEAL_PERCENT: Final[float] = 25.0       

# --- UMBRALES DE ADVERTENCIA (ratios de 0.0 a 1.0) ---
# Determinan a partir de qué nivel de salud relativa (normalizada) se 
# dispara una recomendación específica para el usuario.

WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# --- PESOS DE CALIFICACIÓN ---
# Peso relativo de cada área en el puntaje total (sumatoria debe ser 100).
WEIGHTS: Final[Dict[str, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

# Precalculos para optimizar el hot-path de compute_score.
_TOTAL_WEIGHTS: Final[int] = sum(WEIGHTS.values())
_NORM_FACTOR: Final[float] = 100.0 / _TOTAL_WEIGHTS if _TOTAL_WEIGHTS > 0 else 0.0
_WEIGHT_ITEMS: Final[List[Tuple[str, int]]] = list(WEIGHTS.items())
_SCORE_CACHE: Dict[tuple, float] = {}


def _validate_weights() -> bool:
    """Valida que los pesos definidos en WEIGHTS sean enteros positivos y sumen valores coherentes."""
    return _TOTAL_WEIGHTS > 0 and all(isinstance(w, int) and w >= 0 for w in WEIGHTS.values())


@dataclass
class SystemMetrics:
    """
    Contenedor de datos crudos (métricas) provenientes de los diversos módulos.
    Los campos numéricos son normalizados durante el proceso de cálculo en
    `compute_score` mediante la validación previa.
    """
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def validate(self) -> None:
        """
        Normaliza internamente los valores para asegurar que las operaciones 
        aritméticas posteriores no reciban tipos incorrectos o fuera de rango.
        """
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        """Verifica la integridad numérica de todos los campos mediante comprobación de finitud IEEE 754."""
        for field_name in self.__dataclass_fields__:
            val = getattr(self, field_name)
            if isinstance(val, (int, float)) and not math.isfinite(val):
                return False
        return True


@dataclass
class HealthResult:
    """Estructura de datos inmutable que encapsula el resultado del análisis."""
    score: int
    grade: str
    breakdown: Dict[str, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Retorna True si el puntaje global alcanza el nivel de salud satisfactorio (>= 80)."""
        return math.isfinite(self.score) and self.score >= 80


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Restringe un número al intervalo [low, high]. Retorna 'low' ante NaN/Inf."""
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return low
    return max(low, min(high, float(value)))


def _to_float(value: Any, default: float = 0.0) -> float:
    """Convierte un objeto genérico a float con manejo de excepciones y validación de finitud."""
    try:
        val = float(value) if value is not None else default
        return val if math.isfinite(val) else default
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """Convierte un objeto genérico a int mediante una conversión intermedia a float."""
    try:
        val = int(float(value)) if value is not None else default
        return val if math.isfinite(float(val)) else default
    except (TypeError, ValueError):
        return default


def score_junk(junk_mb: float) -> float:
    """Normaliza volumen de basura: 0 MB = 1.0 (óptimo), > JUNK_LIMIT_MB = 0.0."""
    key = ("junk", junk_mb)
    if key in _SCORE_CACHE: return _SCORE_CACHE[key]
    val = _to_float(junk_mb)
    res = 1.0 if JUNK_LIMIT_MB <= 0 else _clamp(1.0 - (val / JUNK_LIMIT_MB))
    _SCORE_CACHE[key] = res
    return res


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """Normaliza seguridad: penaliza hallazgos (-0.05 c/u) y advertencias críticas (-0.25 c/u)."""
    key = ("security", suspicious_count, warnings)
    if key in _SCORE_CACHE: return _SCORE_CACHE[key]
    s_count = max(0, _to_int(suspicious_count))
    w_count = max(0, _to_int(warnings))
    res = _clamp(1.0 - ((float(s_count) * 0.05) + (float(w_count) * 0.25)), 0.0, 1.0)
    _SCORE_CACHE[key] = res
    return res


def score_memory(available_percent: float) -> float:
    """Normaliza uso de RAM: puntaje basado en ratio de disponibilidad frente al objetivo ideal."""
    key = ("mem", available_percent)
    if key in _SCORE_CACHE: return _SCORE_CACHE[key]
    val = _to_float(available_percent)
    res = 0.0 if RAM_IDEAL_PERCENT <= 0 else _clamp(val / RAM_IDEAL_PERCENT)
    _SCORE_CACHE[key] = res
    return res


def score_disk(free_percent: float) -> float:
    """Normaliza espacio en disco: puntaje basado en ratio de espacio libre frente al objetivo ideal."""
    key = ("disk", free_percent)
    if key in _SCORE_CACHE: return _SCORE_CACHE[key]
    val = _to_float(free_percent)
    res = 0.0 if DISK_IDEAL_PERCENT <= 0 else _clamp(val / DISK_IDEAL_PERCENT)
    _SCORE_CACHE[key] = res
    return res


def score_duplicates(duplicate_mb: float) -> float:
    """Normaliza espacio ocupado por duplicados: 0 MB = 1.0, > DUPLICATE_LIMIT_MB = 0.0."""
    key = ("dup", duplicate_mb)
    if key in _SCORE_CACHE: return _SCORE_CACHE[key]
    val = _to_float(duplicate_mb)
    res = 1.0 if DUPLICATE_LIMIT_MB <= 0 else _clamp(1.0 - (val / DUPLICATE_LIMIT_MB))
    _SCORE_CACHE[key] = res
    return res


def score_startup(startup_count: int) -> float:
    """Normaliza carga de inicio: penaliza linealmente por cada programa extra hasta el límite permitido."""
    key = ("start", startup_count)
    if key in _SCORE_CACHE: return _SCORE_CACHE[key]
    val = max(0, _to_int(startup_count))
    res = 1.0 if STARTUP_LIMIT_COUNT <= 0 else _clamp(1.0 - (float(val) / STARTUP_LIMIT_COUNT))
    _SCORE_CACHE[key] = res
    return res


def grade_for_score(score: int) -> str:
    """Mapea puntaje numérico [0, 100] a nivel de calificación cualitativa."""
    score_int = int(_clamp(float(score), 0.0, 100.0))
    if score_int >= 90: return "A"
    if score_int >= 80: return "B"
    if score_int >= 65: return "C"
    if score_int >= 50: return "D"
    return "F"


def _generate_recommendations(m: SystemMetrics, ratios: ScoreMap) -> List[str]:
    """Genera textos explicativos según el estado de salud de cada métrica normalizada."""
    if not isinstance(m, SystemMetrics) or not isinstance(ratios, dict):
        return ["Error: Datos de entrada inválidos para recomendaciones."]
    
    recs: List[str] = []
    
    if ratios.get("seguridad", 1.0) < WARN_THRESHOLD_HIGH:
        recs.append(f"Revisá los {max(0, m.suspicious_count)} hallazgo(s) de seguridad; podés aislarlos en cuarentena sin borrarlos.")
    if ratios.get("disco", 1.0) < WARN_THRESHOLD_LOW:
        recs.append(f"Queda {m.disk_free_percent:.1f}% de disco libre. Mirá el análisis de disco para ver qué ocupa más.")
    if ratios.get("memoria", 1.0) < WARN_THRESHOLD_LOW:
        recs.append("Memoria disponible baja: cerrá programas que no uses. Ojo, 'liberar RAM' no sirve, cerrar procesos sí.")
    if ratios.get("basura", 1.0) < WARN_THRESHOLD_MED:
        recs.append(f"Hay unos {int(max(0.0, m.junk_mb))} MB de archivos temporales para revisar.")
    if ratios.get("duplicados", 1.0) < WARN_THRESHOLD_MED:
        recs.append(f"Podrías recuperar ~{int(max(0.0, m.duplicate_mb))} MB eliminando copias duplicadas.")
    if ratios.get("arranque", 1.0) < WARN_THRESHOLD_LOW:
        recs.append(f"{max(0, m.startup_count)} programas arrancan con Windows; desactivá los que no necesites desde el Administrador de tareas.")
    
    if m.quarantined_count > 0:
        recs.append(f"Tenés {max(0, m.quarantined_count)} archivo(s) en cuarentena esperando tu decisión.")
    
    return recs if recs else ["No hay nada urgente para hacer. El sistema está en buen estado."]


def compute_score(metrics: SystemMetrics) -> HealthResult:
    """
    Función principal: toma métricas crudas, calcula el puntaje ponderado 
    y genera un objeto HealthResult inmutable.
    """
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Instancia de métricas no válida."])
    
    metrics.validate()
    if not metrics.is_finite() or not _validate_weights():
        return HealthResult(0, "F", {}, ["Error: Datos de entrada o configuración no procesables."])

    scores: ScoreMap = {
        "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
        "disco": score_disk(metrics.disk_free_percent),
        "memoria": score_memory(metrics.memory_available_percent),
        "basura": score_junk(metrics.junk_mb),
        "duplicados": score_duplicates(metrics.duplicate_mb),
        "arranque": score_startup(metrics.startup_count)
    }

    breakdown: Dict[str, int] = {}
    total_weighted_score: float = 0.0
    
    for area, weight in _WEIGHT_ITEMS:
        score_val: float = scores.get(area, 0.0) * float(weight) * _NORM_FACTOR
        breakdown[area] = int(score_val + 0.5)
        total_weighted_score += score_val

    final_score: int = int(_clamp(total_weighted_score, 0.0, 100.0))
    if not math.isclose(sum(breakdown.values()), final_score, abs_tol=1):
        final_score = sum(breakdown.values())

    return HealthResult(
        score=final_score,
        grade=grade_for_score(final_score),
        breakdown=breakdown,
        recommendations=_generate_recommendations(metrics, scores),
    )


def summarize(result: HealthResult) -> List[str]:
    """Crea una representación textual legible de los resultados para la UI."""
    if not isinstance(result, HealthResult) or not isinstance(getattr(result, 'breakdown', None), dict):
        return ["Error: Resultado de salud no válido."]

    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    
    for area, maximo in _WEIGHT_ITEMS:
        puntos_val: int = result.breakdown.get(area, 0)
        visual: str = f"[{'#' * puntos_val}{'.' * (max(0, maximo - puntos_val))}]"
        lines.append(f"  {area.capitalize():<12} {puntos_val:>2}/{maximo:<2} {visual}")
    
    lines.extend(["", "Recomendaciones:"])
    recs: List[str] = result.recommendations if isinstance(result.recommendations, list) else ["No hay recomendaciones disponibles."]
    for rec in recs:
        lines.append(f"  - {str(rec)}")
    return lines
