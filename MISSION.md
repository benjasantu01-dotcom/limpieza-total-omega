# Misión actual del bucle autónomo

Editá este archivo para cambiar en qué se enfoca Gemini en cada corrida.
El bucle lo lee al principio de cada iteración.

## Reglas que NUNCA cambian, sin importar el enfoque

Estas están antes que cualquier objetivo. Un cambio que las viole se
rechaza aunque mejore todo lo demás:

1. **Nada se borra automáticamente.** Toda acción destructiva requiere
   confirmación explícita del usuario en la app.
2. **Nunca se toca una ruta de sistema.** Todo borrado o movimiento pasa
   por `app/safety.py` (`ensure_safe_to_modify` / `filter_safe_paths`).
3. **No se debilita la seguridad.** Las listas `PROTECTED_DIR_NAMES`,
   `SENSITIVE_EXTENSIONS`, `SYSTEM_FOLDER_BLOCKLIST` y `NEVER_TOUCH` solo
   pueden crecer, nunca encogerse. `evolve/guards.py` lo verifica.
4. **Destruir es reversible o no se hace.** Lo sospechoso va a cuarentena
   (`app/quarantine.py`), que guarda la ruta original para restaurar.
5. **Prohibido `shutil.rmtree`** y cualquier borrado recursivo de carpetas.
6. **Sin dependencias nuevas.** Solo librería estándar, `ctypes` y
   PowerShell. Nada de psutil, Pillow, etc.
7. **Los parsers reciben texto crudo por parámetro**, así se pueden testear
   en Linux (los tests corren en GitHub Actions, no en Windows).
8. **Todo cambio debe pasar los tests** antes de aceptarse.

## Estado del proyecto

Módulos existentes en `app/`:

| Archivo | Qué hace | Toca el disco |
|---|---|---|
| `safety.py` | Valida rutas, bloquea las de sistema | no |
| `branding.py` | Nombre, paleta, tipografía, logo por código | no |
| `main.py` | Interfaz con pestañas (customtkinter) | vía los demás |
| `organizer.py` | Busca basura, mueve a revisión, borra revisados | sí (con confirmación) |
| `scanner.py` | Heurísticas de archivos sospechosos + Defender | no |
| `quarantine.py` | Aísla y restaura archivos, con manifiesto | sí (reversible) |
| `memory.py` | Diagnóstico honesto de RAM, top de procesos | no |
| `duplicates.py` | Encuentra duplicados por hash en 3 pasos | no |
| `diskreport.py` | Uso de disco por unidad, extensión y carpeta | no |
| `startup.py` | Inventario de programas de arranque | no |
| `browser.py` | Detecta y mide cachés de navegador | no |
| `healthscore.py` | Combina todo en un puntaje 0-100 (función pura) | no |
| `reporting.py` | Informe unificado en texto y Markdown | solo al guardar |

## Prioridades por módulo

Para la categoría **funcionalidad incremental** (la única que puede sumar
comportamiento nuevo, y que puede buscar en la web cómo lo resuelven
limpiadores y antivirus reales). Siempre de forma aditiva:

**safety.py**
- Detectar puntos de reparse / junctions y no seguirlos.
- Reconocer rutas UNC (`\\servidor\recurso`) y tratarlas con cuidado.
- Detectar si un archivo está en uso antes de intentar moverlo.

**organizer.py**
- Exclusión de carpetas elegidas por el usuario, persistida.
- Reglas de antigüedad (ej. solo temporales de más de 30 días).
- Estimación de espacio recuperable antes de mover nada.

**scanner.py**
- Más heurísticas: extensiones ocultas con caracteres RTL, ejecutables
  sin firma en carpetas de usuario, scripts en Startup.
- Consultar el estado de Defender (`Get-MpComputerStatus`) y avisar si la
  protección en tiempo real está apagada.
- Chequear si el firewall está activo (solo informar, nunca cambiarlo).

**memory.py**
- Uso del archivo de paginación y detección de presión sostenida.
- Historial de mediciones en memoria para mostrar tendencia.
- Detectar procesos con crecimiento constante (posible fuga).

**duplicates.py**
- Detección de imágenes parecidas por tamaño + fecha (sin dependencias).
- Modo "solo dentro de la misma carpeta" para acelerar.
- Exportar el listado de grupos a CSV.

**diskreport.py**
- Árbol de carpetas con porcentaje relativo al total.
- Detección de carpetas que crecieron desde el último análisis.
- Aviso de unidades por debajo del 10% libre.

**startup.py**
- Leer también tareas programadas (`Get-ScheduledTask`), solo lectura.
- Estimar impacto real por tamaño del ejecutable.
- Marcar entradas cuyo ejecutable ya no existe (huérfanas).

**browser.py**
- Soportar perfiles múltiples (`Profile 1`, `Profile 2`...).
- Detectar Firefox (estructura distinta: `Profiles/*.default*/cache2`).
- Avisar si el navegador está abierto antes de sugerir limpiar.

**healthscore.py**
- Guardar histórico de puntajes para mostrar evolución.
- Pesos configurables por el usuario.
- Explicar qué acción concreta sube cada área del puntaje.

**main.py**
- Barra de progreso real en las tareas largas.
- Botón de cancelar en los análisis que recorren disco.
- Recordar la última carpeta usada entre sesiones.
- Atajos de teclado y navegación accesible por tabulador.

**reporting.py**
- Comparar dos informes y mostrar qué cambió.
- Exportar a HTML con la paleta de `branding.py`.

## Cómo priorizar cuando no hay nada obvio

En orden: primero seguridad defensiva sobre `safety.py` y `quarantine.py`,
después casos límite en los módulos que recorren disco, después
legibilidad y documentación en castellano, y al final rendimiento.
