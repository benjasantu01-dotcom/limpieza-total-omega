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
