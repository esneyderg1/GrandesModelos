# Guía del agente SQL — cómo funciona y cómo explicarlo

Esta guía es para entender (y poder explicar) el chat que responde preguntas sobre los
bots RPA. Se lee en 10 minutos. No hace falta saber de modelos de lenguaje.

---

## 1. La idea en 30 segundos

1. El usuario escribe una pregunta en español.
2. Un modelo de lenguaje **que corre en el mismo computador** la convierte en una consulta SQL.
3. La aplicación **revisa** esa consulta y la ejecuta sobre la base de datos en modo **solo lectura**.
4. El mismo modelo recibe el resultado y lo explica en español.

Las tres decisiones de diseño que hay que saber defender:

| Decisión | Qué significa | Para qué |
|---|---|---|
| **El modelo no parte de cero** | Recibe una base de consultas ya probadas y las usa como guía | Acierta más: no inventa tablas ni columnas |
| **El modelo propone, el código dispone** | El modelo solo escribe texto; quien ejecuta (o bloquea) es nuestro código | Seguridad: no puede borrar ni modificar nada |
| **Lo difícil ya está resuelto** | El ROI está precalculado en una vista; el modelo solo la consulta | Mismas cifras que la pestaña de ROI, y respuestas más rápidas |

---

## 2. El recorrido de una pregunta

```
Usuario: "¿Cuál es el desarrollador con más soluciones?"
   │
   ▼
app/chat.py                      interfaz (Streamlit). Si la pregunta llega por voz,
   │                             aquí se pasa a texto con Whisper.
   │  ask(pregunta, historial, modelo)
   ▼
src/agent/sql_agent.py           el recorrido completo está en la función ask()
   │
   ├─ Paso 1 · El modelo propone una consulta
   │     _call_model()  ───────►  Ollama (qwen2.5-coder:7b)
   │     le enviamos:   prompt del sistema + conversación + pregunta
   │     nos devuelve:  ```sql SELECT ... ```
   │
   ├─ Paso 2 · Validar y ejecutar            (src/agent/database.py)
   │     candado 1: validate_sql()    → ¿es un único SELECT?
   │     candado 2: conexión mode=ro  → SQLite en solo lectura
   │     si el SQL falla → se le muestra el error al modelo y corrige (1 vez)
   │
   └─ Paso 3 · El modelo explica el resultado
         _call_model()  ───────►  Ollama
         le enviamos:   la tabla de resultados
         nos devuelve:  la explicación en español
   │
   ▼
app/chat.py muestra: la explicación + "Ver SQL ejecutado" + la tabla
```

**Ejemplo real** (así respondió el agente en las pruebas):

| Paso | Qué pasó |
|---|---|
| Pregunta | ¿Cuál es el desarrollador con más soluciones? |
| 1. El modelo escribió | `SELECT Desarrollador, COUNT(DISTINCT NombreBot) AS Num_Soluciones FROM TiemposManuales_clean GROUP BY Desarrollador ORDER BY Num_Soluciones DESC LIMIT 10` |
| 2. Se ejecutó | 4 filas: 45, 19, 18 y 10 soluciones |
| 3. El modelo explicó | "El desarrollador con más soluciones es … con un total de 45 soluciones…" |

---

## 3. Mapa de archivos: ¿qué busco y dónde está?

Los cuatro archivos del agente están en `src/agent/`. Cada uno tiene una sola responsabilidad:

| Si quiero ver… | Archivo | Qué mirar |
|---|---|---|
| **Dónde se consume el modelo** | `src/agent/sql_agent.py` | `_call_model()` — es el **único** punto que llama al modelo (`ollama.chat`) |
| **Qué modelo se usa** | `src/agent/sql_agent.py` | `DEFAULT_MODEL = "qwen2.5-coder:7b"` (se puede cambiar en el selector de la barra lateral) |
| **El recorrido completo** | `src/agent/sql_agent.py` | `ask()` — los tres pasos, comentados |
| **El prompt** | `src/agent/prompts.py` | `build_system_prompt()`, `INTERPRETATION_PROMPT`, `RETRY_PROMPT` |
| **La base de consultas (la guía del modelo)** | `src/agent/query_catalog.py` | `REFERENCE_QUERIES` (ejemplos) y `VIEWS` (vista del ROI) |
| **Las tablas que conoce el modelo** | `src/agent/database.py` | `SCHEMA` |
| **Las sentencias prohibidas** | `src/agent/database.py` | `FORBIDDEN_KEYWORDS`, `validate_sql()`, `execute_query()` |
| La interfaz del chat | `app/chat.py` | sección `TAB 1 · CHAT` |
| El modelo de voz | `app/chat.py` | `get_whisper_model()` y `transcribe_audio()` |
| La fórmula del ROI | `src/utils/roi_calculator.py` | `build_roi_dataset()` y `COSTO_HORA_ROBOT_COP` |

Cómo se conectan (cada archivo solo usa los que tiene a su derecha):

```
app/chat.py  →  sql_agent.py  →  prompts.py  →  query_catalog.py  →  database.py
  interfaz        el flujo        los textos      las consultas       la base de datos
```

---

## 4. Los modelos: cuáles son y dónde se consumen

| Modelo | Para qué | Dónde se consume | Dónde corre |
|---|---|---|---|
| **Qwen 2.5 Coder 7B** (LLM) | Escribir el SQL y redactar la respuesta | `_call_model()` en `src/agent/sql_agent.py` | Ollama, local |
| **Whisper-small** | Pasar la voz a texto | `transcribe_audio()` en `app/chat.py` | faster-whisper, local, en CPU |
| **XGBoost** | Predecir ROI (ejercicio del notebook 03) | `src/models/roi_predictor.py` | No lo usa la app: la calculadora es una fórmula |

Por cada pregunta, el LLM se llama **2 veces** (proponer el SQL y explicar el resultado), **3** si
hubo que corregir la consulta, o **1** si era un saludo. No se usa ninguna API externa ni llave: los
datos no salen del equipo.

---

## 5. El prompt: qué le decimos al modelo

Todo el texto que lee el modelo está en `src/agent/prompts.py`. Son tres prompts:

| Prompt | Cuándo se usa | Qué le pide |
|---|---|---|
| **Del sistema** — `build_system_prompt()` | Al inicio de cada pregunta | Quién es, qué datos hay, qué puede hacer y ejemplos |
| `INTERPRETATION_PROMPT` | Después de ejecutar la consulta | "Estos son los resultados, explícalos en español" |
| `RETRY_PROMPT` | Solo si la consulta falló | "SQLite devolvió este error, corrige la consulta" |

El prompt del sistema se arma con **cuatro partes**, siempre en este orden:

| # | Parte | Constante en `prompts.py` | De dónde sale el contenido |
|---|---|---|---|
| 1 | Rol, esquema de la base e instrucciones | `ROLE_AND_INSTRUCTIONS` | El esquema viene de `database.SCHEMA` |
| 2 | Sentencias permitidas y prohibidas | `READ_ONLY_RULES` | La lista viene de `database.FORBIDDEN_KEYWORDS` |
| 3 | Vistas calculadas | `VIEWS_SECTION` | `query_catalog.VIEWS_DESCRIPTION` |
| 4 | Consultas de referencia | `REFERENCE_QUERIES_INTRO` | `query_catalog.REFERENCE_QUERIES` |

Para ver el prompt completo, tal cual lo recibe el modelo (unas 150 líneas):

```bash
uv run python -m src.agent.prompts
```

---

## 6. La base de consultas (la recomendación del profesor)

**La recomendación:** no darle al modelo toda la responsabilidad de construir la consulta, sino
entregarle una base de consultas que pueda usar como guía.

**Por qué hizo falta.** Antes, a la pregunta *"¿cuál es el desarrollador con más soluciones?"* el
modelo respondía con una consulta que buscaba la columna `Desarrollador` en la tabla de
ejecuciones, donde no existe. SQLite devolvía `no such column` y el usuario veía
*"no fue posible responder"*. Con un ejemplo de esa consulta en la base, el modelo acierta al primer intento.

**Dónde está:** `src/agent/query_catalog.py`. Tiene dos partes:

1. **`REFERENCE_QUERIES` — consultas de referencia.** Ocho pares de pregunta y consulta correcta.
   El modelo reutiliza el ejemplo que responde la pregunta (cambiando filtros, orden o `LIMIT`) o lo
   imita para escribir una nueva.

   ```python
   {
       "question": "¿Cuál es el desarrollador con más soluciones?",
       "sql": """SELECT Desarrollador, COUNT(DISTINCT NombreBot) AS Num_Soluciones
   FROM TiemposManuales_clean
   GROUP BY Desarrollador
   ORDER BY Num_Soluciones DESC
   LIMIT 10""",
   },
   ```

2. **`VIEWS` — la vista `ROI_por_bot`.** La fórmula del ROI cruza las tres tablas y ocupa unas 30
   líneas de SQL. En vez de pedirle al modelo que la escriba, la dejamos resuelta como una "tabla
   virtual": el modelo solo escribe `SELECT ... FROM ROI_por_bot`. Usa la misma fórmula que
   `roi_calculator.py`, así el chat y la pestaña **Análisis ROI** dan exactamente las mismas cifras
   (se comprobó bot por bot: 48 bots, diferencia cero).

**Cómo agregar una consulta nueva** (por ejemplo, cuando una pregunta sale mal):

1. Escribe la consulta correcta y pruébala.
2. Agrégala a `REFERENCE_QUERIES` con su pregunta.
3. Valida toda la base contra los datos reales:

   ```bash
   uv run python -m src.agent.query_catalog
   ```

   Debe decir `OK` en todas. Si una falla o sale vacía, no la subas.

**Lo que aprendimos probándolo.** El modelo imita el ejemplo más parecido. Cuando preguntamos
*"¿cuántas ejecuciones tuvo cada tecnología?"* y todavía no había un ejemplo de eso, copió el de
"bots por tecnología" y contó bots en lugar de ejecuciones. Al agregar el ejemplo correcto, acertó.
Por eso la base se alimenta con las preguntas reales que vayan fallando.

---

## 7. Seguridad: dos candados

Una frase para recordar: **el prompt informa, el código hace cumplir.**

En el prompt le decimos al modelo qué sentencias están prohibidas, pero no dependemos de que
obedezca. La protección real está en `execute_query()` (`src/agent/database.py`):

| Candado | Qué hace | Dónde |
|---|---|---|
| **1. Validación del texto** | Solo pasa si es **una** sentencia, empieza por `SELECT` o `WITH`, y no contiene ninguna palabra prohibida | `validate_sql()` |
| **2. Conexión de solo lectura** | La base se abre con `mode=ro`: aunque algo se escapara del candado 1, SQLite se niega a escribir | `execute_query()` |

Sentencias prohibidas (`FORBIDDEN_KEYWORDS`): `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`,
`CREATE`, `TRUNCATE`, `ATTACH`, `DETACH`, `PRAGMA`, `VACUUM`, `REINDEX` y `REPLACE INTO`.

**¿Qué pasa si alguien escribe "borra todos los registros de 2021"?**

1. El modelo, por las reglas del prompt, responde que solo puede consultar (así pasó en la prueba).
2. Si aun así escribiera un `DELETE`, el candado 1 lo bloquea y el usuario ve *"Solo puedo consultar información…"*.
3. Si el candado 1 fallara, el candado 2 hace que SQLite rechace la escritura.

Se probaron 12 sentencias peligrosas (borrados, `DROP`, varias sentencias encadenadas, sentencias
escondidas en comentarios) y todas quedaron bloqueadas.

---

## 8. Cuando la consulta sale mal

Si el SQL del modelo falla al ejecutarse (por ejemplo, una columna que no existe):

1. El agente le muestra al modelo el error exacto de SQLite (`RETRY_PROMPT`).
2. El modelo corrige la consulta. **Solo tiene un intento.**
3. Si vuelve a fallar, el usuario ve: *"Lo siento, no fue posible responder esta duda…"*.

Esto está en `_ask_model_to_fix()`, en `src/agent/sql_agent.py`.

---

## 9. Pruébalo tú mismo

```bash
# Ver el prompt completo que recibe el modelo
uv run python -m src.agent.prompts

# Validar la base de consultas contra los datos reales
uv run python -m src.agent.query_catalog

# Hacerle una pregunta al agente desde la terminal y ver cada pieza
uv run python -c "from src.agent.sql_agent import ask; r = ask('Cuantos bots estan activos?', []); print(r['sql']); print(r['data']); print(r['response'])"

# Abrir la aplicación
uv run streamlit run app/chat.py
```

En la aplicación, cada respuesta trae el desplegable **"Ver SQL ejecutado"**: es la mejor forma de
mostrar en vivo que la respuesta sale de una consulta real y no de la imaginación del modelo.

---

## 10. Límites conocidos

Es mejor decirlos antes de que los pregunten:

- **Es lento sin tarjeta gráfica.** En un portátil sin GPU el modelo genera unos 4 tokens (fragmentos
  de palabra) por segundo: cada respuesta tarda entre 30 segundos y 2 minutos, y la primera pregunta
  es la más lenta (hasta 3 minutos) porque hay que cargar el modelo. La mayor parte del tiempo se va
  en redactar la explicación.
- **La explicación puede equivocarse aunque la tabla esté bien.** El SQL y la tabla son datos reales;
  el texto lo redacta el modelo y a veces calcula mal una proporción. La fuente de verdad es la tabla.
- **El modelo imita los ejemplos.** Una pregunta muy distinta a las de la base puede salir mal: la
  solución es agregar el ejemplo (sección 6).
- **Los datos tienen anomalías.** En algunas áreas hay más errores que transacciones, y el 90 % de las
  ejecuciones no tiene duración registrada. El agente reporta lo que hay; no corrige los datos.
- **La memoria del modelo es limitada.** El prompt del sistema ocupa cerca de la mitad del espacio
  disponible (unos 1.860 de 4.096 tokens); por eso solo se le pasan los últimos 8 mensajes y las
  primeras 30 filas del resultado.

---

## 11. Preguntas que te pueden hacer

**¿Por qué no dejar que el modelo escriba todas las consultas solo?**
Porque se equivoca con el esquema (el caso del desarrollador) y porque no debe decidir qué se
ejecuta. Con la base de consultas acierta más; con los candados, un error no daña nada.

**¿Y si la pregunta no está en la base de consultas?**
El modelo escribe una consulta nueva imitando las tablas, columnas y uniones de los ejemplos. En las
pruebas respondió bien *"¿cuál fue el mes con más ejecuciones en 2024?"*, que no estaba en la base.

**¿Cómo saben que las consultas de referencia están bien?**
Se ejecutan todas contra la base real con `uv run python -m src.agent.query_catalog`, y la vista del
ROI se comparó contra el cálculo de la pestaña de ROI.

**¿Por qué una vista para el ROI y no un ejemplo más?**
Se probaron las dos. Con la fórmula como ejemplo, el modelo tardaba 103 segundos en copiarla; con la
vista escribe una consulta corta en 11 segundos, y no puede equivocarse en la fórmula.

**¿Por qué un modelo local y no una API?**
Los datos son de procesos internos: con Ollama nada sale del computador y no hay costo por uso. El
precio es la velocidad.

**¿Dónde se cambia el modelo?**
En `DEFAULT_MODEL` (`src/agent/sql_agent.py`) o en el selector de la barra lateral. Cualquier modelo
descargado en Ollama aparece ahí.
