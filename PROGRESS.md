# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **202** (40.1% de aceptación)
- Rechazadas por tests: 26
- Rechazadas por guardia de seguridad: 43
- Sin cambios (nada sustancial que mejorar): 9
- Sin respuesta de la IA (error o límite): 224

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-10-06 | 31 | 6 | 9 | 0 | 40 |
| 2026-10-07 | 139 | 17 | 28 | 8 | 158 |
| 2026-10-08 | 32 | 3 | 6 | 1 | 26 |

## Mejoras aceptadas por enfoque

- manejo de errores y validación de entradas: **46**
- legibilidad y documentación: **42**
- rendimiento: **42**
- seguridad defensiva: **36**
- robustez ante casos límite: **36**

## Mejoras aceptadas por archivo

- `assistant.py`: **21**
- `browser.py`: **21**
- `quarantine.py`: **20**
- `diskreport.py`: **19**
- `healthscore.py`: **19**
- `memory.py`: **18**
- `safety.py`: **16**
- `settings.py`: **14**
- `branding.py`: **13**
- `organizer.py`: **12**
- `scanner.py`: **11**
- `duplicates.py`: **10**
- `main.py`: **8**

## Últimas 15 mejoras aceptadas

- `2026-10-08T02:52:19` **diskreport.py** (robustez ante casos límite): Se ha mejorado la robustez de `_safe_stat` y `_is_excluded_path` para manejar situaciones donde el sistema de archivos devuelve metadatos parciales o rutas extremadamente largas en entornos Windows, evitando excepciones no capturadas durante el recorrido del disco.
- `2026-10-08T02:49:05` **branding.py** (robustez ante casos límite): Mejoré la robustez de `save_logo_svg` y `draw_logo` ante valores de entrada extremos o inválidos, asegurando que el estado interno no se corrompa si se pasan datos fuera de rango o tipos inesperados, cumpliendo con el enfoque de robustez ante casos límite.
- `2026-10-08T02:48:20` **assistant.py** (robustez ante casos límite): Se mejora la robustez de `_is_safe_payload_structure` y `_is_input_too_deep_or_complex` añadiendo una comprobación explícita para evitar errores de `RecursionError` o evaluaciones infinitas ante estructuras cíclicas o excesivamente profundas, integrando una cota superior estricta en la profundidad de la recursión.
- `2026-10-08T02:39:23` **settings.py** (rendimiento): Optimizé la gestión de la caché de configuración en `_load_impl` utilizando un `try-finally` para asegurar el cierre del lock y evitar la lectura innecesaria de archivos vacíos/inválidos mediante una verificación de `st_size` previa a la apertura, reduciendo ciclos de I/O y llamadas al sistema.
- `2026-10-08T02:38:17` **safety.py** (rendimiento): Optimicé el rendimiento de las validaciones de seguridad mediante la implementación de un caché de resultados para `_is_kernel_managed` y `is_protected_path` basado en la resolución de rutas, evitando cálculos redundantes costosos en operaciones de disco frecuentes.
- `2026-10-08T02:29:13` **quarantine.py** (rendimiento): Se optimizó el acceso al sistema de archivos en `purge_all` y `load_manifest` mediante el uso de un diccionario de búsqueda para evitar iteraciones O(N) y se eliminó la recarga redundante del manifiesto al detectar archivos inexistentes, reduciendo significativamente las operaciones de I/O.
- `2026-10-08T02:28:29` **organizer.py** (rendimiento): Optimicé el rendimiento de `_process_directory` reemplazando múltiples llamadas a `stat()` en el bucle principal por el uso de `os.DirEntry.stat()`, que aprovecha la caché de atributos obtenida durante el escaneo inicial del sistema operativo, reduciendo drásticamente las syscalls innecesarias.
- `2026-10-08T02:27:43` **memory.py** (rendimiento): Se optimizó el bucle de enumeración de procesos en `top_memory_processes` reemplazando la creación innecesaria de objetos `ProcessMemory` para cada PID existente (muchos de los cuales son omitidos) por un filtrado previo más eficiente, reduciendo drásticamente las llamadas al sistema y la carga de memoria.
- `2026-10-08T02:19:23` **main.py** (rendimiento): Optimicé el sistema de caché implementando una invalidación granular basada en eventos en lugar de confiar solo en el TTL, evitando re-cálculos costosos (como `_compile_metrics`) cuando no ha habido cambios en los estados fuente, reduciendo significativamente la carga de CPU durante el bucle de salud.
- `2026-10-08T02:18:22` **healthscore.py** (rendimiento): Optimicé el rendimiento de `compute_score` convirtiendo `_PIPELINE` de una `List` a un `tuple` para asegurar inmutabilidad y mejorar ligeramente la velocidad de acceso, y sobre todo, moví el filtrado de mensajes imprimibles fuera del bucle de reglas a una función auxiliar para reducir la sobrecarga de procesamiento de strings.
- `2026-10-08T02:17:29` **diskreport.py** (rendimiento): Optimizé la función `_is_excluded_path` para evitar la conversión innecesaria a objetos `Path` y el uso de `.relative_to` (que realiza validaciones costosas) mediante un chequeo de cadena basado en `Path.parts`, reduciendo significativamente la sobrecarga por archivo durante el escaneo recursivo.
- `2026-10-08T02:08:57` **browser.py** (rendimiento): Se optimizó el rendimiento del escaneo recursivo mediante la sustitución de la lógica de chequeo de archivos en uso (`_is_file_in_use`), la cual realizaba llamadas costosas a `kernel32.CreateFileW` por cada archivo encontrado, reemplazándola por una verificación de metadatos `stat` que es órdenes de magnitud más rápida y segura.
- `2026-10-08T02:08:45` **branding.py** (rendimiento): Se optimizó el rendimiento del dibujado de franjas (`_draw_shield_stripes`) y gradientes (`draw_gradient_bar`) reemplazando cálculos redundantes en tiempo de ejecución por consultas al cache `_get_grouped_segments`, reduciendo drásticamente las llamadas a `create_rectangle` y `create_line` en el Canvas.
- `2026-10-08T02:08:02` **assistant.py** (rendimiento): Optimicé el cálculo de `active_problems` en `SystemContext` convirtiéndolo en un `@cached_property` para evitar iteraciones repetitivas sobre los criterios en cada acceso, aprovechando que el estado del contexto es inmutable durante su ciclo de vida tras la ingesta.
- `2026-10-08T01:58:32` **scanner.py** (legibilidad y documentación): Se introdujeron docstrings descriptivos y type hints faltantes en el orquestador principal (`Scanner`) y sus métodos auxiliares, mejorando la legibilidad técnica sin alterar la lógica de escaneo.
