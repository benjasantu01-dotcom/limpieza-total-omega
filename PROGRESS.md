# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **235** (46.6% de aceptación)
- Rechazadas por tests: 16
- Rechazadas por guardia de seguridad: 34
- Sin cambios (nada sustancial que mejorar): 13
- Sin respuesta de la IA (error o límite): 206

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-08-27 | 145 | 10 | 20 | 7 | 134 |
| 2026-08-28 | 90 | 6 | 14 | 6 | 72 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **55**
- manejo de errores y validación de entradas: **51**
- robustez ante casos límite: **45**
- rendimiento: **44**
- seguridad defensiva: **40**

## Mejoras aceptadas por archivo

- `scanner.py`: **23**
- `assistant.py`: **21**
- `branding.py`: **20**
- `memory.py`: **20**
- `quarantine.py`: **20**
- `browser.py`: **18**
- `settings.py`: **18**
- `diskreport.py`: **18**
- `duplicates.py`: **17**
- `healthscore.py`: **17**
- `main.py`: **13**
- `startup.py`: **13**
- `safety.py`: **11**
- `organizer.py`: **6**

## Últimas 15 mejoras aceptadas

- `2026-08-28T07:52:01` **branding.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `save_logo_svg` validando la existencia de la carpeta padre mediante `is_safe_to_modify` antes de intentar crearla, evitando posibles escrituras en rutas bloqueadas por el sistema o fuera del alcance permitido.
- `2026-08-28T07:51:45` **assistant.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_call_gemini` añadiendo una validación explícita del tamaño del payload antes del envío y limitando estrictamente el uso de `json.dumps` a los datos ya saneados, previniendo inyecciones de encabezados o malformaciones en la solicitud HTTP.
- `2026-08-28T07:51:10` **startup.py** (robustez ante casos límite): Se ha mejorado la robustez de `StartupEntry._resolve_and_cache_path` añadiendo un manejo explícito para `PermissionError` y `OSError` durante la normalización y resolución de rutas, evitando que la app falle ante archivos bloqueados o sin privilegios de acceso (un caso límite común en carpetas de sistema).
- `2026-08-28T07:50:43` **settings.py** (robustez ante casos límite): Se ha mejorado la robustez de `settings.py` al implementar una verificación de salud atómica en `load()` que detecta archivos de configuración bloqueados o en uso parcial mediante `os.access(ruta, os.R_OK)`, evitando excepciones críticas y retornando proactivamente los valores de fábrica en entornos con alta concurrencia de I/O.
- `2026-08-28T07:41:25` **scanner.py** (robustez ante casos límite): Se añadió una verificación de estado de archivo (`entry.is_symlink()`) en el bloque de heurísticas de `Scanner.process_entry` para prevenir errores de acceso a enlaces simbólicos rotos o recursivos que escapan a la lógica de `_is_reparse_point`, mejorando la robustez ante archivos inexistentes.
- `2026-08-28T07:40:32` **quarantine.py** (robustez ante casos límite): Se ha añadido una validación de longitud de nombre de archivo antes de la copia atómica para prevenir errores `OSError` (Nombre de archivo demasiado largo) en Windows, asegurando que el sandbox no falle ante rutas profundas.
- `2026-08-28T07:31:45` **memory.py** (robustez ante casos límite): Mejoré la robustez de `read_snapshot` y `top_memory_processes` añadiendo validaciones explícitas contra posibles estados corruptos (archivos vacíos o errores de lectura imprevistos) que podrían causar fallos en cascada en las funciones de parsing, garantizando una salida segura ante entornos degradados.
- `2026-08-28T07:31:17` **main.py** (robustez ante casos límite): Mejoré la robustez de la aplicación ante el cierre inesperado de la ventana y el manejo de recursos, añadiendo una comprobación exhaustiva de `winfo_exists()` antes de cualquier interacción con widgets de `customtkinter` o `tkinter` en los callbacks de los hilos de trabajo, previniendo excepciones `TclError` que ocurrían durante el proceso de apagado de la app.
- `2026-08-28T07:21:11` **duplicates.py** (robustez ante casos límite): Se ha mejorado la robustez de `_scan_recursive` frente a rutas con caracteres especiales o estados inconsistentes al añadir un manejo de excepciones específico para `OSError` durante el acceso a atributos de archivo (`stat`) y al iterar, evitando que una entrada dañada detenga el escaneo completo.
- `2026-08-28T07:21:00` **diskreport.py** (robustez ante casos límite): Mejoré la robustez de `walk_files` y `drive_usage` ante condiciones de carrera y denegación de acceso, implementando una gestión de excepciones más granular para evitar que el escaneo se interrumpa prematuramente al encontrar archivos bloqueados o en uso.
- `2026-08-28T07:13:14` **assistant.py** (robustez ante casos límite): Se mejora la robustez de `SystemContext.ingest` y `_validate_and_assign` mediante la implementación de una validación explícita de tipos numéricos antes del casteo, evitando fallos ante valores `NaN`, `inf`, o tipos de datos contenedores (listas/dict) que puedan ser inyectados accidentalmente, protegiendo al asistente de procesar datos inválidos.
- `2026-08-28T07:12:45` **startup.py** (rendimiento): Se implementó un filtrado preventivo en `entries_from_folders` mediante un `set` de extensiones pre-compilado y la eliminación de la creación innecesaria de objetos `Path` para archivos que no son ejecutables, reduciendo drásticamente las llamadas al sistema y la presión sobre el recolector de basura durante el escaneo.
- `2026-08-28T07:11:54` **settings.py** (rendimiento): Optimicé el rendimiento de `load()` evitando lecturas redundantes de disco mediante el uso del timestamp de modificación (`st_mtime`) y la caché existente, y mejoré la eficiencia de `_Validators` convirtiendo las comprobaciones de clave en búsquedas de diccionario de tiempo constante.
- `2026-08-28T07:11:24` **scanner.py** (rendimiento): Optimicé el rendimiento del escaneo transformando `WATCHED_FOLDERS` de un `frozenset` de strings a un `frozenset` de nombres base normalizados, y eliminé el bucle `any()` dentro de `check_recent_executable_in_downloads` a favor de una verificación directa de pertenencia, evitando iteraciones innecesarias por cada archivo escaneado.
- `2026-08-28T07:00:37` **safety.py** (rendimiento): Optimicé el rendimiento de `is_protected_path` reemplazando la creación dinámica de un `set` de partes por una verificación más eficiente mediante `any` sobre los componentes de la ruta, evitando la sobrecarga de asignación de memoria en cada iteración y aprovechando el `lru_cache` existente de forma más efectiva.
