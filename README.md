# Limpieza Total Omega

Proyecto de demostración: una app de escritorio para Windows 11 que
organiza archivos basura y hace un escaneo heurístico de seguridad,
combinada con un bucle autónomo en la nube que mejora el código solo,
usando la API gratuita de Gemini (Google AI Studio).

## Estructura

```
app/          → la app de escritorio (la corrés vos, manualmente, en tu PC)
  organizer.py  → busca y organiza archivos basura (nunca borra sin confirmación)
  scanner.py    → escaneo heurístico + integración con Windows Defender real
  main.py       → interfaz gráfica (customtkinter), con selector de disco/carpeta

evolve/       → el bucle autónomo (corre en GitHub Actions, en la nube)
  evolve.py     → llama a Gemini, propone un cambio, lo valida, y commitea si pasa
  guards.py     → validaciones previas a los tests (sintaxis, pérdida de código)
  budget.py     → controla que no se pase del límite diario gratis
  tracking.py   → rotación persistente de archivo/enfoque + métricas
  tests/        → tests que un cambio debe pasar para ser aceptado

MISSION.md    → la "guía" que le decís al bucle en qué enfocarse esta semana
evolve_log.md → se genera solo, historial cronológico de cada decisión
PROGRESS.md   → se genera solo, resumen con métricas por día/enfoque/archivo
```

## Cómo trabaja el bucle

Cada 5 minutos, GitHub Actions dispara una corrida que hace **una** mejora:

1. Chequea el presupuesto diario (tope 350, objetivo 300 de las 500 gratis).
2. Elige qué archivo y con qué enfoque, rotando sistemáticamente sobre las
   18 combinaciones posibles (3 archivos × 6 enfoques), así ninguno queda sin
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

1. **El bucle autónomo solo edita código dentro del repo.** Nunca ejecuta
   comandos de limpieza reales ni toca tu PC directamente.
2. **Tres capas de validación antes de aceptar un cambio**, porque los tests
   solos no alcanzan:
   - *Archivo correcto*: si la respuesta viene etiquetada con otro archivo,
     se descarta (evita mezclar dos módulos).
   - *Guardias* (`evolve/guards.py`): valida la sintaxis con AST, rechaza
     encogimientos sospechosos del archivo, y rechaza si desapareció alguna
     función, clase o método que existía antes. Esto es lo único que protege
     a `app/main.py`, que ningún test puede importar en CI (necesita
     customtkinter y una pantalla real).
   - *Tests* (`evolve/tests/`): 30 pruebas de comportamiento real sobre
     `organizer` y `scanner`, más las de las propias guardias y la rotación.
3. **Si algo falla, se revierte solo** y queda logueado con el motivo exacto.
4. **Presupuesto diario controlado** (`evolve/budget.py`): objetivo de 300
   peticiones/día con tope duro de 350, sobre una cuota gratuita real de 500.
   Además espacia las llamadas para respetar el límite por minuto, y detecta
   cuando la cuota diaria se agotó para no insistir al vacío.
5. **Corte de tiempo propio** por corrida, para terminar prolijo y alcanzar a
   commitear siempre, en vez de que GitHub mate el job a mitad de camino.
6. **La app de escritorio nunca borra nada directamente.** Mueve candidatos a
   una carpeta `_Para_Revisar`; el borrado real es un botón aparte que el
   usuario aprieta a propósito. Un test verifica que nunca aparezca un borrado
   masivo en el código, ni siquiera introducido por la IA.

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
3. El workflow `.github/workflows/evolve.yml` ya corre cada 5 minutos
   automáticamente. También se puede disparar a mano desde la pestaña
   Actions ("Run workflow").
4. Editá `MISSION.md` cuando quieras cambiar el foco de las mejoras.

> **Importante sobre minutos de GitHub Actions:** en repositorios **privados**
> el plan gratuito incluye 2.000 minutos/mes, y cada corrida se factura
> redondeando hacia arriba al minuto. A una corrida cada 5 minutos son ~288
> corridas/día, o sea que la cuota mensual se agota en menos de una semana y
> los workflows dejan de correr. En repositorios **públicos** Actions es
> gratis e ilimitado. Si querés que el bucle sostenga el ritmo 24/7 durante
> toda la semana, la opción sin costo es tener el repo público (los secretos
> siguen siendo privados: `GEMINI_API_KEY` no se expone).

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
