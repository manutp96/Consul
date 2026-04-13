"""
Modulo de Asistente IA para Tramites Consulares
================================================

Usa Claude API para responder preguntas sobre tramites consulares
basandose en la base de datos gestionada por kb_manager.

Tambien incluye busqueda local por keywords en tramites_db.json.
"""

import logging
import os
import re
import time
from collections import deque

from kb_manager import get_system_prompt, get_tramites_data

log = logging.getLogger("AI")

# Rate limiting
MAX_QUERIES_PER_MINUTE = int(os.environ.get("AI_RATE_LIMIT", "20"))
_query_timestamps = deque()

# Claude model
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")


# ============================================================================
# BUSQUEDA LOCAL (sin IA)
# ============================================================================

def buscar_tramite_local(query: str) -> list:
    """
    Busca tramites por keywords sin usar IA.
    Retorna lista de dicts con los tramites que matchean.
    """
    data = get_tramites_data()
    query_lower = query.lower().strip()
    query_words = query_lower.split()

    resultados = []

    for categoria in data.get("categorias", []):
        for tramite in categoria.get("tramites", []):
            score = 0
            keywords = tramite.get("keywords", [])
            nombre = tramite.get("nombre", "").lower()

            # Match exacto en nombre
            if query_lower in nombre:
                score += 10

            # Match en keywords
            for kw in keywords:
                if query_lower in kw or kw in query_lower:
                    score += 5
                for word in query_words:
                    if word in kw:
                        score += 2

            # Match en descripcion
            desc = tramite.get("descripcion", "").lower()
            for word in query_words:
                if len(word) > 3 and word in desc:
                    score += 1

            if score > 0:
                resultados.append({
                    "score": score,
                    "categoria": categoria.get("nombre", ""),
                    "tramite": tramite
                })

    # Ordenar por score descendente
    resultados.sort(key=lambda x: x["score"], reverse=True)
    return resultados[:5]  # Top 5


def formatear_resultado_local(resultado: dict) -> str:
    """Formatea un resultado de busqueda local para mostrar."""
    tramite = resultado["tramite"]
    lineas = []
    lineas.append(f"**{tramite['id']} - {tramite['nombre']}**")
    lineas.append(f"*Categoria: {resultado['categoria']}*")
    lineas.append(f"\n{tramite.get('descripcion', '')}")

    if tramite.get("documentos"):
        lineas.append("\n**Documentos necesarios:**")
        for doc in tramite["documentos"]:
            lineas.append(f"- {doc}")

    if tramite.get("procedimiento"):
        lineas.append(f"\n**Procedimiento:** {tramite['procedimiento']}")

    if tramite.get("notas"):
        lineas.append("\n**Notas:**")
        for nota in tramite["notas"]:
            lineas.append(f"- {nota}")

    return "\n".join(lineas)


# ============================================================================
# CONSULTA CON CLAUDE API
# ============================================================================

def _check_rate_limit() -> bool:
    """Verifica rate limit. Retorna True si se puede hacer la consulta."""
    ahora = time.time()
    while _query_timestamps and _query_timestamps[0] < ahora - 60:
        _query_timestamps.popleft()

    if len(_query_timestamps) >= MAX_QUERIES_PER_MINUTE:
        return False

    _query_timestamps.append(ahora)
    return True


async def consultar_tramite(pregunta: str, contexto_extra: str = "") -> str:
    """
    Consulta a Claude API sobre un tramite consular.
    Retorna la respuesta como string.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY no configurada")
        return "Error: La API de IA no esta configurada. Contacta al administrador."

    if not _check_rate_limit():
        return "Se alcanzo el limite de consultas por minuto. Intenta de nuevo en unos segundos."

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)

        # Obtener system prompt actualizado (con correcciones recientes incluidas)
        system = get_system_prompt()
        if contexto_extra:
            system += f"\n\nContexto adicional: {contexto_extra}"

        log.info(f"Consultando Claude API: {pregunta[:80]}...")

        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=system,
            messages=[
                {"role": "user", "content": pregunta}
            ]
        )

        respuesta = response.content[0].text
        log.info(f"Respuesta recibida ({len(respuesta)} chars)")
        return respuesta

    except ImportError:
        log.error("El paquete 'anthropic' no esta instalado")
        return "Error: El modulo de IA no esta instalado correctamente."
    except Exception as e:
        log.error(f"Error consultando Claude API: {e}")
        return "Lo siento, no pude procesar tu consulta. Intenta de nuevo mas tarde."


# ============================================================================
# FORMATEO PARA DIFERENTES PLATAFORMAS
# ============================================================================

def formatear_respuesta_discord(respuesta: str) -> list:
    """
    Formatea la respuesta para Discord.
    Discord tiene limite de 2000 chars por mensaje.
    Retorna lista de strings (chunks).
    """
    if len(respuesta) <= 1900:
        return [respuesta]

    chunks = []
    chunk_actual = ""

    for linea in respuesta.split("\n"):
        if len(chunk_actual) + len(linea) + 1 > 1900:
            if chunk_actual:
                chunks.append(chunk_actual)
            chunk_actual = linea
        else:
            chunk_actual += ("\n" if chunk_actual else "") + linea

    if chunk_actual:
        chunks.append(chunk_actual)

    return chunks


def formatear_respuesta_whatsapp(respuesta: str) -> str:
    """
    Formatea la respuesta para WhatsApp.
    Simplifica markdown para formato WhatsApp.
    """
    resultado = respuesta
    resultado = re.sub(r'\*\*(.+?)\*\*', r'*\1*', resultado)
    resultado = re.sub(r'^#{1,6}\s+', '', resultado, flags=re.MULTILINE)

    if len(resultado) > 4000:
        resultado = resultado[:3950] + "\n\n... (mensaje truncado, consulta mas detalles)"

    return resultado


# ============================================================================
# FUNCION DE CONVENIENCIA
# ============================================================================

async def responder(pregunta: str, plataforma: str = "discord") -> str | list:
    """
    Funcion principal: recibe pregunta, retorna respuesta formateada.
    plataforma: "discord", "whatsapp", "raw"
    """
    resultados_locales = buscar_tramite_local(pregunta)

    if resultados_locales and resultados_locales[0]["score"] >= 10:
        contexto = f"El usuario probablemente pregunta sobre: {resultados_locales[0]['tramite']['nombre']}"
    else:
        contexto = ""

    respuesta = await consultar_tramite(pregunta, contexto)

    if plataforma == "discord":
        return formatear_respuesta_discord(respuesta)
    elif plataforma == "whatsapp":
        return formatear_respuesta_whatsapp(respuesta)
    else:
        return respuesta
