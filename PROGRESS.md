# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **233** (46.2% de aceptación)
- Rechazadas por tests: 11
- Rechazadas por guardia de seguridad: 32
- Sin cambios (nada sustancial que mejorar): 19
- Sin respuesta de la IA (error o límite): 209

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-08-28 | 53 | 2 | 7 | 1 | 51 |
| 2026-08-29 | 162 | 9 | 22 | 18 | 139 |
| 2026-08-30 | 18 | 0 | 3 | 0 | 19 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **54**
- seguridad defensiva: **52**
- manejo de errores y validación de entradas: **46**
- rendimiento: **42**
- robustez ante casos límite: **39**

## Mejoras aceptadas por archivo

- `settings.py`: **24**
- `memory.py`: **21**
- `scanner.py`: **21**
- `quarantine.py`: **20**
- `assistant.py`: **18**
- `browser.py`: **18**
- `diskreport.py`: **18**
- `duplicates.py`: **16**
- `branding.py`: **16**
- `healthscore.py`: **15**
- `main.py`: **14**
- `organizer.py`: **11**
- `startup.py`: **11**
- `safety.py`: **10**

## Últimas 15 mejoras aceptadas

- `2026-08-30T01:07:24` **startup.py** (seguridad defensiva): Se reforzó la seguridad de `entries_from_registry` validando que la salida de PowerShell no contenga caracteres de control peligrosos antes de procesar el CSV, asegurando que el motor de parseo no sea inyectado mediante una salida de consola malintencionada.
- `2026-08-30T01:06:49` **settings.py** (seguridad defensiva): Mejoré la seguridad defensiva en `save()` añadiendo una verificación explícita mediante `is_safe_to_modify` antes de proceder con cualquier operación de escritura, asegurando que la ruta destino no sea un punto de reparse o junction, complementando así la validación del directorio padre y fortaleciendo la integridad del proceso de persistencia.
- `2026-08-30T00:58:50` **scanner.py** (seguridad defensiva): Se ha mejorado la robustez defensiva de `scanner.py` implementando una validación explícita para evitar que `_is_safe_entry` evalúe rutas que contienen caracteres de control de ofuscación (RTL), reduciendo el riesgo de confusión de rutas antes de cualquier operación.
- `2026-08-30T00:58:41` **safety.py** (seguridad defensiva): Se ha mejorado la robustez de `is_within_directory` para prevenir escapes de ruta mediante el uso de `resolve()` (que expande cualquier link simbólico o punto de reparse antes de comparar) y se ha añadido una validación adicional para asegurar que la ruta normalizada no pertenezca a la raíz del sistema, mitigando riesgos de seguridad en entornos con permisos elevados.
- `2026-08-30T00:56:35` **quarantine.py** (seguridad defensiva): Se ha mejorado la seguridad del módulo `quarantine.py` reforzando la validación en `_validate_isolation_request` para impedir explícitamente el aislamiento de archivos que contengan puntos de reparse o enlaces simbólicos (junctions/symlinks), previniendo así posibles ataques de "link following" o recursiones inesperadas fuera del sandbox.
- `2026-08-30T00:48:11` **organizer.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_is_safe_for_disk_op` añadiendo una validación explícita de `is_protected_path` sobre la ruta resuelta (`resolve()`) antes de cualquier operación, garantizando que ni siquiera mediante enlaces simbólicos o redirecciones maliciosas se pueda operar fuera del alcance permitido.
- `2026-08-30T00:48:00` **memory.py** (seguridad defensiva): Mejoré la seguridad defensiva al limitar las máscaras de acceso en `OpenProcess` al mínimo necesario y agregué una validación de existencia de archivo mediante `os.path.exists` antes de la resolución de `Path.resolve`, mitigando riesgos de manipulación de punteros durante la inspección de procesos.
- `2026-08-30T00:47:31` **main.py** (seguridad defensiva): Se introdujo una comprobación explícita de seguridad al importar módulos de configuración y escaneo (`settings.py` y `scanner.py`) dentro de la app para asegurar que no se inicialicen sobre rutas de sistema protegidas, alineándose con el enfoque de seguridad defensiva.
- `2026-08-30T00:37:14` **duplicates.py** (seguridad defensiva): Se ha mejorado la robustez defensiva de la recolección de candidatos en `_collect_candidates` añadiendo una validación explícita mediante `is_protected_path` en cada entrada del sistema de archivos antes de cualquier operación de stat o recursión, evitando así posibles accesos a rutas protegidas incluso si el sistema operativo no reporta atributos especiales.
- `2026-08-30T00:37:05` **diskreport.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `walk_files` y `largest_folders` validando explícitamente que cualquier ruta derivada (`entry.path` o `top_folder`) sea tratada como un objeto `Path` antes de ser validada por `is_protected_path`, evitando inconsistencias de tipo y asegurando que ninguna ruta pase por alto los filtros de seguridad al recorrer el árbol.
- `2026-08-30T00:36:39` **browser.py** (seguridad defensiva): Se ha mejorado la defensa contra el acceso a rutas protegidas mediante la implementación de `_is_canonical_path` en `_sum_directory_recursive`, asegurando que cada subdirectorio escaneado sea validado mediante `is_protected_path` y `is_safe_to_modify` antes de proceder, previniendo así posibles escapes de contexto durante la recursión.
- `2026-08-30T00:36:13` **branding.py** (seguridad defensiva): Se reforzó la seguridad en `save_logo_svg` reemplazando la instanciación directa de `Path` por una validación previa de la ruta de destino, garantizando que ninguna escritura ocurra si la ruta es inválida, protegida o un punto de reparse, alineándose con las reglas de seguridad defensiva.
- `2026-08-30T00:27:11` **assistant.py** (seguridad defensiva): Mejoré la seguridad defensiva en `_call_gemini` al validar la integridad estructural de la respuesta JSON antes de procesarla, asegurando que cualquier desviación en el esquema esperado resulte en un fallo seguro en lugar de una excepción no controlada.
- `2026-08-30T00:26:50` **startup.py** (robustez ante casos límite): Mejoré la robustez de `parse_registry_csv` añadiendo una validación explícita para asegurar que cada fila procesada posea el número mínimo de campos requeridos antes de intentar acceder a sus índices, evitando errores de `IndexError` o `KeyError` ante datos corruptos o mal formateados del registro.
- `2026-08-30T00:26:24` **settings.py** (robustez ante casos límite): Mejoré la robustez de `settings.py` ante errores de entrada en el diccionario `DEFAULTS` y durante la validación al detectar una inconsistencia en la clave `asistente_enviar_METRICAS` (case-sensitive) que causaba que el valor real se perdiera o reiniciara, unificando además la estructura de validación para evitar errores silenciosos en tiempo de ejecución.
