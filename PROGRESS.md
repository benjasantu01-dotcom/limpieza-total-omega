# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **184** (36.5% de aceptación)
- Rechazadas por tests: 27
- Rechazadas por guardia de seguridad: 43
- Sin cambios (nada sustancial que mejorar): 18
- Sin respuesta de la IA (error o límite): 232

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-09-27 | 97 | 21 | 23 | 12 | 135 |
| 2026-09-28 | 87 | 6 | 20 | 6 | 97 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **54**
- manejo de errores y validación de entradas: **36**
- robustez ante casos límite: **32**
- rendimiento: **31**
- seguridad defensiva: **31**

## Mejoras aceptadas por archivo

- `safety.py`: **17**
- `browser.py`: **17**
- `duplicates.py`: **17**
- `quarantine.py`: **17**
- `diskreport.py`: **16**
- `healthscore.py`: **16**
- `scanner.py`: **14**
- `memory.py`: **13**
- `assistant.py`: **12**
- `settings.py`: **11**
- `main.py`: **10**
- `organizer.py`: **8**
- `startup.py`: **8**
- `branding.py`: **8**

## Últimas 15 mejoras aceptadas

- `2026-09-28T09:11:14` **safety.py** (robustez ante casos límite): Se ha añadido `_is_sparse_file` mediante la constante `FILE_ATTRIBUTE_SPARSE_FILE` (0x200) para reforzar la detección de archivos dispersos que podrían ocultar datos o causar errores de escritura, integrando esta comprobación robusta en la validación de integridad (`_VALIDATORS`) y en los diagnósticos.
- `2026-09-28T09:10:25` **quarantine.py** (robustez ante casos límite): Se ha introducido un chequeo de existencia previa del archivo en `_atomic_isolate_file` para evitar race conditions y comportamientos indefinidos ante archivos que cambian de estado durante la ejecución, reforzando la robustez ante concurrencia.
- `2026-09-28T09:09:43` **organizer.py** (robustez ante casos límite): Se reforzó la robustez de `organizer.py` añadiendo chequeos de integridad en las operaciones con rutas (validación de `is_absolute` y existencia de padres) y mejorando el manejo de errores en `_get_win_attributes` para prevenir bloqueos por atributos inesperados.
- `2026-09-28T09:03:08` **main.py** (robustez ante casos límite): Mejoré la robustez de la aplicación ante estados de red inciertos y errores de hilo principal añadiendo una validación de salud de los widgets antes de cualquier operación de UI en los callbacks asíncronos (`_safe_run_ui_callback` y `_flush_logs`), y asegurando que las llamadas de persistencia de configuración manejen correctamente widgets que podrían haber sido destruidos.
- `2026-09-28T08:59:42` **healthscore.py** (robustez ante casos límite): Mejoré la robustez de `SystemMetrics` y `compute_score` ante valores inesperados (como `None` o estados de error parciales) asegurando que el motor de puntuación siempre devuelva un resultado válido y coherente, incluso si los datos de entrada provienen de sensores fallidos.
- `2026-09-28T08:50:20` **browser.py** (robustez ante casos límite): Se añadió una validación de existencia (`p.exists()`) en `_resolve_browser_path` antes de intentar resolver rutas, evitando que el módulo falle silenciosamente al procesar rutas relativas que no existen en el sistema (un caso límite común en perfiles de usuario incompletos).
- `2026-09-28T08:40:23` **startup.py** (rendimiento): Se optimizó `entries_from_folders` eliminando el uso innecesario de `is_safe_to_modify` dentro del bucle principal, ya que `is_protected_path` junto con la lógica de `os.scandir` es suficiente y más performante para el filtrado inicial, evitando llamadas redundantes a `Path` y chequeos de seguridad extra en archivos que ya se sabe que son seguros.
- `2026-09-28T08:40:07` **settings.py** (rendimiento): Optimizé la gestión de memoria y el rendimiento de acceso a `settings.py` implementando un `lru_cache` específico en `load` para evitar lecturas de disco innecesarias durante llamadas repetidas dentro de la misma iteración, minimizando también las llamadas a `stat()` al verificar el `mtime` del archivo una sola vez por acceso.
- `2026-09-28T08:39:34` **scanner.py** (rendimiento): Se implementó un filtrado preventivo en el bucle principal de `scan_directory` utilizando `is_protected_path` sobre la ruta del directorio antes de realizar el `scandir`, evitando así exploraciones redundantes y el costo de instanciar `os.DirEntry` en carpetas que ya sabemos que son protegidas por definición, optimizando el rendimiento de I/O.
- `2026-09-28T08:39:06` **safety.py** (rendimiento): Se ha optimizado la validación de rutas de sistema utilizando una búsqueda de prefijos constante y pre-calculada (`_SYSTEM_ROOT_PATHS_TUPLE`) en lugar de iteraciones y normalizaciones repetidas, mejorando el rendimiento en operaciones de escaneo masivo de disco.
- `2026-09-28T08:29:46` **quarantine.py** (rendimiento): Optimizé `load_manifest` reemplazando la validación física de archivos (que requiere I/O lento) por un procesamiento en memoria utilizando un diccionario, evitando llamadas repetidas a `exists()` y `stat()` sobre el disco, delegando la integridad física a los métodos que realmente requieren acceder al archivo (como `restore` o `purge`).
- `2026-09-28T08:28:44` **memory.py** (rendimiento): Se optimizó el rendimiento de `top_memory_processes` eliminando el uso de `Sort-Object` y `Select-Object` dentro de la llamada a PowerShell, moviendo el filtrado y ordenamiento al lado de Python, lo cual reduce drásticamente el tiempo de ejecución del comando y el uso de memoria en la sub-shell.
- `2026-09-28T08:19:14` **healthscore.py** (rendimiento): Optimicé el cálculo del score evitando la creación innecesaria de objetos `NamedTuple` y funciones `lambda` en tiempo de ejecución, además de reemplazar la re-instanciación del diccionario de desglose por una pre-asignación eficiente.
- `2026-09-28T08:18:50` **duplicates.py** (rendimiento): Optimizé el rendimiento de `_collect_candidates` utilizando `os.scandir` de forma más eficiente al consolidar los filtros de seguridad y atributos antes de realizar llamadas costosas al sistema de archivos (`stat`), reduciendo drásticamente la latencia en directorios con gran cantidad de archivos.
- `2026-09-28T08:09:51` **browser.py** (rendimiento): Optimicé el rendimiento de `directory_size` y `detect_profiles` evitando cálculos redundantes mediante la consolidación del `memo` (para detectar archivos ya contados) y utilizando una única instancia de `kernel32` compartida entre los procesos recursivos, reduciendo la sobrecarga de llamadas a la API de Windows.
