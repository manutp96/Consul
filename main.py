"""
Orquestador Principal - RH Tramites Consulares
================================================

Arranca todos los servicios en un solo proceso:
- CitaBot (thread dedicado - Playwright sincronico)
- Discord Bot (asyncio)
- FastAPI Server (asyncio via uvicorn)

Cada servicio es opcional y se activa segun las variables de entorno.
"""

import asyncio
import logging
import os
import queue
import signal
import sys
import threading

# Cargar variables de entorno desde .env (si existe)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # En produccion (Railway) no se necesita dotenv

# Configurar logging ANTES de importar modulos
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("Main")

# ============================================================================
# CONFIGURACION
# ============================================================================

ENABLE_CITA_BOT = os.environ.get("ENABLE_CITA_BOT", "true").lower() == "true"
ENABLE_DISCORD = os.environ.get("ENABLE_DISCORD", "true").lower() == "true"
ENABLE_API = os.environ.get("ENABLE_API", "true").lower() == "true"
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
API_PORT = int(os.environ.get("PORT", "8000"))

# Queue para comunicacion entre CitaBot (thread) y Discord/API (async)
notification_queue = queue.Queue()

# Estado compartido del CitaBot (leido por Discord y API)
cita_bot_estado = {
    "activo": False,
    "ciclo": 0,
    "ultimo_intento": None,
    "ultima_reserva": None,
    "servicio": "Registro Civil",
    "cliente_actual": None,
}

# Flag de shutdown
shutdown_event = threading.Event()


# ============================================================================
# CITA BOT (Thread dedicado)
# ============================================================================

def run_cita_bot():
    """Corre el CitaBot en un thread separado (es sincronico con Playwright)."""
    log.info("Iniciando CitaBot en thread dedicado...")

    try:
        from cita_bot_playwright import CitaBot, leer_clientes_google_sheets, POLL_INTERVAL

        bot = CitaBot(notification_queue=notification_queue)

        # Compartir referencia al estado
        cita_bot_estado.update(bot.estado)
        bot.estado = cita_bot_estado

        # Verificar Google Sheets
        log.info("Verificando conexion con Google Sheets...")
        clientes = leer_clientes_google_sheets()
        if clientes:
            log.info(f"Clientes pendientes: {len(clientes)}")
        else:
            log.info("No hay clientes pendientes por ahora.")

        bot.iniciar_browser()
        log.info("CitaBot iniciado - entrando en modo monitoreo")
        bot.monitorear(intervalo=POLL_INTERVAL)

    except Exception as e:
        log.error(f"Error en CitaBot: {e}", exc_info=True)
        cita_bot_estado["activo"] = False
    finally:
        cita_bot_estado["activo"] = False
        log.info("CitaBot finalizado")


# ============================================================================
# DISCORD BOT (Asyncio)
# ============================================================================

async def run_discord_bot():
    """Corre el bot de Discord en el event loop principal."""
    if not DISCORD_TOKEN:
        log.warning("DISCORD_TOKEN no configurado - Discord desactivado")
        return

    log.info("Iniciando bot de Discord...")

    try:
        from discord_bot import ConsularBot, relay_notificaciones

        bot = ConsularBot(
            notification_queue=notification_queue,
            cita_bot_estado=cita_bot_estado
        )

        # Compartir referencia del bot con el API server para WhatsApp → Discord
        from api_server import set_discord_bot
        set_discord_bot(bot)

        # Crear task para relay de notificaciones
        asyncio.create_task(relay_notificaciones(bot, notification_queue))

        await bot.start(DISCORD_TOKEN)

    except Exception as e:
        log.error(f"Error en Discord bot: {e}", exc_info=True)


# ============================================================================
# API SERVER (Asyncio via uvicorn)
# ============================================================================

async def run_api_server():
    """Corre el servidor FastAPI con uvicorn."""
    log.info(f"Iniciando API server en puerto {API_PORT}...")

    try:
        import uvicorn
        from api_server import app, set_cita_bot_estado

        # Compartir estado del CitaBot con la API
        set_cita_bot_estado(cita_bot_estado)

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=API_PORT,
            log_level="info",
            access_log=False  # Evitar log duplicado
        )
        server = uvicorn.Server(config)
        await server.serve()

    except Exception as e:
        log.error(f"Error en API server: {e}", exc_info=True)


# ============================================================================
# SHUTDOWN HANDLER
# ============================================================================

def signal_handler(sig, frame):
    """Maneja SIGTERM (Railway) y SIGINT (Ctrl+C)."""
    log.info(f"\nRecibida señal {sig}. Cerrando servicios...")
    shutdown_event.set()
    sys.exit(0)


# ============================================================================
# PUNTO DE ENTRADA PRINCIPAL
# ============================================================================

async def async_main():
    """Main asincrono: corre Discord + API en paralelo."""
    tasks = []

    if ENABLE_API:
        tasks.append(asyncio.create_task(run_api_server()))
    else:
        log.info("API server desactivado")

    if ENABLE_DISCORD and DISCORD_TOKEN:
        tasks.append(asyncio.create_task(run_discord_bot()))
    else:
        log.info("Discord bot desactivado (sin DISCORD_TOKEN o ENABLE_DISCORD=false)")

    if tasks:
        await asyncio.gather(*tasks)
    else:
        # Si no hay servicios async, solo mantener vivo para el CitaBot thread
        log.info("Solo CitaBot activo (sin servicios async)")
        while not shutdown_event.is_set():
            await asyncio.sleep(5)


def main():
    """Punto de entrada principal."""
    log.info("=" * 60)
    log.info("RH TRAMITES CONSULARES - Sistema Integrado")
    log.info("=" * 60)
    log.info(f"CitaBot:  {'ACTIVADO' if ENABLE_CITA_BOT else 'DESACTIVADO'}")
    log.info(f"Discord:  {'ACTIVADO' if ENABLE_DISCORD and DISCORD_TOKEN else 'DESACTIVADO'}")
    log.info(f"API:      {'ACTIVADO (puerto ' + str(API_PORT) + ')' if ENABLE_API else 'DESACTIVADO'}")
    log.info("=" * 60)

    # Registrar signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Iniciar CitaBot en thread dedicado (si esta habilitado)
    if ENABLE_CITA_BOT:
        cita_thread = threading.Thread(target=run_cita_bot, daemon=True, name="CitaBot")
        cita_thread.start()
        log.info("CitaBot thread iniciado")
    else:
        log.info("CitaBot desactivado")

    # Correr servicios async (Discord + API)
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        log.info("\nCerrando (Ctrl+C)...")
    except Exception as e:
        log.error(f"Error fatal: {e}", exc_info=True)
    finally:
        shutdown_event.set()
        log.info("Sistema cerrado.")


if __name__ == "__main__":
    main()
