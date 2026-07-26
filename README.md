# Limpieza Total Omega

Suite de mantenimiento y seguridad para Windows 11: limpieza, antivirus
heurístico, diagnóstico de RAM, análisis de disco, duplicados, arranque,
cachés de navegador y cuarentena reversible, en una sola app con pestañas.
Además trae un bucle autónomo en la nube que mejora su propio código, usando
la API gratuita de Gemini (Google AI Studio).

## Estructura

```
app/            → la app de escritorio (la corrés vos, en tu PC)
  safety.py       → capa de seguridad: bloquea rutas de sistema (base de todo)
  branding.py     → nombre, paleta, tipografía y logo generado por código
  main.py         → interfaz con pestañas (customtkinter)
  organizer.py    → busca basura y la mueve a revisión (nunca borra solo)
  scanner.py      → escaneo heurístico + Windows Defender real
  quarantine.py   → aísla archivos sospechosos y los puede restaurar
  memory.py       → diagnóstico honesto de RAM (no es un "limpiador de RAM")
  duplicates.py   → duplicados por hash en 3 pasos (solo lectura)
  diskreport.py   → uso de disco por unidad, extensión y carpeta (solo lectura)
  startup.py      → inventario de programas de arranque (solo lectura)
  browser.py      → detecta y mide cachés de navegador (solo lectura)
  healthscore.py  → combina todo en un puntaje 0-100 (función pura)
  reporting.py    → informe unificado en texto y Markdown

evolve/         → el bucle autónomo (corre en GitHub Actions, en la nube)
  evolve.py       → llama a Gemini, propone un cambio, lo valida y commitea
  guards.py       → validaciones previas a los tests (sintaxis, pérdida de código)
  budget.py       → controla que no se pase del límite diario gratis
  tracking.py     → rotación persistente de archivo/enfoque + métricas
  chain.py        → frenos de la cadena de auto-disparo
  logrotate.py    → recorta y archiva los logs para que el repo no se infle
  tests/          → 197 tests que un cambio debe pasar para ser aceptado

MISSION.md      → la guía que le decís al bucle: reglas fijas + roadmap por módulo
evolve_log.md   → se genera solo, historial cronológico de cada decisión
PROGRESS.md     → se genera solo, resumen con métricas por día/enfoque/archivo
```

## Qué hace la app

| Pestaña | Qué hace | Modifica el disco |
|---|---|---|
| **Salud** | Puntaje 0-100 combinando todas las áreas, con recomendaciones | no |
| **Limpieza** | Busca temporales, los mueve a revisión, y borra si vos lo pedís | sí, con confirmación |
| **Seguridad** | Heurísticas de archivos sospechosos + Windows Defender | no |
| **Cuarentena** | Aísla lo sospechoso y lo restaura a su ruta original | sí, reversible |
| **Memoria** | Estado real de la RAM y qué procesos consumen | no |
| **Disco** | En qué se fue el espacio: unidades, extensiones, carpetas | no |
| **Duplicados** | Encuentra copias idénticas y sugiere cuál conservar | no |
| **Navegadores** | Mide la caché de Chrome, Edge, Brave, Opera, Vivaldi | no |
| **Inicio** | Qué arranca con Windows y cuánto pesa el arranque | no |
| **Informe** | Exporta todo lo analizado a .txt o .md | solo el archivo que elegís |

### Sobre los "limpiadores de RAM"

La pestaña de Memoria **no** libera RAM a la fuerza, y es a propósito. Las
apps que prometen eso llaman a `EmptyWorkingSet` sobre todos los procesos:
el número de "memoria libre" sube, pero el rendimiento empeora, porque
Windows tiene que releer del disco lo que acaba de descartar. En un sistema
moderno la RAM ocupada como caché es lo que hace que los programas abran
rápido. Así que acá se mide, se explica y se muestra qué conviene cerrar.
El trim manual existe, pero limitado a un PID y con la advertencia puesta.

## Cómo trabaja el bucle

Cada 5 minutos, GitHub Actions dispara una corrida que hace **una** mejora:

1. Chequea el presupuesto diario (tope 350, objetivo 300 de las 500 gratis).
2. Elige qué archivo y con qué enfoque, rotando sistemáticamente sobre las
   78 combinaciones posibles (13 módulos × 6 enfoques), así ninguno queda sin
   recibir todos los enfoques.
3. Le pide a Gemini una mejora sustancial, con una justificación de una línea.
   En el enfoque de "funcionalidad incremental" además puede buscar en la web
   cómo lo resuelven limpiadores/antivirus reales.
4. Valida la propuesta (ver más abajo), corre los tests, y solo acepta si todo
   pasa. Si no, revierte y lo deja registrado.
5. Actualiza `evolve_log.md`, `PROGRESS.md` y las métricas, y commitea.

Los 6 enfoques que rota: manejo de errores, legibilidad/documentación,
rendimiento, casos límite, seguridad defensiva, y funcionalidad incremental
(el único que puede sumar comportamiento nuevo, siempre de forma aditiva).

## Diseño de seguridad (léelo antes de la demo)

El problema de fondo: una IA reescribe `app/*.py` sin supervisión durante
días, y esos módulos borran archivos. "Que la IA tenga cuidado" no es una
defensa. Así que la seguridad está puesta como **estructura**, en capas que
la IA no puede sacar sin que el cambio se rechace.

### En la app

1. **`app/safety.py` es la única puerta de las operaciones destructivas.**
   Bloquea la raíz de cualquier unidad, cualquier ruta que contenga una
   carpeta de sistema (Windows, System32, Program Files, ProgramData,
   WinSxS, `$RECYCLE.BIN`, `.ssh`, y ~40 más), y las extensiones sensibles
   (`.sys`, `.dll`, `.exe`, `.pem`...). Lanza excepción en vez de devolver
   `False`, para que un olvido de chequear el resultado no termine en un
   borrado.
2. **Las rutas se resuelven antes de compararse.** Es lo que impide que un
   `carpeta/../../Windows/System32` se cuele en una operación que debería
   estar limitada a una carpeta.
3. **Destruir es reversible o no se hace.** Lo sospechoso va a cuarentena
   (`app/quarantine.py`), que guarda la ruta original en un manifiesto y
   puede devolver el archivo exactamente a su lugar. Restaurar hacia una
   ruta de sistema está bloqueado, y vaciar la cuarentena verifica que cada
   archivo esté realmente dentro de ella antes de borrarlo.
4. **8 de los 13 módulos no modifican nada.** Memoria, disco, duplicados,
   arranque y navegadores son de solo lectura: informan y explican.
5. **Nada se borra sin dos pasos.** Un diálogo que dice exactamente qué va a
   pasar, más la validación de `safety.py`.

### En el bucle autónomo

6. **El bucle solo edita archivos de texto dentro del repo.** Nunca ejecuta
   comandos de limpieza ni toca tu PC.
7. **Cuatro capas de validación antes de aceptar un cambio**, porque los
   tests solos no alcanzan:
   - *Archivo correcto*: si la respuesta viene etiquetada con otro archivo,
     se descarta.
   - *Guardias de integridad* (`evolve/guards.py`): sintaxis vía AST,
     rechazo de encogimientos sospechosos, y rechazo si desapareció alguna
     función, clase o método. Esto es lo único que protege a `app/main.py`,
     que ningún test puede importar en CI (necesita customtkinter y pantalla).
   - *Guardias de seguridad*: `safety.py` y `quarantine.py` están marcados
     como módulos críticos y no pueden perder sus funciones clave. Además
     las listas de protección **solo pueden crecer**: si la IA "simplifica"
     `PROTECTED_DIR_NAMES` sacando carpetas, el cambio se rechaza.
   - *Tests* (`evolve/tests/`): 197 pruebas, de las cuales las de
     `test_safety.py` son un contrato de seguridad, no de funcionalidad.
     Verifican que un archivo de sistema nunca se mueva, que la cuarentena
     no pueda borrar fuera de sí misma, y que un manifiesto manipulado no
     pueda escribir en `System32`.
8. **Si algo falla, se revierte solo** y queda logueado con el motivo exacto.
4. **Presupuesto diario controlado** (`evolve/budget.py`): objetivo de 300
   peticiones/día con tope duro de 350, sobre una cuota gratuita real de 500.
   Además espacia las llamadas para respetar el límite por minuto, y detecta
   cuando la cuota diaria se agotó para no insistir al vacío.
5. **Corte de tiempo propio** por corrida, para terminar prolijo y alcanzar a
   commitear siempre, en vez de que GitHub mate el job a mitad de camino.
11. **La limpieza nunca borra directamente.** Mueve candidatos a
    `_Para_Revisar`; el borrado real es un botón aparte que el usuario
    aprieta a propósito. Un test verifica que `shutil.rmtree` no aparezca en
    ningún módulo, ni siquiera introducido por la IA.
12. **Rotación de logs** (`evolve/logrotate.py`): recorta `evolve_log.md` y
    `metrics.jsonl` dejando lo reciente y archivando el resto, así una semana
    de corridas no infla el repo. Solo borra dentro de `evolve/archive/`, y
    verifica la contención de rutas antes de cada borrado.

## Cómo arrancar

### 1. La app de escritorio
```bash
pip install -r requirements.txt
python app/main.py
```

### 2. El bucle autónomo
1. En Google AI Studio, generá una `GEMINI_API_KEY`.
2. En GitHub: Settings → Secrets and variables → Actions → agregá el
   secreto `GEMINI_API_KEY`.
3. Agregá el secreto `SELF_TRIGGER_TOKEN` (ver más abajo) para que el bucle
   se mantenga vivo solo. Sin él funciona, pero depende del cron de GitHub,
   que es poco confiable.
4. Editá `MISSION.md` cuando quieras cambiar el foco de las mejoras.

## Cómo se mantiene vivo 24/7 (y por qué no alcanza el cron)

El cron de GitHub Actions no es puntual: GitHub avisa que el evento
`schedule` se atrasa o se descarta cuando hay carga alta. En este repo,
medido con la API, en las primeras ~10 horas con cron activo se ejecutó
**1 sola** de las ~19 corridas esperadas.

Por eso el workflow **se re-dispara a sí mismo** al terminar cada corrida,
logrando un latido propio de ~10 minutos independiente del cron (que queda
solo como reinicio de respaldo). Para eso necesita un token propio, porque
el `GITHUB_TOKEN` por defecto no puede disparar nuevos workflows (GitHub lo
bloquea justamente para evitar bucles infinitos).

**Crear el token:** Settings de tu cuenta → Developer settings → Personal
access tokens → Fine-grained tokens → Generate new token. Dale acceso solo
a este repositorio, con permisos `Actions: Read and write` y
`Contents: Read and write`. Copiá el token y guardalo en el repo como
secreto `SELF_TRIGGER_TOKEN`.

### Cómo frenar la cadena
De la más rápida a la más suave:
1. Actions → Evolve (bucle autónomo) → `...` → **Disable workflow**.
2. Borrar el secreto `SELF_TRIGGER_TOKEN` (sin token no se re-dispara).
3. Crear un archivo vacío llamado `STOP_CHAIN` en la raíz del repo.

### Frenos automáticos ya puestos
- **Tope de 200 eslabones por día** (`evolve/chain.py`). Al alcanzarlo la
  cadena se detiene y el cron queda como reinicio.
- **Antipileup**: antes de re-disparar, verifica que no haya otra corrida en
  curso o en cola. Sin esto, el cron y la cadena podrían generar cadenas
  paralelas que se multiplican.
- **Presupuesto de Gemini** (tope duro 350/día): aunque la cadena siga viva,
  deja de gastar cuota.
- **Reinicio diario automático**: el contador de eslabones se resetea cada
  día, así el bucle retoma solo.

> **Sobre minutos de Actions:** en repos **privados** el plan gratuito da
> 2.000 minutos/mes y cada corrida se factura redondeando al minuto, así que
> un ritmo de 24/7 agota la cuota en menos de una semana. En repos
> **públicos** Actions es gratis e ilimitado. Este repo es público por eso.
> Los secretos siguen siendo privados: GitHub censura sus valores en los
> logs, así que la `GEMINI_API_KEY` no queda expuesta.

### 3. Para la demo con tus compañeros
- `PROGRESS.md` → el resumen ejecutivo: cuántas mejoras se aceptaron y
  rechazaron cada día, cómo se reparten por enfoque y por archivo. Ideal para
  mostrar avance día a día.
- `evolve_log.md` → el detalle cronológico de cada decisión, con la
  justificación de la IA y el motivo de cada rechazo.
- El historial de commits → cada mejora aceptada es un commit del bot.

Lo más convincente no es "corrió solo": es mostrar que el sistema **rechaza**
cambios malos y explica por qué. Los rechazos son evidencia de criterio, no de
fracaso.
