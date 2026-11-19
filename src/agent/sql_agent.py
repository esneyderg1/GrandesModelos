"""
Agente SQL: responde preguntas en español sobre la base de datos de procesos RPA
usando un modelo de lenguaje local (Ollama).

El recorrido de una pregunta está en la función ask() y tiene tres pasos:

  Paso 1 · El modelo lee la pregunta y propone una consulta SQL.
  Paso 2 · El agente valida esa consulta y la ejecuta en modo solo lectura.
           Si falla, le muestra el error al modelo para que la corrija (una vez).
  Paso 3 · El modelo recibe los resultados y los explica en español.

Dónde está cada cosa:
  - Los textos que lee el modelo (prompts)      → prompts.py
  - Las consultas de referencia y la vista ROI  → query_catalog.py
  - El esquema, la validación y la ejecución    → database.py
  - La llamada al modelo                        → _call_model(), en este archivo
"""

import logging
import re

import ollama
import pandas as pd

from .database import ForbiddenSQLError, execute_query
from .prompts import INTERPRETATION_PROMPT, RETRY_PROMPT, build_system_prompt
from .query_catalog import VIEWS

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen2.5-coder:7b"
MAX_HISTORY_MESSAGES = 8         # mensajes anteriores de la conversación que ve el modelo
MAX_RESULT_ROWS_IN_PROMPT = 30   # filas del resultado que se le pasan para interpretar

# Mensajes para el usuario cuando no se puede responder.
DEFAULT_FALLBACK_MESSAGE = (
    "Lo siento, no fue posible responder esta duda. "
    "Intenta reformular la pregunta o consultar otra información."
)
FORBIDDEN_MESSAGE = (
    "Solo puedo consultar información: no ejecuto sentencias que modifiquen, "
    "creen o eliminen datos. Reformula la pregunta como una consulta."
)
OLLAMA_DOWN_MESSAGE = (
    "No se puede conectar a Ollama. "
    "Asegúrate de tener Ollama instalado y corriendo:\n\n"
    "```\nollama serve\n```"
)


# ─────────────────────────────────────────────────────────────────────────────
# Consumo del modelo
# ─────────────────────────────────────────────────────────────────────────────

def _call_model(model: str, messages: list[dict]) -> str:
    """
    ÚNICO punto donde el agente consume el modelo de lenguaje.

    Envía la conversación a Ollama (que corre en este mismo equipo) y retorna
    el texto que responde el modelo.
    """
    return ollama.chat(model=model, messages=messages)["message"]["content"]


def list_available_models() -> list[str]:
    """Retorna los modelos Ollama disponibles localmente."""
    try:
        models = ollama.list()
        return [m["model"] for m in models.get("models", [])]
    except Exception as e:
        logger.info("No se pudo listar modelos de Ollama: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# El recorrido de una pregunta
# ─────────────────────────────────────────────────────────────────────────────

def ask(question: str, history: list[dict], model: str = DEFAULT_MODEL) -> dict:
    """
    Procesa una pregunta del usuario y retorna un diccionario con:
      - response (str): respuesta en lenguaje natural
      - sql (str | None): SQL generado y ejecutado
      - data (list[dict] | None): filas resultado de la query
      - columns (list[str] | None): nombres de columnas
      - error (str | None): mensaje de error si ocurrió
    """
    messages = _build_messages(question, history)

    # ── Paso 1 · El modelo propone una consulta SQL ──────────────────────────
    try:
        model_reply = _call_model(model, messages)
    except Exception as e:
        return _answer(_model_error_message(e), error=str(e))

    sql = _extract_sql(model_reply)
    if not sql:
        # Saludo o pregunta conceptual: el modelo respondió sin consultar datos.
        return _answer(model_reply)

    # ── Paso 2 · Validar y ejecutar la consulta ──────────────────────────────
    try:
        df = execute_query(sql, VIEWS)
    except ForbiddenSQLError as e:
        # No era un SELECT (p. ej. DELETE): no se ejecuta ni se reintenta.
        logger.warning("SQL bloqueado: %s\nSQL: %s", e, sql)
        return _answer(FORBIDDEN_MESSAGE, error=str(e))
    except Exception as e:
        # SQL inválido (p. ej. una columna que no existe): una oportunidad de corregir.
        logger.warning("SQL execution failed: %s\nSQL: %s", e, sql)
        corrected = _ask_model_to_fix(model, messages, model_reply, e)
        if corrected is None:
            return _answer(DEFAULT_FALLBACK_MESSAGE, error=str(e))
        model_reply, sql, df = corrected

    # ── Paso 3 · El modelo explica el resultado en español ───────────────────
    interpretation_messages = messages + [
        {"role": "assistant", "content": model_reply},
        {"role": "user", "content": INTERPRETATION_PROMPT.format(results=_format_df_for_prompt(df))},
    ]
    try:
        response = _call_model(model, interpretation_messages)
    except Exception:
        response = f"Consulta ejecutada. Resultados con {len(df)} filas."

    return _answer(response, sql=sql, df=df)


# ─────────────────────────────────────────────────────────────────────────────
# Funciones de apoyo de ask()
# ─────────────────────────────────────────────────────────────────────────────

def _build_messages(question: str, history: list[dict]) -> list[dict]:
    """Conversación que recibe el modelo: prompt del sistema + historial reciente + pregunta."""
    messages = [{"role": "system", "content": build_system_prompt()}]
    for msg in history[-MAX_HISTORY_MESSAGES:]:
        if msg["role"] in ("user", "assistant"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": question})
    return messages


def _extract_sql(text: str) -> str | None:
    """Saca la consulta del bloque ```sql ... ``` que escribe el modelo (None si no hay)."""
    match = re.search(r"```sql\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # Bloque sin la etiqueta "sql". También se captura un INSERT/UPDATE para que
    # llegue al validador y el usuario reciba el mensaje de "solo lectura".
    match = re.search(r"```\s*(SELECT|WITH|INSERT|UPDATE)\b.*?```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(0).strip("`").strip()
    return None


def _ask_model_to_fix(
    model: str, messages: list[dict], model_reply: str, error: Exception
) -> tuple[str, str, pd.DataFrame] | None:
    """
    Le muestra al modelo el error de SQLite para que corrija la consulta (un solo
    reintento). Retorna (respuesta del modelo, sql, resultado) o None si tampoco funciona.
    """
    retry_messages = messages + [
        {"role": "assistant", "content": model_reply},
        {"role": "user", "content": RETRY_PROMPT.format(error=error.__cause__ or error)},
    ]
    try:
        new_reply = _call_model(model, retry_messages)
        new_sql = _extract_sql(new_reply)
        if not new_sql:
            return None
        return new_reply, new_sql, execute_query(new_sql, VIEWS)
    except Exception as e:
        logger.warning("SQL retry failed: %s", e)
        return None


def _format_df_for_prompt(df: pd.DataFrame | None) -> str:
    """Convierte el resultado en texto (columnas separadas por |) para que el modelo lo lea."""
    if df is None or df.empty:
        return "La consulta no retornó resultados."
    preview = df.head(MAX_RESULT_ROWS_IN_PROMPT)
    lines = [" | ".join(_format_value(v) for v in row) for row in preview.itertuples(index=False)]
    header = " | ".join(df.columns)
    return header + "\n" + "\n".join(lines)


def _format_value(value) -> str:
    """Decimales grandes con separador de miles: evita que el modelo lea 186756.0 como 186.76."""
    if isinstance(value, float) and abs(value) >= 1000 and value not in (float("inf"), float("-inf")):
        return f"{value:,.0f}"
    return str(value)


def _model_error_message(error: Exception) -> str:
    """Mensaje para el usuario cuando falla la llamada al modelo."""
    if any(k in str(error).lower() for k in ("refused", "connect", "connection")):
        return OLLAMA_DOWN_MESSAGE
    return DEFAULT_FALLBACK_MESSAGE


def _answer(
    response: str, sql: str | None = None, df: pd.DataFrame | None = None, error: str | None = None
) -> dict:
    """Arma el diccionario que ask() le retorna a la interfaz."""
    return {
        "response": response,
        "sql": sql,
        "data": None if df is None else df.to_dict("records"),
        "columns": None if df is None else list(df.columns),
        "error": error,
    }
