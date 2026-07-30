"""
healthscore.py — el panel que combina todos los módulos en un solo número.

Toma las mediciones de limpieza, seguridad, memoria, disco, duplicados y
arranque, y las convierte en un puntaje de 0 a 100 con una nota de A a F y
recomendaciones concretas.

DECISIÓN DE DISEÑO: `compute_score` es una función pura — recibe un objeto
con las métricas y no toca el disco ni el sistema. Eso permite testear
todos los casos límite (sistema impecable, sistema desastroso, datos
faltantes) sin necesitar una PC sucia de verdad. La recolección de datos
vive en los otros módulos; acá solo se puntúa.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, Tuple
import math

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

# Umbrales críticos para la normalización: definen el punto de saturación (ratio 0.0).
# Superar estos valores implica un sistema degradado en esa categoría específica.
JUNK_LIMIT_MB: float = 5000.0          
DUPLICATE_LIMIT_MB: float = 2000.0     
STARTUP_LIMIT_COUNT: int = 20          
RAM_IDEAL_PERCENT: float = 35.0        # Basado en el estándar de capacidad disponible.
DISK_IDEAL_PERCENT: float = 25.0       # El 25% libre es el límite inferior recomendado antes de alertar.

# Peso relativo de cada área en el puntaje total (sumatoria debe ser 100).
WEIGHTS: Final[Dict[str, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}


@dataclass
class SystemMetrics:
    """Contenedor de datos crudos (métricas) provenientes de los diversos módulos."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def validate(self) -> None:
        """Asegura que los valores numéricos estén dentro de rangos lógicos y tipos válidos."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        """Verifica que todas las métricas críticas sean números finitos."""
        return all(math.isfinite(v) for v in (
            self.junk_mb, self.memory_available_percent, self.disk_free_percent, self.duplicate_mb
        ))


@dataclass
class HealthResult:
    """Resultado final: puntaje (0-100), nota cualitativa y recomendaciones."""
    score: int
    grade: str
    breakdown: Dict[str, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Determina si el estado general es aceptable (>= 80/100)."""
        return self.score >= 80


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Acota un valor numérico al rango [low, high]."""
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return low
    return max(low, min(high, float(value)))


def _to_float(value: Any, default: float = 0.0) -> float:
    """Convierte un valor a float de manera segura."""
    try:
        val = float(value) if value is not None else default
        return val if math.isfinite(val) else default
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """Convierte un valor a int de manera segura."""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def score_junk(junk_mb: float) -> float:
    """Calcula el ratio de limpieza de basura (0.0 a 1.0)."""
    if JUNK_LIMIT_MB <= 0: return 0.0
    return _clamp(1.0 - (junk_mb / JUNK_LIMIT_MB))


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """Calcula el ratio de seguridad (0.0 a 1.0) penalizando hallazgos."""
    penalty: float = float(suspicious_count) * 0.05 + float(warnings) * 0.25
    return _clamp(1.0 - penalty, 0.0, 1.0)


def score_memory(available_percent: float) -> float:
    """Calcula el ratio de disponibilidad de RAM (0.0 a 1.0)."""
    if RAM_IDEAL_PERCENT <= 0: return 0.0
    return _clamp(available_percent / RAM_IDEAL_PERCENT)


def score_disk(free_percent: float) -> float:
    """Calcula el ratio de espacio libre en disco (0.0 a 1.0)"""
    if DISK_IDEAL_PERCENT <= 0: return 0.0
    return _clamp(free_percent / DISK_IDEAL_PERCENT)


def score_duplicates(duplicate_mb: float) -> float:
    """Calcula el ratio de archivos duplicados (0.0 a 1.0)."""
    if DUPLICATE_LIMIT_MB <= 0: return 0.0
    return _clamp(1.0 - (duplicate_mb / DUPLICATE_LIMIT_MB))


def score_startup(startup_count: int) -> float:
    """Calcula el ratio de programas en inicio (0.0 a 1.0)."""
    if STARTUP_LIMIT_COUNT <= 0: return 0.0
    return _clamp(1.0 - (startup_count / STARTUP_LIMIT_COUNT))


def grade_for_score(score: int) -> str:
    """Mapea un puntaje entero [0-100] a una nota (A-F)."""
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 65: return "C"
    if score >= 50: return "D"
    return "F"


def _generate_recommendations(m: SystemMetrics, ratios: Dict[str, float]) -> List[str]:
    """Genera sugerencias accionables basadas en los ratios obtenidos."""
    recs: List[str] = []
    
    if ratios["seguridad"] < 0.9:
        recs.append(f"Revisá los {max(0, int(m.suspicious_count))} hallazgo(s) de seguridad; podés aislarlos en cuarentena sin borrarlos.")
    if ratios["disco"] < 0.6:
        recs.append(f"Queda {round(_clamp(float(m.disk_free_percent), 0.0, 100.0), 1)}% de disco libre. Mirá el análisis de disco para ver qué ocupa más.")
    if ratios["memoria"] < 0.6:
        recs.append("Memoria disponible baja: cerrá programas que no uses. Ojo, 'liberar RAM' no sirve, cerrar procesos sí.")
    if ratios["basura"] < 0.8:
        recs.append(f"Hay unos {int(max(0.0, float(m.junk_mb)))} MB de archivos temporales para revisar.")
    if ratios["duplicados"] < 0.8:
        recs.append(f"Podrías recuperar ~{int(max(0.0, float(m.duplicate_mb)))} MB eliminando copias duplicadas.")
    if ratios["arranque"] < 0.6:
        recs.append(f"{max(0, int(m.startup_count))} programas arrancan con Windows; desactivá los que no necesites desde el Administrador de tareas.")
    
    if m.quarantined_count > 0:
        recs.append(f"Tenés {max(0, int(m.quarantined_count))} archivo(s) en cuarentena esperando tu decisión.")
    
    if not recs:
        recs.append("No hay nada urgente para hacer. El sistema está en buen estado.")
    return recs


def compute_score(metrics: SystemMetrics) -> HealthResult:
    """Calcula el puntaje global de salud del sistema."""
    if not isinstance(metrics, SystemMetrics) or sum(WEIGHTS.values()) != 100:
        return HealthResult(0, "F", {}, ["Error: Datos de entrada faltantes, inválidos o configuración desbalanceada."])

    try:
        metrics.validate()
        if not metrics.is_finite():
            return HealthResult(0, "F", {}, ["Error: Las métricas contienen datos numéricos no procesables."])
        
        ratios = {
            "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
            "disco": score_disk(metrics.disk_free_percent),
            "memoria": score_memory(metrics.memory_available_percent),
            "basura": score_junk(metrics.junk_mb),
            "duplicados": score_duplicates(metrics.duplicate_mb),
            "arranque": score_startup(metrics.startup_count),
        }

        breakdown = {k: int(round(ratios[k] * w)) for k, w in WEIGHTS.items()}
        total_score = max(0, min(100, sum(breakdown.values())))

        return HealthResult(
            score=total_score,
            grade=grade_for_score(total_score),
            breakdown=breakdown,
            recommendations=_generate_recommendations(metrics, ratios),
        )

    except Exception:
        return HealthResult(0, "F", {}, ["Error crítico inesperado al procesar métricas."])


def summarize(result: HealthResult) -> List[str]:
    """Genera una representación visual y textual del resultado de salud."""
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    
    orden = sorted(result.breakdown.items(), key=lambda item: item[1] - WEIGHTS[item[0]])
    
    for area, puntos in orden:
        maximo = WEIGHTS[area]
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (maximo - puntos)}]")
    
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {rec}" for rec in result.recommendations])
    return lines
