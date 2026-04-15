"""
API Server para RH Tramites Consulares
=======================================

FastAPI con webhooks para n8n/WhatsApp y endpoints de consulta.
"""

import logging
import os
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ai_assistant import responder, buscar_tramite_local, consultar_tramite, consultar_conversacional
from kb_manager import (
    invalidar_cache, obtener_feedback_pendiente, marcar_feedback,
    corregir_tramite as kb_corregir_tramite, agregar_nota as kb_agregar_nota
)

log = logging.getLogger("API")

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "")

# ============================================================================
# MODELOS PYDANTIC
# ============================================================================


class ConsultaRequest(BaseModel):
    pregunta: str
    plataforma: str = "raw"  # "discord", "whatsapp", "raw"


class ConsultaResponse(BaseModel):
    respuesta: str
    fuente: str  # "claude" o "local"
    timestamp: str


class WebhookN8NRequest(BaseModel):
    action: str  # "consulta", "estado", "clientes"
    query: str = ""
    remoteJid: str = ""  # Numero de WhatsApp del remitente


# ============================================================================
# APP FASTAPI
# ============================================================================

app = FastAPI(
    title="RH Tramites Consulares API",
    description="API para integracion con n8n, WhatsApp y Discord",
    version="1.0.0"
)

# Referencia al estado del CitaBot (se setea desde main.py)
_cita_bot_estado = {}


def set_cita_bot_estado(estado: dict):
    """Llamado desde main.py para compartir el estado del CitaBot."""
    global _cita_bot_estado
    _cita_bot_estado = estado


# ============================================================================
# AUTENTICACION
# ============================================================================

async def verificar_api_key(request: Request):
    """Verifica el API key en el header X-API-Key."""
    if not API_SECRET_KEY:
        return  # Sin key configurada, acceso libre (dev mode)

    api_key = request.headers.get("X-API-Key", "")
    if api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="API key invalida")


# ============================================================================
# ENDPOINTS
# ============================================================================

WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "rh_tramites_verify_2024")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")

# Canales de WhatsApp en Discord (multi-canal)
DISCORD_WA_CHANNELS = []
for key in ["DISCORD_WA_CHANNEL_1", "DISCORD_WA_CHANNEL_2", "DISCORD_WA_CHANNEL_3"]:
    val = os.environ.get(key, "")
    if val:
        DISCORD_WA_CHANNELS.append(int(val))

# Backward compat: si no hay canales nuevos, usar el viejo
if not DISCORD_WA_CHANNELS:
    old_channel = int(os.environ.get("DISCORD_WHATSAPP_CHANNEL_ID", "0"))
    if old_channel:
        DISCORD_WA_CHANNELS.append(old_channel)

DISCORD_WHATSAPP_CHANNEL_ID = DISCORD_WA_CHANNELS[0] if DISCORD_WA_CHANNELS else 0

# Referencia al Discord bot (se setea desde main.py)
_discord_bot = None


def set_discord_bot(bot):
    """Llamado desde main.py para compartir el Discord bot."""
    global _discord_bot
    _discord_bot = bot


@app.get("/health")
async def health():
    """Health check para Railway."""
    return {
        "status": "ok",
        "service": "RH Tramites Consulares API",
        "timestamp": datetime.now().isoformat()
    }


# ============================================================================
# WHATSAPP CLOUD API (Meta)
# ============================================================================

@app.get("/webhook/meta")
async def meta_webhook_verify(request: Request):
    """Verificacion de webhook de Meta (GET). Meta envia esto al configurar el webhook."""
    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        log.info(f"Webhook Meta verificado OK")
        return int(challenge)
    else:
        log.warning(f"Webhook Meta verificacion fallida: mode={mode}, token={token}")
        raise HTTPException(status_code=403, detail="Verificacion fallida")


@app.post("/webhook/meta")
async def meta_webhook_receive(request: Request):
    """
    Recibe mensajes de WhatsApp via Meta Cloud API.
    Flujo: guardar en SQLite -> generar sugerencia conversacional -> reenviar a Discord.
    Los empleados responden al cliente desde Discord.
    """
    try:
        payload = await request.json()

        # Extraer mensaje
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return {"status": "no_message"}

        msg = messages[0]
        texto = msg.get("text", {}).get("body", "")
        sender = msg.get("from", "")
        msg_type = msg.get("type", "text")

        # Obtener nombre del contacto si esta disponible
        contacts = value.get("contacts", [])
        sender_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

        if not texto:
            texto = f"[Mensaje de tipo: {msg_type}]"

        log.info(f"WhatsApp de {sender_name or sender}: {texto[:80]}")

        # 1. Obtener o crear conversacion
        import conversation_db
        conv = await conversation_db.get_or_create_conversation(sender, sender_name)
        conv_id = conv["id"]
        channel_id = conv["discord_channel_id"]

        # 2. Si es conversacion nueva (channel_id=0), crear canal de Discord
        if channel_id == 0 and _discord_bot:
            channel_id = await _discord_bot.get_or_create_client_channel(sender, sender_name)
            await conversation_db.assign_channel(conv_id, channel_id)

        # 2. Guardar mensaje del cliente
        await conversation_db.add_message(conv_id, "client", texto)

        # 3. Obtener historial reciente
        history = await conversation_db.get_recent_messages(conv_id, limit=20)

        # 4. Generar sugerencia conversacional (con contexto inteligente)
        sugerencia = ""
        try:
            sugerencia = await consultar_conversacional(
                client_message=texto,
                conversation_history=history,
            )
        except Exception as e:
            log.error(f"Error generando sugerencia conversacional: {e}")
            sugerencia = "No se pudo generar sugerencia."

        # 5. Guardar sugerencia del bot
        await conversation_db.add_message(conv_id, "bot", sugerencia)

        # 6. Reenviar a Discord (canal asignado)
        if _discord_bot and channel_id:
            try:
                await _discord_bot.enviar_mensaje_whatsapp({
                    "sender": sender,
                    "sender_name": sender_name,
                    "texto": texto,
                    "sugerencia": sugerencia,
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                }, channel_id=channel_id)
                log.info(f"Mensaje reenviado a Discord canal {channel_id}")
            except Exception as e:
                log.error(f"Error reenviando a Discord: {e}")
        else:
            log.warning("Discord bot o canales WhatsApp no configurados")

        return {"status": "ok", "sender": sender, "channel": channel_id, "forwarded_to_discord": True}

    except Exception as e:
        log.error(f"Error en webhook Meta: {e}")
        return {"status": "error", "detail": str(e)}


@app.post("/api/consulta", response_model=ConsultaResponse)
async def api_consulta(body: ConsultaRequest, _=Depends(verificar_api_key)):
    """
    Consulta sobre tramites consulares usando Claude AI.
    """
    log.info(f"Consulta API: {body.pregunta[:80]}")

    try:
        respuesta = await responder(body.pregunta, plataforma=body.plataforma)

        # Si es lista (discord), unir en un string
        if isinstance(respuesta, list):
            respuesta = "\n".join(respuesta)

        return ConsultaResponse(
            respuesta=respuesta,
            fuente="claude",
            timestamp=datetime.now().isoformat()
        )
    except Exception as e:
        log.error(f"Error en consulta: {e}")
        raise HTTPException(status_code=500, detail="Error procesando consulta")


@app.get("/api/clientes")
async def api_clientes(_=Depends(verificar_api_key)):
    """
    Retorna la lista de clientes pendientes de cita.
    """
    try:
        from cita_bot_playwright import leer_clientes_google_sheets
        clientes = leer_clientes_google_sheets()

        return {
            "total": len(clientes),
            "clientes": [
                {
                    "nombre": c.nombre,
                    "email": c.email,
                    "movil": c.movil,
                    "tramite": c.tramite,
                    "fila": c.fila
                }
                for c in clientes
            ]
        }
    except Exception as e:
        log.error(f"Error obteniendo clientes: {e}")
        raise HTTPException(status_code=500, detail="Error obteniendo clientes")


@app.get("/api/estado")
async def api_estado(_=Depends(verificar_api_key)):
    """
    Retorna el estado actual del bot de citas.
    """
    return {
        "bot": _cita_bot_estado,
        "timestamp": datetime.now().isoformat()
    }


# ============================================================================
# WEBHOOK PARA N8N (WhatsApp via Evolution API)
# ============================================================================

@app.post("/webhook/n8n")
async def webhook_n8n(body: WebhookN8NRequest, _=Depends(verificar_api_key)):
    """
    Recibe webhooks de n8n.

    Flujo: WhatsApp -> Evolution API -> n8n -> este endpoint -> respuesta -> n8n -> Evolution API -> WhatsApp

    Actions:
    - "consulta": Consulta sobre tramites (query = pregunta del usuario)
    - "estado": Devuelve estado del bot
    - "clientes": Devuelve clientes pendientes
    """
    log.info(f"Webhook n8n: action={body.action}, query={body.query[:50] if body.query else 'N/A'}")

    try:
        if body.action == "consulta":
            if not body.query:
                return {"respuesta": "No recibí ninguna pregunta. ¿En qué puedo ayudarte?"}

            respuesta = await responder(body.query, plataforma="whatsapp")

            return {
                "respuesta": respuesta,
                "remoteJid": body.remoteJid,
                "timestamp": datetime.now().isoformat()
            }

        elif body.action == "estado":
            estado = _cita_bot_estado
            activo = "Activo" if estado.get("activo") else "Inactivo"
            ciclo = estado.get("ciclo", 0)
            ultimo = estado.get("ultimo_intento", "N/A")

            return {
                "respuesta": f"*Bot de Citas*\nEstado: {activo}\nCiclo: {ciclo}\nUltimo intento: {ultimo}",
                "remoteJid": body.remoteJid
            }

        elif body.action == "clientes":
            try:
                from cita_bot_playwright import leer_clientes_google_sheets
                clientes = leer_clientes_google_sheets()

                if not clientes:
                    texto = "No hay clientes pendientes."
                else:
                    lineas = [f"*Clientes pendientes ({len(clientes)}):*"]
                    for i, c in enumerate(clientes[:5], 1):
                        lineas.append(f"{i}. {c.nombre} - {c.tramite}")
                    if len(clientes) > 5:
                        lineas.append(f"... y {len(clientes) - 5} más")
                    texto = "\n".join(lineas)

                return {
                    "respuesta": texto,
                    "remoteJid": body.remoteJid
                }
            except Exception as e:
                return {"respuesta": f"Error obteniendo clientes: {str(e)}"}

        elif body.action == "revisar_feedback":
            # IA revisa feedback pendiente y auto-aplica correcciones validas
            pendientes = obtener_feedback_pendiente()

            if not pendientes:
                return {"respuesta": "No hay feedback pendiente", "total": 0}

            resultados = []
            for fb in pendientes[:10]:  # Max 10 por llamada
                try:
                    prompt = f"""Analiza este feedback de un usuario sobre una respuesta del bot de tramites consulares.

Pregunta original: {fb.get('pregunta', '')}
Respuesta del bot: {fb.get('respuesta', '')[:300]}
Correccion del usuario: {fb.get('correccion', '')}

Decide que hacer. Responde UNICAMENTE con un JSON valido (sin markdown, sin explicacion):

Si la correccion es valida y especifica:
{{"accion": "aplicar", "tramite_id": "X.Y", "campo": "descripcion|procedimiento|notas", "valor": "texto corregido"}}

Si la correccion no es clara o necesita revision humana:
{{"accion": "revisar", "razon": "explicacion breve"}}

Si la correccion es incorrecta o spam:
{{"accion": "descartar", "razon": "explicacion breve"}}"""

                    respuesta_ia = await consultar_tramite(prompt, "")

                    # Parsear JSON de la respuesta
                    import json as json_module
                    # Limpiar respuesta (a veces Claude agrega texto)
                    respuesta_limpia = respuesta_ia.strip()
                    if respuesta_limpia.startswith("```"):
                        respuesta_limpia = respuesta_limpia.split("```")[1]
                        if respuesta_limpia.startswith("json"):
                            respuesta_limpia = respuesta_limpia[4:]
                    respuesta_limpia = respuesta_limpia.strip()

                    decision = json_module.loads(respuesta_limpia)
                    accion = decision.get("accion", "revisar")

                    if accion == "aplicar":
                        tid = decision.get("tramite_id", "")
                        campo = decision.get("campo", "")
                        valor = decision.get("valor", "")

                        if campo == "notas":
                            kb_agregar_nota(tid, valor, f"auto-correccion (feedback {fb['id']})")
                        elif tid and campo and valor:
                            kb_corregir_tramite(tid, {campo: valor}, f"auto-correccion (feedback {fb['id']})")

                        marcar_feedback(fb["id"], "aplicado")
                        resultados.append({"id": fb["id"], "accion": "aplicado", "tramite": tid})

                    elif accion == "descartar":
                        marcar_feedback(fb["id"], "descartado")
                        resultados.append({"id": fb["id"], "accion": "descartado", "razon": decision.get("razon", "")})

                    else:
                        resultados.append({"id": fb["id"], "accion": "pendiente", "razon": decision.get("razon", "necesita revision")})

                except Exception as e:
                    log.error(f"Error revisando feedback {fb.get('id')}: {e}")
                    resultados.append({"id": fb.get("id"), "accion": "error", "error": str(e)})

            return {
                "respuesta": f"Revision completada: {len(resultados)} feedbacks procesados",
                "resultados": resultados,
                "total": len(pendientes)
            }

        elif body.action == "actualizar_tramite":
            # n8n puede pushear cambios directamente
            try:
                import json as json_module
                datos = json_module.loads(body.query)
                tid = datos.get("tramite_id", "")
                campo = datos.get("campo", "")
                valor = datos.get("valor", "")

                if not tid or not campo or not valor:
                    return {"respuesta": "Faltan campos: tramite_id, campo, valor"}

                resultado = kb_corregir_tramite(tid, {campo: valor}, "n8n")
                return {
                    "respuesta": f"Tramite {tid} actualizado" if resultado["ok"] else resultado["error"],
                    "resultado": resultado
                }
            except Exception as e:
                return {"respuesta": f"Error: {str(e)}"}

        elif body.action == "recargar_db":
            invalidar_cache()
            return {"respuesta": "Cache invalidado exitosamente. La proxima consulta usara datos frescos."}

        else:
            return {"respuesta": f"Accion desconocida: {body.action}"}

    except Exception as e:
        log.error(f"Error en webhook n8n: {e}")
        return {
            "respuesta": "Hubo un error procesando tu mensaje. Intenta de nuevo.",
            "remoteJid": body.remoteJid
        }


# ============================================================================
# WEBHOOK GENERICO PARA EVOLUTION API (directo, sin n8n)
# ============================================================================

@app.post("/webhook/whatsapp")
async def webhook_whatsapp(request: Request, _=Depends(verificar_api_key)):
    """
    Webhook directo de Evolution API (sin pasar por n8n).
    Util si se quiere conectar Evolution API directamente.

    Payload de Evolution API:
    {
        "data": {
            "key": {"remoteJid": "598XXXXXXXXX@s.whatsapp.net"},
            "message": {"conversation": "texto del mensaje"}
        }
    }
    """
    try:
        payload = await request.json()
        data = payload.get("data", {})

        # Extraer mensaje
        message = data.get("message", {})
        texto = (
            message.get("conversation", "") or
            message.get("extendedTextMessage", {}).get("text", "")
        )
        remote_jid = data.get("key", {}).get("remoteJid", "")

        if not texto:
            return {"status": "ignored", "reason": "no text message"}

        log.info(f"WhatsApp de {remote_jid}: {texto[:80]}")

        # Responder con IA
        respuesta = await responder(texto, plataforma="whatsapp")

        return {
            "respuesta": respuesta,
            "remoteJid": remote_jid,
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        log.error(f"Error en webhook WhatsApp: {e}")
        return {"status": "error", "detail": str(e)}


# ============================================================================
# BUSQUEDA RAPIDA (sin IA)
# ============================================================================

@app.get("/api/buscar/{query}")
async def api_buscar(query: str):
    """Busqueda rapida de tramites por keyword (sin usar IA)."""
    resultados = buscar_tramite_local(query)

    return {
        "query": query,
        "total": len(resultados),
        "resultados": [
            {
                "id": r["tramite"]["id"],
                "nombre": r["tramite"]["nombre"],
                "categoria": r["categoria"],
                "descripcion": r["tramite"].get("descripcion", ""),
                "score": r["score"]
            }
            for r in resultados
        ]
    }
