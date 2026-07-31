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
from typing import Dict, List, Any, Final
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
# Un ratio de 1.0 significa desempeño ideal, mientras que 0.0 indica que se ha alcanzado
# el límite crítico de degradación para esa métrica específica.
JUNK_LIMIT_MB: Final[float] = 5000.0          
DUPLICATE_LIMIT_MB: Final[float] = 2000.0     
STARTUP_LIMIT_COUNT: Final[int] = 20          
RAM_IDEAL_PERCENT: Final[float] = 35.0        
DISK_IDEAL_PERCENT: Final[float] = 25.0       

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
        return (math.isfinite(self.junk_mb) and 
                math.isfinite(self.memory_available_percent) and 
                math.isfinite(self.disk_free_percent) and 
                math.isfinite(self.duplicate_mb) and
                math.isfinite(self.suspicious_count) and
                math.isfinite(self.startup_count))


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
    """Acota un valor numérico al rango [low, high] asegurando que sea un float finito."""
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return low
    return max(low, min(high, float(value)))


def _to_float(value: Any, default: float = 0.0) -> float:
    """Intenta convertir un valor arbitrario a float; retorna el default en caso de fallo."""
    try:
        val = float(value) if value is not None else default
        return val if math.isfinite(val) else default
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """Intenta convertir un valor arbitrario a int; valida finitud antes de convertir."""
    try:
        val = int(float(value)) if value is not None else default
        return val if math.isfinite(float(val)) else default
    except (TypeError, ValueError):
        return default


def score_junk(junk_mb: float) -> float:
    """Normaliza MB de basura a un ratio [0.0, 1.0] basado en JUNK_LIMIT_MB."""
    if JUNK_LIMIT_MB <= 0 or not math.isfinite(junk_mb): return 0.0
    return _clamp(1.0 - (junk_mb / JUNK_LIMIT_MB))


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """Calcula ratio [0.0, 1.0] penalizando hallazgos de seguridad y advertencias."""
    if not math.isfinite(suspicious_count) or not math.isfinite(warnings): return 0.0
    penalty: float = float(suspicious_count) * 0.05 + float(warnings) * 0.25
    return _clamp(1.0 - penalty, 0.0, 1.0)


def score_memory(available_percent: float) -> float:
    """Normaliza el porcentaje de RAM disponible a un ratio [0.0, 1.0] contra RAM_IDEAL_PERCENT."""
    if RAM_IDEAL_PERCENT <= 0 or not math.isfinite(available_percent): return 0.0
    return _clamp(available_percent / RAM_IDEAL_PERCENT)


def score_disk(free_percent: float) -> float:
    """Normaliza el porcentaje de espacio libre a un ratio [0.0, 1.0] contra DISK_IDEAL_PERCENT."""
    if DISK_IDEAL_PERCENT <= 0 or not math.isfinite(free_percent): return 0.0
    return _clamp(free_percent / DISK_IDEAL_PERCENT)


def score_duplicates(duplicate_mb: float) -> float:
    """Normaliza MB de duplicados a un ratio [0.0, 1.0] basado en DUPLICATE_LIMIT_MB."""
    if DUPLICATE_LIMIT_MB <= 0 or not math.isfinite(duplicate_mb): return 0.0
    return _clamp(1.0 - (duplicate_mb / DUPLICATE_LIMIT_MB))


def score_startup(startup_count: int) -> float:
    """Normaliza el conteo de programas de inicio a un ratio [0.0, 1.0] contra STARTUP_LIMIT_COUNT."""
    if STARTUP_LIMIT_COUNT <= 0 or not math.isfinite(startup_count): return 0.0
    return _clamp(1.0 - (float(startup_count) / STARTUP_LIMIT_COUNT))


def grade_for_score(score: int) -> str:
    """Mapea un puntaje entero [0-100] a una categoría cualitativa (A-F)."""
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 65: return "C"
    if score >= 50: return "D"
    return "F"


def _generate_recommendations(m: SystemMetrics, ratios: Dict[str, float]) -> List[str]:
    """Genera una lista de acciones correctivas basadas en ratios individuales por área."""
    recs: List[str] = []
    
    if ratios.get("seguridad", 1.0) < 0.9:
        recs.append(f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad; podés aislarlos en cuarentena sin borrarlos.")
    if ratios.get("disco", 1.0) < 0.6:
        recs.append(f"Queda {m.disk_free_percent:.1f}% de disco libre. Mirá el análisis de disco para ver qué ocupa más.")
    if ratios.get("memoria", 1.0) < 0.6:
        recs.append("Memoria disponible baja: cerrá programas que no uses. Ojo, 'liberar RAM' no sirve, cerrar procesos sí.")
    if ratios.get("basura", 1.0) < 0.8:
        recs.append(f"Hay unos {int(m.junk_mb)} MB de archivos temporales para revisar.")
    if ratios.get("duplicados", 1.0) < 0.8:
        recs.append(f"Podrías recuperar ~{int(m.duplicate_mb)} MB eliminando copias duplicadas.")
    if ratios.get("arranque", 1.0) < 0.6:
        recs.append(f"{m.startup_count} programas arrancan con Windows; desactivá los que no necesites desde el Administrador de tareas.")
    
    if m.quarantined_count > 0:
        recs.append(f"Tenés {m.quarantined_count} archivo(s) en cuarentena esperando tu decisión.")
    
    if not recs:
        recs.append("No hay nada urgente para hacer. El sistema está en buen estado.")
    return recs


def compute_score(metrics: SystemMetrics) -> HealthResult:
    """Calcula el puntaje global de salud del sistema normalizando áreas según sus pesos."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Instancia de métricas no válida."])
    
    if sum(WEIGHTS.values()) != 100:
        return HealthResult(0, "F", {}, ["Error: Configuración de pesos desbalanceada."])

    try:
        metrics.validate()
        if not metrics.is_finite():
            return HealthResult(0, "F", {}, ["Error: Métricas contienen datos no procesables."])
        
        ratios: Dict[str, float] = {
            "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
            "disco": score_disk(metrics.disk_free_percent),
            "memoria": score_memory(metrics.memory_available_percent),
            "basura": score_junk(metrics.junk_mb),
            "duplicados": score_duplicates(metrics.duplicate_mb),
            "arranque": score_startup(metrics.startup_count),
        }

        total_score: float = 0.0
        breakdown: Dict[str, int] = {}
        for area, weight in WEIGHTS.items():
            score_part = int(ratios.get(area, 0.0) * weight + 0.5)
            breakdown[area] = score_part
            total_score += score_part

        final_score = int(total_score)
        return HealthResult(
            score=final_score,
            grade=grade_for_score(final_score),
            breakdown=breakdown,
            recommendations=_generate_recommendations(metrics, ratios),
        )

    except Exception:
        return HealthResult(0, "F", {}, ["Error crítico inesperado al procesar métricas."])


def _sort_by_performance_delta(item: tuple[str, int]) -> int:
    """Calcula la desviación respecto al peso ideal para ordenar áreas prioritarias."""
    area, puntos = item
    return puntos - WEIGHTS.get(area, 0)


def summarize(result: HealthResult) -> List[str]:
    """Genera una representación visual y textual detallada del resultado de salud."""
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    
    orden = sorted(result.breakdown.items(), key=_sort_by_performance_delta)
    
    for area, puntos in orden:
        maximo = WEIGHTS.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (maximo - puntos)}]")
    
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {rec}" for rec in result.recommendations])
    return lines
