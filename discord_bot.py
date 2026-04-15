"""
Bot de Discord para RH Tramites Consulares
===========================================

Slash commands para consultar tramites, ver clientes y estado del bot.
Comandos de gestion para agregar/corregir info en la base de datos.
Sistema de feedback con reacciones para auto-correccion.
"""

import asyncio
import logging
import os
import queue
from datetime import datetime, timedelta

import discord
from discord import app_commands

from ai_assistant import responder, buscar_tramite_local, formatear_resultado_local, procesar_mensaje_natural, formatear_respuesta_discord, consultar_conversacional
from kb_manager import agregar_info, corregir_tramite, agregar_nota, guardar_feedback

log = logging.getLogger("Discord")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")

# Multi-canal WhatsApp
DISCORD_WA_CHANNELS = []
for _key in ["DISCORD_WA_CHANNEL_1", "DISCORD_WA_CHANNEL_2", "DISCORD_WA_CHANNEL_3"]:
    _val = os.environ.get(_key, "")
    if _val:
        DISCORD_WA_CHANNELS.append(int(_val))
if not DISCORD_WA_CHANNELS:
    _old = int(os.environ.get("DISCORD_WHATSAPP_CHANNEL_ID", "0"))
    if _old:
        DISCORD_WA_CHANNELS.append(_old)
DISCORD_WHATSAPP_CHANNEL_ID = DISCORD_WA_CHANNELS[0] if DISCORD_WA_CHANNELS else 0


# ============================================================================
# BOT DE DISCORD
# ============================================================================

class ConsularBot(discord.Client):
    def __init__(self, notification_queue: queue.Queue = None, cita_bot_estado: dict = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.notification_queue = notification_queue
        self.cita_bot_estado = cita_bot_estado or {}
        self._pending_feedback = {}  # message_id -> {pregunta, respuesta, usuario, timestamp, estado}
        self._whatsapp_messages = {}  # message_id -> {sender: "598XXXXXXX", sender_name: "...", timestamp: datetime}
        self._setup_commands()

    def _setup_commands(self):
        """Registra los slash commands."""

        # ==============================================================
        # CONSULTAS
        # ==============================================================

        @self.tree.command(
            name="tramite",
            description="Consultar informacion sobre un tramite consular"
        )
        @app_commands.describe(consulta="Que tramite necesitas? Ej: pasaporte, nacimiento, nacionalidad")
        async def cmd_tramite(interaction: discord.Interaction, consulta: str):
            await interaction.response.defer(thinking=True)
            log.info(f"Consulta de {interaction.user}: {consulta}")

            try:
                respuesta_chunks = await responder(consulta, plataforma="discord")

                if isinstance(respuesta_chunks, list):
                    msg = await interaction.followup.send(respuesta_chunks[0], wait=True)
                    for chunk in respuesta_chunks[1:]:
                        await interaction.channel.send(chunk)
                else:
                    msg = await interaction.followup.send(respuesta_chunks, wait=True)

                # Agregar reacciones de feedback
                try:
                    await msg.add_reaction("\U0001f44d")  # 👍
                    await msg.add_reaction("\U0001f44e")  # 👎
                    self._pending_feedback[msg.id] = {
                        "pregunta": consulta,
                        "respuesta": respuesta_chunks[0] if isinstance(respuesta_chunks, list) else respuesta_chunks,
                        "usuario": str(interaction.user),
                        "timestamp": datetime.now(),
                        "estado": "esperando_reaccion"
                    }
                except Exception as e:
                    log.warning(f"No se pudieron agregar reacciones: {e}")

            except Exception as e:
                log.error(f"Error en /tramite: {e}")
                await interaction.followup.send(
                    "Hubo un error procesando tu consulta. Intenta de nuevo."
                )

        @self.tree.command(
            name="buscar",
            description="Busqueda rapida de tramite (sin IA)"
        )
        @app_commands.describe(query="Palabra clave: pasaporte, nacimiento, divorcio, etc.")
        async def cmd_buscar(interaction: discord.Interaction, query: str):
            log.info(f"Busqueda local de {interaction.user}: {query}")

            resultados = buscar_tramite_local(query)

            if not resultados:
                await interaction.response.send_message(
                    f"No encontre tramites para '{query}'. "
                    f"Proba con `/tramite {query}` para una busqueda con IA."
                )
                return

            embed = discord.Embed(
                title=f"Resultados para: {query}",
                color=discord.Color.blue()
            )

            for r in resultados[:3]:
                tramite = r["tramite"]
                desc = tramite.get("descripcion", "")[:200]
                if len(tramite.get("descripcion", "")) > 200:
                    desc += "..."
                embed.add_field(
                    name=f"{tramite['id']} - {tramite['nombre']}",
                    value=f"*{r['categoria']}*\n{desc}",
                    inline=False
                )

            embed.set_footer(text="Usa /tramite para una respuesta detallada con IA")
            await interaction.response.send_message(embed=embed)

        # ==============================================================
        # GESTION DE BASE DE DATOS
        # ==============================================================

        @self.tree.command(
            name="agregar",
            description="Agregar un nuevo tramite a la base de datos"
        )
        @app_commands.describe(
            categoria="Categoria: Familia, Certificados, Nacionalidad, Pasaportes, Notaria, Visados, etc.",
            nombre="Nombre del tramite",
            descripcion="Descripcion del tramite"
        )
        async def cmd_agregar(interaction: discord.Interaction, categoria: str, nombre: str, descripcion: str):
            log.info(f"Agregar por {interaction.user}: {categoria} / {nombre}")

            resultado = agregar_info(
                categoria_nombre=categoria,
                info={"nombre": nombre, "descripcion": descripcion},
                usuario=str(interaction.user)
            )

            if resultado["ok"]:
                embed = discord.Embed(
                    title="Tramite Agregado",
                    color=discord.Color.green()
                )
                embed.add_field(name="ID", value=resultado["id"], inline=True)
                embed.add_field(name="Nombre", value=nombre, inline=True)
                embed.add_field(name="Categoria", value=categoria, inline=True)
                embed.add_field(name="Descripcion", value=descripcion[:200], inline=False)
                embed.set_footer(text=f"Por {interaction.user}")
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message(
                    f"Error: {resultado['error']}", ephemeral=True
                )

        @self.tree.command(
            name="corregir",
            description="Corregir informacion de un tramite existente"
        )
        @app_commands.describe(
            tramite_id="ID del tramite. Ej: 1.1, 5.1, 4.2",
            campo="Campo a corregir",
            valor="Nuevo valor"
        )
        @app_commands.choices(campo=[
            app_commands.Choice(name="Descripcion", value="descripcion"),
            app_commands.Choice(name="Procedimiento", value="procedimiento"),
            app_commands.Choice(name="Quien puede solicitarlo", value="quien_puede_solicitarlo"),
        ])
        async def cmd_corregir(interaction: discord.Interaction, tramite_id: str, campo: app_commands.Choice[str], valor: str):
            log.info(f"Corregir por {interaction.user}: {tramite_id}.{campo.value}")

            resultado = corregir_tramite(
                tramite_id=tramite_id,
                correccion={campo.value: valor},
                usuario=str(interaction.user)
            )

            if resultado["ok"]:
                embed = discord.Embed(
                    title="Tramite Corregido",
                    color=discord.Color.orange()
                )
                embed.add_field(name="Tramite", value=tramite_id, inline=True)
                embed.add_field(name="Campo", value=campo.name, inline=True)
                embed.add_field(name="Nuevo valor", value=valor[:200], inline=False)
                embed.set_footer(text=f"Por {interaction.user}")
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message(
                    f"Error: {resultado['error']}", ephemeral=True
                )

        @self.tree.command(
            name="notas",
            description="Agregar una nota a un tramite"
        )
        @app_commands.describe(
            tramite_id="ID del tramite. Ej: 1.1, 5.1, 4.2",
            nota="Nota o informacion adicional"
        )
        async def cmd_notas(interaction: discord.Interaction, tramite_id: str, nota: str):
            log.info(f"Nota por {interaction.user}: {tramite_id}")

            resultado = agregar_nota(
                tramite_id=tramite_id,
                nota=nota,
                usuario=str(interaction.user)
            )

            if resultado["ok"]:
                embed = discord.Embed(
                    title="Nota Agregada",
                    color=discord.Color.teal()
                )
                embed.add_field(name="Tramite", value=tramite_id, inline=True)
                embed.add_field(name="Nota", value=nota[:200], inline=False)
                embed.set_footer(text=f"Por {interaction.user}")
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message(
                    f"Error: {resultado['error']}", ephemeral=True
                )

        # ==============================================================
        # MONITOREO
        # ==============================================================

        @self.tree.command(
            name="clientes",
            description="Ver clientes pendientes de cita"
        )
        async def cmd_clientes(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            log.info(f"Consulta de clientes por {interaction.user}")

            try:
                from cita_bot_playwright import leer_clientes_google_sheets
                clientes = leer_clientes_google_sheets()

                if not clientes:
                    await interaction.followup.send(
                        "No hay clientes pendientes en la hoja de Google Sheets."
                    )
                    return

                embed = discord.Embed(
                    title=f"Clientes Pendientes ({len(clientes)})",
                    color=discord.Color.orange()
                )

                for i, c in enumerate(clientes[:10], 1):
                    embed.add_field(
                        name=f"{i}. {c.nombre}",
                        value=f"Email: {c.email}\nTramite: {c.tramite}",
                        inline=False
                    )

                if len(clientes) > 10:
                    embed.set_footer(text=f"... y {len(clientes) - 10} mas")

                await interaction.followup.send(embed=embed)

            except Exception as e:
                log.error(f"Error en /clientes: {e}")
                await interaction.followup.send("Error al obtener la lista de clientes.")

        @self.tree.command(
            name="estado",
            description="Estado actual del bot de citas"
        )
        async def cmd_estado(interaction: discord.Interaction):
            log.info(f"Consulta de estado por {interaction.user}")

            estado = self.cita_bot_estado

            embed = discord.Embed(
                title="Estado del Bot de Citas",
                color=discord.Color.green() if estado.get("activo") else discord.Color.red()
            )
            embed.add_field(name="Estado", value="Activo" if estado.get("activo") else "Inactivo", inline=True)
            embed.add_field(name="Ciclo actual", value=str(estado.get("ciclo", 0)), inline=True)
            embed.add_field(name="Ultimo intento", value=estado.get("ultimo_intento", "N/A"), inline=True)
            embed.add_field(name="Ultima reserva", value=estado.get("ultima_reserva", "Ninguna aun"), inline=True)
            embed.add_field(name="Servicio", value=estado.get("servicio", "Registro Civil"), inline=True)

            await interaction.response.send_message(embed=embed)

        # ==============================================================
        # AYUDA
        # ==============================================================

        @self.tree.command(
            name="ayuda",
            description="Mostrar comandos disponibles"
        )
        async def cmd_ayuda(interaction: discord.Interaction):
            embed = discord.Embed(
                title="RH Tramites Consulares - Bot de Discord",
                description="Comandos disponibles:",
                color=discord.Color.gold()
            )
            embed.add_field(
                name="--- CONSULTAS ---",
                value="\u200b",
                inline=False
            )
            embed.add_field(
                name="/tramite <consulta>",
                value="Consultar con IA. Ej: `/tramite que necesito para renovar pasaporte`",
                inline=False
            )
            embed.add_field(
                name="/buscar <palabra>",
                value="Busqueda rapida por keyword. Ej: `/buscar nacimiento`",
                inline=False
            )
            embed.add_field(
                name="--- GESTION ---",
                value="\u200b",
                inline=False
            )
            embed.add_field(
                name="/agregar <categoria> <nombre> <descripcion>",
                value="Agregar nuevo tramite a la base de datos",
                inline=False
            )
            embed.add_field(
                name="/corregir <tramite_id> <campo> <valor>",
                value="Corregir info de un tramite. Ej: `/corregir 5.1 descripcion 'nuevo texto'`",
                inline=False
            )
            embed.add_field(
                name="/notas <tramite_id> <nota>",
                value="Agregar nota a un tramite. Ej: `/notas 5.1 El plazo actual es 8 semanas`",
                inline=False
            )
            embed.add_field(
                name="--- MONITOREO ---",
                value="\u200b",
                inline=False
            )
            embed.add_field(name="/clientes", value="Ver clientes pendientes de cita", inline=False)
            embed.add_field(name="/estado", value="Ver estado actual del bot de citas", inline=False)
            embed.add_field(
                name="--- FEEDBACK ---",
                value="Despues de cada respuesta de /tramite, reacciona con \U0001f44d o \U0001f44e.\n"
                      "Si reaccionas \U0001f44e, el bot te preguntara que estuvo mal para aprender.",
                inline=False
            )

            embed.set_footer(text="RH Tramites Consulares | WhatsApp: +598 91 090 980")
            await interaction.response.send_message(embed=embed)

    # ==================================================================
    # SETUP & EVENTS
    # ==================================================================

    async def setup_hook(self):
        """Sincroniza los slash commands al arrancar."""
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"Comandos sincronizados con guild {DISCORD_GUILD_ID}")
        else:
            await self.tree.sync()
            log.info("Comandos sincronizados globalmente (puede tardar hasta 1 hora)")

        # Tarea de limpieza de feedback viejo
        self.loop.create_task(self._limpiar_feedback_viejo())

    async def on_ready(self):
        log.info(f"Bot conectado como {self.user} (ID: {self.user.id})")
        log.info(f"Servidores: {len(self.guilds)}")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="tramites consulares"
            )
        )

    # ==================================================================
    # SISTEMA DE FEEDBACK
    # ==================================================================

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Detecta reacciones 👎 en respuestas del bot para pedir feedback."""
        # Ignorar reacciones propias
        if payload.user_id == self.user.id:
            return

        # Solo procesar si es un mensaje con feedback pendiente
        if payload.message_id not in self._pending_feedback:
            return

        feedback_data = self._pending_feedback[payload.message_id]

        # Solo procesar 👎
        if str(payload.emoji) == "\U0001f44e":
            feedback_data["estado"] = "esperando_correccion"

            channel = self.get_channel(payload.channel_id)
            if channel:
                user = self.get_user(payload.user_id) or await self.fetch_user(payload.user_id)
                await channel.send(
                    f"{user.mention} Gracias por el feedback! "
                    f"Que estuvo mal en mi respuesta? Responde a este mensaje con la correccion.",
                    reference=discord.MessageReference(
                        message_id=payload.message_id,
                        channel_id=payload.channel_id
                    )
                )
                log.info(f"Feedback negativo de {user} en mensaje {payload.message_id}")

        elif str(payload.emoji) == "\U0001f44d":
            # Feedback positivo, limpiar
            del self._pending_feedback[payload.message_id]

    async def on_message(self, message: discord.Message):
        """Maneja menciones al bot y correcciones de feedback."""
        # Ignorar mensajes del bot
        if message.author == self.user:
            return

        # ============================================================
        # WHATSAPP: reply en canal de WhatsApp → enviar por WhatsApp o re-engage bot
        # ============================================================
        is_wa_channel = message.channel.id in DISCORD_WA_CHANNELS
        if message.reference and message.reference.message_id and is_wa_channel:
            ref_id = message.reference.message_id
            wa_data = self._whatsapp_messages.get(ref_id)

            # If not in memory (bot restarted), try to recover from the referenced message
            if not wa_data:
                wa_data = await self._recover_wa_data(message)

            if not wa_data:
                await message.reply("No pude identificar el cliente. Responde directamente al mensaje verde del cliente.")
                return

            # Strip bot mentions from content (Discord adds them automatically on reply)
            respuesta_texto = message.content
                for mention in message.mentions:
                    respuesta_texto = respuesta_texto.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
                respuesta_texto = respuesta_texto.strip()

                if not respuesta_texto:
                    return

                # Re-engagement: employee wants a new suggestion from the bot
                # Must start with "!bot" or "!sugerencia" prefix
                re_engage_prefixes = ("!bot ", "!sugerencia ", "!bot\n", "!sugerencia\n")
                if respuesta_texto.lower().startswith(re_engage_prefixes):
                    # Remove prefix to get the instruction
                    instruccion = respuesta_texto.split(None, 1)[1] if ' ' in respuesta_texto or '\n' in respuesta_texto else ""
                    instruccion = instruccion.strip()

                    if instruccion:
                        async with message.channel.typing():
                            try:
                                import conversation_db
                                conv = await conversation_db.get_conversation_by_phone(wa_data["sender"])
                                history = []
                                if conv:
                                    history = await conversation_db.get_recent_messages(conv["id"], limit=20)

                                nueva_sugerencia = await consultar_conversacional(
                                    client_message=history[-1]["content"] if history else "",
                                    conversation_history=history,
                                    employee_context=instruccion,
                                )

                                if conv:
                                    await conversation_db.add_message(conv["id"], "bot", nueva_sugerencia)

                                embed = discord.Embed(
                                    title="🤖 Nueva sugerencia del Bot",
                                    description=nueva_sugerencia[:1900],
                                    color=discord.Color.blue()
                                )
                                embed.set_footer(text=f"Instruccion: {instruccion[:100]}")
                                new_msg = await message.channel.send(embed=embed)
                                self._whatsapp_messages[new_msg.id] = wa_data

                            except Exception as e:
                                log.error(f"Error en re-engagement del bot: {e}")
                                await message.reply(f"Error generando nueva sugerencia: {e}")
                    else:
                        await message.reply("Usa: `!bot <instruccion>` para pedir una nueva sugerencia.")
                    return

                # Default: send reply to client via WhatsApp
                enviado = await self._enviar_whatsapp(wa_data["sender"], respuesta_texto)
                if enviado:
                    await message.add_reaction("\u2705")
                    log.info(f"Respuesta enviada por WhatsApp a {wa_data['sender']}: {respuesta_texto[:50]}")

                    try:
                        import conversation_db
                        conv = await conversation_db.get_conversation_by_phone(wa_data["sender"])
                        if conv:
                            await conversation_db.add_message(conv["id"], "employee", respuesta_texto)
                    except Exception as e:
                        log.error(f"Error guardando respuesta en DB: {e}")
                else:
                    await message.add_reaction("\u274c")
                    await message.reply("Error enviando el mensaje por WhatsApp. Verificar configuracion.")
                return

        # ============================================================
        # MENCION AL BOT (@bot mensaje en lenguaje natural)
        # Skip WhatsApp channels — replies there are handled above
        # ============================================================
        if self.user.mentioned_in(message) and not message.mention_everyone and not is_wa_channel:
            # Extraer texto sin la mencion
            texto = message.content
            for mention in message.mentions:
                texto = texto.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
            texto = texto.strip()

            if not texto:
                await message.reply("Hola! Preguntame lo que necesites sobre tramites consulares.")
                return

            log.info(f"Mencion de {message.author}: {texto[:80]}")

            async with message.channel.typing():
                resultado = await procesar_mensaje_natural(texto, str(message.author))

            tipo = resultado.get("tipo", "respuesta")
            texto_respuesta = resultado.get("texto", "No pude procesar tu mensaje.")

            # Formatear para Discord (max 2000 chars)
            chunks = formatear_respuesta_discord(texto_respuesta)

            if tipo == "guardado":
                msg = await message.reply(f"✅ {chunks[0]}")
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)

            elif tipo == "correccion":
                msg = await message.reply(f"✏️ {chunks[0]}")
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)

            elif tipo == "clientes":
                try:
                    from cita_bot_playwright import leer_clientes_google_sheets
                    clientes = leer_clientes_google_sheets()
                    if not clientes:
                        await message.reply("No hay clientes pendientes.")
                    else:
                        lineas = [f"**Clientes pendientes ({len(clientes)}):**"]
                        for i, c in enumerate(clientes[:10], 1):
                            lineas.append(f"{i}. {c.nombre} - {c.tramite}")
                        await message.reply("\n".join(lineas))
                except Exception as e:
                    await message.reply(f"Error obteniendo clientes: {e}")

            elif tipo == "estado":
                estado = self.cita_bot_estado
                activo = "Activo" if estado.get("activo") else "Inactivo"
                await message.reply(f"**Bot de Citas:** {activo} | Ciclo: {estado.get('ciclo', 0)} | Ultimo: {estado.get('ultimo_intento', 'N/A')}")

            elif tipo == "error":
                await message.reply(f"⚠️ {chunks[0]}")

            else:
                msg = await message.reply(chunks[0])
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)
                # Agregar reacciones de feedback
                try:
                    await msg.add_reaction("\U0001f44d")
                    await msg.add_reaction("\U0001f44e")
                    self._pending_feedback[msg.id] = {
                        "pregunta": texto,
                        "respuesta": chunks[0],
                        "usuario": str(message.author),
                        "timestamp": datetime.now(),
                        "estado": "esperando_reaccion"
                    }
                except Exception:
                    pass

            return

        # ============================================================
        # FEEDBACK: replies a mensajes del bot
        # ============================================================

        # Solo procesar si es un reply
        if not message.reference or not message.reference.message_id:
            return

        # Buscar si hay feedback pendiente para el mensaje referenciado
        # El reply puede ser al mensaje original del bot O al mensaje de "que estuvo mal?"
        ref_id = message.reference.message_id

        # Buscar en pending_feedback por el mensaje original
        feedback_data = None
        feedback_msg_id = None

        for msg_id, data in self._pending_feedback.items():
            if data.get("estado") == "esperando_correccion":
                # Verificar si el reply es al mensaje del bot o al followup
                if msg_id == ref_id or data.get("followup_id") == ref_id:
                    feedback_data = data
                    feedback_msg_id = msg_id
                    break

        # Tambien buscar si el reply es a un mensaje que contiene "que estuvo mal"
        if not feedback_data:
            try:
                ref_msg = await message.channel.fetch_message(ref_id)
                if ref_msg.author == self.user and "estuvo mal" in ref_msg.content.lower():
                    # Buscar el feedback original via la referencia del ref_msg
                    if ref_msg.reference and ref_msg.reference.message_id:
                        orig_id = ref_msg.reference.message_id
                        if orig_id in self._pending_feedback:
                            feedback_data = self._pending_feedback[orig_id]
                            feedback_msg_id = orig_id
            except Exception:
                pass

        if not feedback_data:
            return

        # Guardar la correccion
        correccion_texto = message.content.strip()
        if not correccion_texto:
            return

        resultado = guardar_feedback(
            pregunta=feedback_data.get("pregunta", ""),
            respuesta=feedback_data.get("respuesta", ""),
            correccion=correccion_texto,
            usuario=str(message.author)
        )

        if resultado.get("ok"):
            await message.reply(
                f"Correccion registrada (ID: {resultado['id']}). "
                f"Voy a tener en cuenta tu feedback para futuras respuestas. Gracias!"
            )
            log.info(f"Correccion guardada: {resultado['id']} por {message.author}")
        else:
            await message.reply("Hubo un error guardando tu correccion. Intenta de nuevo.")

        # Limpiar feedback pendiente
        if feedback_msg_id and feedback_msg_id in self._pending_feedback:
            del self._pending_feedback[feedback_msg_id]

    # ==================================================================
    # NOTIFICACIONES
    # ==================================================================

    async def notificar_reserva(self, datos: dict):
        """Envia notificacion al canal cuando se confirma una reserva."""
        if not DISCORD_CHANNEL_ID:
            return

        try:
            channel = self.get_channel(DISCORD_CHANNEL_ID)
            if not channel:
                channel = await self.fetch_channel(DISCORD_CHANNEL_ID)

            embed = discord.Embed(title="RESERVA EXITOSA", color=discord.Color.green())
            embed.add_field(name="Cliente", value=datos.get("cliente", "N/A"), inline=True)
            embed.add_field(name="Email", value=datos.get("email", "N/A"), inline=True)
            embed.add_field(name="Cita", value=datos.get("fecha_cita", "Confirmada"), inline=False)
            embed.set_footer(text=f"Reservado: {datos.get('timestamp', '')}")

            await channel.send(embed=embed)
            log.info(f"Notificacion enviada: {datos.get('cliente')}")

        except Exception as e:
            log.error(f"Error enviando notificacion: {e}")

    # ==================================================================
    # WHATSAPP → DISCORD
    # ==================================================================

    async def enviar_mensaje_whatsapp(self, datos: dict, channel_id: int = 0):
        """Reenvía un mensaje de WhatsApp a Discord con sugerencia de IA."""
        target_channel_id = channel_id or DISCORD_WHATSAPP_CHANNEL_ID
        if not target_channel_id:
            log.warning("Ningun canal de WhatsApp configurado")
            return

        try:
            channel = self.get_channel(target_channel_id)
            if not channel:
                channel = await self.fetch_channel(target_channel_id)

            sender = datos.get("sender", "Desconocido")
            sender_name = datos.get("sender_name", "")
            texto = datos.get("texto", "")
            sugerencia = datos.get("sugerencia", "")
            timestamp = datos.get("timestamp", "")

            # Formatear nombre del remitente
            nombre_display = sender_name if sender_name else f"+{sender}"
            if sender_name:
                nombre_display = f"{sender_name} (+{sender})"

            # Embed principal con el mensaje del cliente
            embed = discord.Embed(
                title=f"📱 WhatsApp — {nombre_display}",
                description=f"**Mensaje:**\n{texto}",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.set_footer(text=f"Responde a este mensaje para contestarle al cliente por WhatsApp")

            msg = await channel.send(embed=embed)

            # Guardar mapeo para que cuando el empleado responda, se envíe por WhatsApp
            self._whatsapp_messages[msg.id] = {
                "sender": sender,
                "sender_name": sender_name,
                "timestamp": datetime.now()
            }

            # Embed con la sugerencia del bot (separado para que sea fácil copiar)
            if sugerencia:
                if len(sugerencia) > 1900:
                    sugerencia = sugerencia[:1900] + "..."

                embed_sugerencia = discord.Embed(
                    title="🤖 Sugerencia del Bot",
                    description=sugerencia,
                    color=discord.Color.blue()
                )
                embed_sugerencia.set_footer(text="Responde para enviar al cliente | !bot <instruccion> para nueva sugerencia")

                sugerencia_msg = await channel.send(embed=embed_sugerencia)

                # También mapear el mensaje de sugerencia al mismo sender
                self._whatsapp_messages[sugerencia_msg.id] = {
                    "sender": sender,
                    "sender_name": sender_name,
                    "timestamp": datetime.now()
                }

            log.info(f"Mensaje de WhatsApp reenviado a Discord: {nombre_display}")

        except Exception as e:
            log.error(f"Error reenviando WhatsApp a Discord: {e}")

    # ==================================================================
    # RECUPERAR DATOS DE WHATSAPP DESDE MENSAJE REFERENCIADO
    # ==================================================================

    async def _recover_wa_data(self, message: discord.Message) -> dict | None:
        """
        Recover WhatsApp sender data from a referenced Discord message.
        Searches the embed title for a phone number pattern, or looks up
        the conversation DB by scanning recent embeds in the reply chain.
        """
        import re
        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
        except Exception:
            return None

        # Check embeds in the referenced message for phone number
        phone = None
        sender_name = ""
        for embed in ref_msg.embeds:
            title = embed.title or ""
            # Match patterns like "WhatsApp — nombre (+598XXXXXXX)" or "WhatsApp — +598XXXXXXX"
            match = re.search(r'\+?(\d{10,15})', title)
            if match:
                phone = match.group(1)
                # Extract name from title
                name_match = re.search(r'—\s*(.+?)\s*\(', title)
                if name_match:
                    sender_name = name_match.group(1).strip()
                break
            # Also check footer for "Instruccion:" (suggestion embed) — walk up to parent
            footer_text = embed.footer.text if embed.footer else ""
            if "sugerencia" in title.lower() or "Instruccion:" in footer_text:
                # This is a suggestion embed, not the client message.
                # Try to find the client by looking at conversation_db for this channel
                pass

        # If no phone from embed, try conversation_db: find active conversation in this channel
        if not phone:
            try:
                import conversation_db
                conv = await conversation_db.get_active_conversation_by_channel(message.channel.id)
                if conv:
                    phone = conv["phone_number"]
                    sender_name = conv.get("sender_name", "")
            except Exception as e:
                log.error(f"Error recovering WA data from DB: {e}")

        if phone:
            wa_data = {"sender": phone, "sender_name": sender_name, "timestamp": datetime.now()}
            # Cache it for future replies
            self._whatsapp_messages[message.reference.message_id] = wa_data
            return wa_data

        return None

    # ==================================================================
    # ENVIAR WHATSAPP VIA META CLOUD API
    # ==================================================================

    async def _enviar_whatsapp(self, to: str, texto: str) -> bool:
        """Envía un mensaje de WhatsApp al cliente via Meta Cloud API."""
        import os
        whatsapp_token = os.environ.get("WHATSAPP_TOKEN", "")
        whatsapp_phone_id = os.environ.get("WHATSAPP_PHONE_ID", "")

        if not whatsapp_token or not whatsapp_phone_id:
            log.error("WHATSAPP_TOKEN o WHATSAPP_PHONE_ID no configurados")
            return False

        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://graph.facebook.com/v25.0/{whatsapp_phone_id}/messages",
                    headers={
                        "Authorization": f"Bearer {whatsapp_token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "messaging_product": "whatsapp",
                        "to": to,
                        "type": "text",
                        "text": {"body": texto}
                    },
                    timeout=15.0
                )
                if response.status_code == 200:
                    log.info(f"WhatsApp enviado a {to}")
                    return True
                else:
                    log.error(f"Error enviando WhatsApp: {response.status_code} {response.text}")
                    return False
        except Exception as e:
            log.error(f"Error enviando WhatsApp: {e}")
            return False

    # ==================================================================
    # LIMPIEZA PERIODICA
    # ==================================================================

    async def _limpiar_feedback_viejo(self):
        """Limpia feedback pendiente y mapeos de WhatsApp viejos."""
        while True:
            await asyncio.sleep(300)  # Cada 5 minutos
            ahora = datetime.now()

            # Limpiar feedback viejo (30 min)
            ids_a_eliminar = []
            for msg_id, data in self._pending_feedback.items():
                ts = data.get("timestamp")
                if isinstance(ts, datetime) and (ahora - ts) > timedelta(minutes=30):
                    ids_a_eliminar.append(msg_id)
            for msg_id in ids_a_eliminar:
                del self._pending_feedback[msg_id]

            # Limpiar mapeos de WhatsApp viejos (24 horas)
            wa_a_eliminar = []
            for msg_id, data in self._whatsapp_messages.items():
                ts = data.get("timestamp")
                if isinstance(ts, datetime) and (ahora - ts) > timedelta(hours=24):
                    wa_a_eliminar.append(msg_id)
            for msg_id in wa_a_eliminar:
                del self._whatsapp_messages[msg_id]

            total = len(ids_a_eliminar) + len(wa_a_eliminar)
            if total:
                log.info(f"Limpieza: {len(ids_a_eliminar)} feedback + {len(wa_a_eliminar)} WhatsApp")


# ============================================================================
# TASK DE RELAY
# ============================================================================

async def relay_notificaciones(bot: ConsularBot, sync_queue: queue.Queue):
    """Lee eventos de la queue del CitaBot y los envia por Discord."""
    log.info("Relay de notificaciones iniciado")
    while True:
        try:
            try:
                evento = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: sync_queue.get(timeout=2)
                )
            except queue.Empty:
                await asyncio.sleep(1)
                continue

            if evento.get("tipo") == "reserva_exitosa":
                await bot.notificar_reserva(evento)
            else:
                log.info(f"Evento desconocido: {evento.get('tipo')}")

        except Exception as e:
            log.error(f"Error en relay: {e}")
            await asyncio.sleep(5)


# ============================================================================
# EJECUCION STANDALONE
# ============================================================================

async def main():
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN no configurado")
        return
    bot = ConsularBot()
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    asyncio.run(main())
