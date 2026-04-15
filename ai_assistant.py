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

from kb_manager import get_system_prompt, get_system_prompt_base, get_category_names, get_tramites_data

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
# INYECCION INTELIGENTE DE CONTEXTO
# ============================================================================

def _serializar_tramite(tramite: dict) -> str:
    """Serializa un tramite completo a texto para inyectar en el prompt."""
    lineas = []
    lineas.append(f"[{tramite.get('id', '')}] {tramite.get('nombre', '')}")
    if tramite.get("descripcion"):
        lineas.append(f"Descripcion: {tramite['descripcion']}")
    if tramite.get("quien_puede_solicitarlo"):
        lineas.append(f"Quien puede solicitarlo: {tramite['quien_puede_solicitarlo']}")
    if tramite.get("documentos"):
        lineas.append("Documentos necesarios:")
        for doc in tramite["documentos"]:
            lineas.append(f"  - {doc}")
    if tramite.get("procedimiento"):
        lineas.append(f"Procedimiento: {tramite['procedimiento']}")
    if tramite.get("notas"):
        for nota in tramite["notas"]:
            lineas.append(f"Nota: {nota}")
    return "\n".join(lineas)


def build_context_for_query(query: str) -> str:
    """
    Construye contexto de tramites relevantes para inyectar en el prompt.
    Usa busqueda local por keywords en vez del README completo.
    Reduccion de ~233KB a ~5KB por llamada.
    """
    resultados = buscar_tramite_local(query)

    if not resultados or resultados[0]["score"] == 0:
        # Fallback: listar categorias disponibles
        categorias = get_category_names()
        return (
            "No se encontraron tramites especificos para esta consulta.\n"
            "Categorias disponibles: " + ", ".join(categorias) + ".\n"
            "Pregunta al cliente sobre que tema especifico necesita informacion."
        )

    bloques = []
    for r in resultados[:5]:
        bloques.append(_serializar_tramite(r["tramite"]))

    return "\n\n---\n\n".join(bloques)


def format_conversation_history(messages: list[dict]) -> str:
    """Formatea historial de conversacion para inyectar en el prompt."""
    if not messages:
        return "(Sin historial previo - primera interaccion)"

    lineas = []
    for msg in messages:
        role_label = {
            "client": "CLIENTE",
            "bot": "SUGERENCIA BOT",
            "employee": "EMPLEADO"
        }.get(msg["role"], msg["role"].upper())

        ts = msg.get("timestamp", "")
        if ts:
            try:
                dt = ts.split("T")[-1] if "T" in ts else ts.split(" ")[-1]
                hora = dt[:5]
            except (IndexError, ValueError):
                hora = ""
        else:
            hora = ""

        lineas.append(f"[{role_label} {hora}] {msg['content']}")

    return "\n".join(lineas)


# ============================================================================
# CONSULTA CONVERSACIONAL (WhatsApp)
# ============================================================================

CONVERSATIONAL_PROMPT = """Eres el asistente de ventas de RH Tramites Consulares. Tu trabajo es ayudar al
EMPLEADO a responder mensajes de clientes de WhatsApp.

CONTEXTO: El empleado ve tu sugerencia y decide si copiarla, modificarla, o pedirte
otra version. Tu NO hablas directamente con el cliente. Generas SUGERENCIAS de
respuesta para que el empleado envie.

ESTRATEGIA CONVERSACIONAL:
1. NUNCA des toda la informacion de golpe
2. Haz UNA pregunta de seguimiento para entender mejor la situacion del cliente
3. Adapta la longitud de la respuesta a la complejidad de la pregunta:
   - Preguntas simples o iniciales: respuestas cortas (2-4 lineas)
   - Preguntas sobre documentacion o requisitos especificos: incluye toda la info clave necesaria, aunque sea mas largo
   - Listas de documentos: incluye la lista completa, no recortes info importante
4. Guia la conversacion hacia contratar los servicios de RH Tramites

SERVICIOS QUE OFRECES:
- Asesoria sobre tramites consulares
- Gestion completa de documentacion
- Agendar y gestionar citas en el consulado
- Contacto: WhatsApp +598 91 090 980

TACTICAS DE VENTA:
- Si el cliente pregunta requisitos: da info parcial + "hay mas detalles segun tu
  caso particular, te puedo ayudar con la gestion completa"
- Si pregunta por citas: "nosotros agendamos la cita por vos, nos encargamos de todo"
- Si parece complicado: "estos tramites tienen muchos detalles, con nuestra asesoria
  te aseguras de no perder tiempo ni viajes al consulado"
- Si el cliente ya mostro interes: ofrece agendar una consulta

FORMATO DE TU RESPUESTA:
Genera SOLO el texto que el empleado enviaria al cliente. No incluyas explicaciones,
notas internas, ni etiquetas como "SUGERENCIA:". Escribe como si fueras el empleado
hablando por WhatsApp de manera informal pero profesional.

HISTORIAL DE CONVERSACION:
{history}

INFORMACION RELEVANTE SOBRE TRAMITES:
{context}"""


async def consultar_conversacional(
    client_message: str,
    conversation_history: list[dict],
    employee_context: str = ""
) -> str:
    """
    Genera sugerencia conversacional para el empleado.
    Usa inyeccion inteligente de contexto (solo tramites relevantes).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY no configurada")
        return "Error: La API de IA no esta configurada."

    if not _check_rate_limit():
        return "Limite de consultas alcanzado. Intenta en unos segundos."

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)

        # Build smart context
        tramites_context = build_context_for_query(client_message)
        history_text = format_conversation_history(conversation_history)

        system = CONVERSATIONAL_PROMPT.format(
            history=history_text,
            context=tramites_context
        )

        # If employee gave specific instructions, add them
        user_message = client_message
        if employee_context:
            user_message = (
                f"[INSTRUCCION DEL EMPLEADO: {employee_context}]\n\n"
                f"Ultimo mensaje del cliente: {client_message}"
            )

        log.info(f"Consulta conversacional: {client_message[:80]}... (contexto: {len(tramites_context)} chars)")

        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=system,
            messages=[
                {"role": "user", "content": user_message}
            ]
        )

        respuesta = response.content[0].text.strip()
        log.info(f"Sugerencia generada ({len(respuesta)} chars)")
        return respuesta

    except ImportError:
        log.error("El paquete 'anthropic' no esta instalado")
        return "Error: El modulo de IA no esta instalado correctamente."
    except Exception as e:
        log.error(f"Error en consulta conversacional: {e}")
        return "No pude generar una sugerencia. Intenta de nuevo."


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


# ============================================================================
# PROCESAMIENTO DE MENSAJES NATURALES (mencion @bot)
# ============================================================================

async def procesar_mensaje_natural(mensaje: str, usuario: str) -> dict:
    """
    Procesa un mensaje en lenguaje natural dirigido al bot.
    Detecta la intencion (consulta, guardar info, corregir) y actua.

    Retorna: {"tipo": "respuesta|guardado|correccion|error", "texto": "...", "datos": {...}}
    """
    from kb_manager import agregar_nota, agregar_info, corregir_tramite

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"tipo": "error", "texto": "API de IA no configurada."}

    if not _check_rate_limit():
        return {"tipo": "error", "texto": "Limite de consultas alcanzado. Intenta en unos segundos."}

    try:
        import anthropic
        import json as json_module
        client = anthropic.AsyncAnthropic(api_key=api_key)

        system = get_system_prompt()
        system += """

INSTRUCCIONES ADICIONALES PARA MENSAJES CONVERSACIONALES:

Eres un asistente de Discord. Los empleados te etiquetan y te hablan en lenguaje natural.
Debes detectar que quiere el usuario y responder con un JSON seguido de tu respuesta.

SIEMPRE responde con este formato exacto (la primera linea DEBE ser JSON valido):
{"accion": "consulta|guardar|corregir|clientes|estado", "categoria": "", "tramite_id": "", "nombre": "", "descripcion": "", "nota": "", "campo": "", "valor": ""}

Seguido de una linea vacia y luego tu respuesta en texto natural.

REGLAS:
- Si el usuario PREGUNTA algo sobre tramites → accion: "consulta"
- Si el usuario quiere GUARDAR/AGREGAR/ANOTAR informacion nueva → accion: "guardar", con categoria, nombre/nota
- Si el usuario quiere CORREGIR/CAMBIAR/ACTUALIZAR un tramite → accion: "corregir", con tramite_id, campo, valor
- Si pregunta por clientes pendientes → accion: "clientes"
- Si pregunta por estado del bot → accion: "estado"

EJEMPLOS:
Usuario: "que necesito para renovar el pasaporte?"
{"accion": "consulta", "categoria": "", "tramite_id": "", "nombre": "", "descripcion": "", "nota": "", "campo": "", "valor": ""}

(respuesta sobre pasaportes)

Usuario: "guarda que ahora el consulado pide cedula vigente para pasaportes"
{"accion": "guardar", "categoria": "", "tramite_id": "5.1", "nombre": "", "descripcion": "", "nota": "Desde 2026 el consulado pide cedula vigente para pasaportes", "campo": "", "valor": ""}

Listo! Guarde la nota en el tramite 5.1 (Pasaportes).

Usuario: "corregí la descripcion del tramite 1.1, ahora dice que los dos padres deben firmar"
{"accion": "corregir", "categoria": "", "tramite_id": "1.1", "nombre": "", "descripcion": "", "nota": "", "campo": "descripcion", "valor": "Inscripcion en el Registro Civil espanol. Ambos progenitores deben firmar la hoja declaratoria."}

Listo! Corregi la descripcion del tramite 1.1 (Nacimientos)."""

        log.info(f"Mensaje natural de {usuario}: {mensaje[:80]}")

        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": mensaje}]
        )

        respuesta_completa = response.content[0].text.strip()

        # Parsear la primera linea como JSON
        lineas = respuesta_completa.split("\n", 1)
        primera_linea = lineas[0].strip()
        texto_respuesta = lineas[1].strip() if len(lineas) > 1 else ""

        try:
            datos = json_module.loads(primera_linea)
        except json_module.JSONDecodeError:
            # Si no pudo parsear JSON, es solo una respuesta de texto
            return {"tipo": "respuesta", "texto": respuesta_completa}

        accion = datos.get("accion", "consulta")

        if accion == "guardar":
            # Guardar nota o info nueva
            tramite_id = datos.get("tramite_id", "")
            nota = datos.get("nota", "")
            nombre = datos.get("nombre", "")
            descripcion = datos.get("descripcion", "")
            categoria = datos.get("categoria", "")

            if tramite_id and nota:
                resultado = agregar_nota(tramite_id, nota, usuario)
                if resultado["ok"]:
                    return {"tipo": "guardado", "texto": texto_respuesta or f"Nota guardada en tramite {tramite_id}.", "datos": resultado}
                else:
                    return {"tipo": "error", "texto": f"No pude guardar: {resultado['error']}"}
            elif categoria and nombre and descripcion:
                resultado = agregar_info(categoria, {"nombre": nombre, "descripcion": descripcion}, usuario)
                if resultado["ok"]:
                    return {"tipo": "guardado", "texto": texto_respuesta or f"Tramite agregado con ID {resultado['id']}.", "datos": resultado}
                else:
                    return {"tipo": "error", "texto": f"No pude agregar: {resultado['error']}"}
            elif nota:
                # Si hay nota pero no tramite_id, buscar el tramite mas relevante
                resultados = buscar_tramite_local(nota)
                if resultados:
                    tid = resultados[0]["tramite"]["id"]
                    resultado = agregar_nota(tid, nota, usuario)
                    if resultado["ok"]:
                        return {"tipo": "guardado", "texto": texto_respuesta or f"Nota guardada en tramite {tid} ({resultados[0]['tramite']['nombre']}).", "datos": resultado}
                return {"tipo": "respuesta", "texto": texto_respuesta or "No pude determinar donde guardar esa informacion. Intenta especificar el tramite."}

        elif accion == "corregir":
            tramite_id = datos.get("tramite_id", "")
            campo = datos.get("campo", "")
            valor = datos.get("valor", "")
            if tramite_id and campo and valor:
                resultado = corregir_tramite(tramite_id, {campo: valor}, usuario)
                if resultado["ok"]:
                    return {"tipo": "correccion", "texto": texto_respuesta or f"Tramite {tramite_id} corregido.", "datos": resultado}
                else:
                    return {"tipo": "error", "texto": f"No pude corregir: {resultado['error']}"}
            return {"tipo": "respuesta", "texto": texto_respuesta or "No pude determinar que corregir. Especifica el ID del tramite, campo y nuevo valor."}

        elif accion == "clientes":
            return {"tipo": "clientes", "texto": texto_respuesta}

        elif accion == "estado":
            return {"tipo": "estado", "texto": texto_respuesta}

        else:
            # Consulta normal
            return {"tipo": "respuesta", "texto": texto_respuesta or respuesta_completa}

    except Exception as e:
        log.error(f"Error en mensaje natural: {e}")
        return {"tipo": "error", "texto": "Hubo un error procesando tu mensaje. Intenta de nuevo."}
