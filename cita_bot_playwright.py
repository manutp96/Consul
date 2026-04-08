#!/usr/bin/env python3
"""
Bot de Citas Consulares - Playwright Edition v4
================================================

Lee clientes directamente de Google Sheets y reserva citas automáticamente.

INSTALACIÓN:
    pip install playwright
    python -m playwright install chromium

USO:
    python cita_bot_playwright.py

GOOGLE SHEETS:
    La hoja debe tener estas columnas (fila 1 = encabezados):
    | nombre | email | movil | pasaporte | nacimiento | tramite | estado |
"""

import time
import csv
import json
import logging
import os
import io
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

WIDGET_URL = "https://www.citaconsular.es/es/hosteds/widgetdefault/2846ed4c1563cb7e2bdfea65b41ebdd55/"
SERVICIO = "Registro Civil"
POLL_INTERVAL = 300  # 5 minutos entre intentos
MOSTRAR_NAVEGADOR = os.environ.get("SHOW_BROWSER", "false").lower() == "true"

# Google Sheets - exportar como CSV
GOOGLE_SHEET_ID = "1W2x9gyZSHNFraiPxI7ucpNeTyWNuk0fDePV96AiNLAs"
GOOGLE_SHEET_CSV = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/export?format=csv"

# Archivo local para rastrear reservas completadas
SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
RESERVADOS_FILE = SCRIPT_DIR / "reservados.json"

# Columnas esperadas en Google Sheets
COL_NOMBRE = "nombre"
COL_EMAIL = "email"
COL_MOVIL = "movil"
COL_PASAPORTE = "pasaporte"
COL_NACIMIENTO = "nacimiento"
COL_TRAMITE = "tramite"
COL_ESTADO = "estado"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("CitaBot")


# ============================================================================
# DATOS DEL CLIENTE
# ============================================================================

@dataclass
class Cliente:
    nombre: str
    email: str
    movil: str
    pasaporte: str
    nacimiento: str
    tramite: str
    fila: int  # Fila en la hoja (para referencia)


# ============================================================================
# LECTOR DE GOOGLE SHEETS
# ============================================================================

def cargar_reservados() -> dict:
    """Carga el registro local de reservas completadas."""
    if RESERVADOS_FILE.exists():
        try:
            with open(RESERVADOS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}


def guardar_reservado(cliente: Cliente, fecha_cita: str = ""):
    """Registra un cliente como reservado en el archivo local."""
    reservados = cargar_reservados()
    clave = cliente.nombre.strip().upper()
    reservados[clave] = {
        "nombre": cliente.nombre,
        "email": cliente.email,
        "pasaporte": cliente.pasaporte,
        "fecha_cita": fecha_cita,
        "reservado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(RESERVADOS_FILE, "w", encoding="utf-8") as f:
        json.dump(reservados, f, indent=2, ensure_ascii=False)
    log.info(f"Reserva guardada localmente: {cliente.nombre}")


def leer_clientes_google_sheets() -> list:
    """Lee los clientes pendientes directamente de Google Sheets."""
    log.info("Descargando datos de Google Sheets...")

    try:
        response = urlopen(GOOGLE_SHEET_CSV, timeout=15)
        contenido = response.read().decode("utf-8")
    except Exception as e:
        log.error(f"Error descargando Google Sheets: {e}")
        return []

    reader = csv.DictReader(io.StringIO(contenido))

    # Normalizar nombres de columnas (minúsculas, sin espacios extra)
    if reader.fieldnames:
        reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
        log.info(f"Columnas encontradas: {reader.fieldnames}")

    # Verificar columnas necesarias
    required = [COL_NOMBRE, COL_EMAIL, COL_MOVIL, COL_NACIMIENTO]
    for col_name in required:
        if col_name not in (reader.fieldnames or []):
            log.error(f"Falta la columna '{col_name}' en Google Sheets")
            return []

    # Cargar registro de reservados locales
    reservados = cargar_reservados()

    clientes = []
    for fila_num, row in enumerate(reader, start=2):
        nombre = (row.get(COL_NOMBRE) or "").strip()
        if not nombre:
            continue  # Fila vacía

        email = (row.get(COL_EMAIL) or "").strip()
        pasaporte = (row.get(COL_PASAPORTE) or "").strip()

        # Verificar si ya está en la columna "estado" de la hoja
        estado_hoja = (row.get(COL_ESTADO) or "").strip().upper()
        if "RESERVADO" in estado_hoja:
            log.info(f"  Saltando {nombre} (marcado RESERVADO en hoja)")
            continue

        # Verificar si ya está en nuestro registro local (por nombre)
        clave = nombre.upper()
        if clave in reservados:
            log.info(f"  Saltando {nombre} (reservado localmente el {reservados[clave].get('reservado_en', '?')})")
            continue

        movil = (row.get(COL_MOVIL) or "").strip()
        # Asegurar que el móvil sea string (a veces CSV lo lee como número)
        movil = str(int(float(movil))) if movil and movil.replace('.', '').replace(',', '').isdigit() else movil

        cliente = Cliente(
            nombre=nombre.upper(),
            email=email,
            movil=movil,
            pasaporte=pasaporte,
            nacimiento=(row.get(COL_NACIMIENTO) or "").strip(),
            tramite=(row.get(COL_TRAMITE) or "Registro Civil").strip(),
            fila=fila_num,
        )
        clientes.append(cliente)

    log.info(f"Clientes pendientes: {len(clientes)}")
    for c in clientes:
        log.info(f"  - {c.nombre} | {c.email} | Pasaporte: {c.pasaporte}")

    return clientes


# ============================================================================
# BOT PRINCIPAL
# ============================================================================

class CitaBot:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None

    def iniciar_browser(self):
        log.info("Iniciando navegador...")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=not MOSTRAR_NAVEGADOR,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/146.0.7680.178 Safari/537.36",
            locale="es-ES",
        )
        self.page = context.new_page()
        self.page.on("dialog", lambda dialog: dialog.accept())
        log.info("Navegador iniciado")

    def cerrar_browser(self):
        try:
            if self.browser:
                self.browser.close()
        except:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except:
            pass
        self.browser = None
        self.playwright = None
        self.page = None
        log.info("Navegador cerrado")

    def asegurar_browser(self):
        """Verifica que el navegador esté abierto. Si se cerró, abre uno nuevo."""
        try:
            if self.page:
                self.page.url  # Test si la página responde
                return True
        except:
            log.warning("El navegador se cerró. Reabriendo...")

        # Limpiar todo y reabrir
        self.cerrar_browser()
        self.iniciar_browser()
        return True

    def tomar_screenshot(self, nombre: str):
        try:
            path = SCRIPT_DIR / f"screenshot_{nombre}_{datetime.now().strftime('%H%M%S')}.png"
            self.page.screenshot(path=str(path))
            log.info(f"Screenshot: {path.name}")
        except Exception as e:
            log.warning(f"No se pudo guardar screenshot: {e}")

    # ------------------------------------------------------------------
    # PASO 1: Navegar al widget
    # ------------------------------------------------------------------
    def navegar_al_widget(self):
        log.info(f"Navegando al widget...")
        self.page.goto(WIDGET_URL, timeout=30000)
        time.sleep(3)
        try:
            self.page.wait_for_selector("text='Continue'", timeout=15000)
            log.info("Página cargada")
        except PlaywrightTimeout:
            log.warning("No se encontró botón Continue")

    # ------------------------------------------------------------------
    # PASO 2: Click en Continue
    # ------------------------------------------------------------------
    def click_continuar(self) -> bool:
        log.info("Click en Continue / Continuar...")
        time.sleep(2)
        for sel in ["a:has-text('Continue')", "a:has-text('Continuar')",
                     "button:has-text('Continue')", ".btn-success"]:
            try:
                elem = self.page.locator(sel).first
                if elem.is_visible(timeout=2000):
                    elem.evaluate("el => el.click()")
                    log.info(f"OK: {sel}")
                    time.sleep(3)

                    # Esperar a que la URL cambie a #services
                    log.info("Esperando navegación a #services...")
                    for _ in range(20):  # Hasta 40 segundos
                        if "#services" in self.page.url:
                            log.info("Llegamos a #services")
                            break
                        time.sleep(2)
                    else:
                        log.warning(f"URL no cambió a #services. URL actual: {self.page.url}")

                    # Esperar a que cargue el contenido AJAX
                    try:
                        self.page.wait_for_load_state("networkidle", timeout=20000)
                    except:
                        pass
                    time.sleep(5)
                    return True
            except:
                continue
        log.error("No se pudo hacer clic en Continue")
        return False

    # ------------------------------------------------------------------
    # PASO 3: Seleccionar servicio
    # ------------------------------------------------------------------
    def seleccionar_servicio(self) -> bool:
        log.info(f"Seleccionando servicio: {SERVICIO}")

        # Esperar hasta 60 segundos a que aparezca el texto del servicio
        encontrado = False
        for intento in range(12):  # 12 intentos x 5s = 60s
            try:
                self.page.wait_for_selector(f"text='{SERVICIO}'", timeout=5000)
                log.info(f"Texto '{SERVICIO}' visible")
                encontrado = True
                break
            except PlaywrightTimeout:
                log.info(f"Esperando '{SERVICIO}'... (intento {intento + 1}/12)")
                # Verificar si la URL tiene #services
                if "#services" not in self.page.url:
                    log.info(f"URL actual: {self.page.url} - aún no estamos en servicios")
                time.sleep(2)

        if not encontrado:
            log.error(f"'{SERVICIO}' no apareció después de 60 segundos")
            return False

        time.sleep(1)

        for sel in [f"text='{SERVICIO}'", f"li:has-text('{SERVICIO}')",
                     f"a:has-text('{SERVICIO}')", f"div:has-text('{SERVICIO}')"]:
            try:
                elem = self.page.locator(sel).first
                if elem.is_visible(timeout=2000):
                    elem.click()
                    log.info(f"Servicio clickeado: {sel}")
                    time.sleep(2)
                    # Botón siguiente si existe
                    try:
                        btn = self.page.locator("a:has-text('Continuar'), button:has-text('Continuar')").first
                        if btn.is_visible(timeout=3000):
                            btn.evaluate("el => el.click()")
                    except:
                        pass
                    time.sleep(5)
                    try:
                        self.page.wait_for_load_state("networkidle", timeout=30000)
                    except:
                        pass
                    time.sleep(5)
                    return True
            except:
                continue

        log.error("No se pudo seleccionar servicio")
        return False

    # ------------------------------------------------------------------
    # PASO 4: Buscar y seleccionar horario (espera hasta 5 minutos)
    # ------------------------------------------------------------------
    def buscar_y_seleccionar_horario(self) -> bool:
        log.info("Buscando horarios disponibles (espera hasta 5 min)...")
        MAX_ESPERA = 300  # 5 minutos en segundos
        INTERVALO_CHECK = 10  # Revisar cada 10 segundos
        inicio = time.time()

        while (time.time() - inicio) < MAX_ESPERA:
            transcurrido = int(time.time() - inicio)

            # Esperar a que la página cargue algo
            try:
                self.page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass
            time.sleep(3)

            url = self.page.url
            log.info(f"[{transcurrido}s] URL: {url}")

            # Si ya estamos en el formulario, éxito
            if "#signupfirstappointment" in url:
                log.info("¡Ya estamos en el formulario!")
                return True

            # Leer contenido de la página
            try:
                texto = self.page.inner_text("body").lower()
            except:
                texto = ""

            # Verificar si dice explícitamente que no hay turnos
            sin_turno = ["no hay horas disponibles", "no hay citas disponibles",
                         "no quedan horas", "inténtelo de nuevo",
                         "intentelo de nuevo", "no hay turnos", "sin disponibilidad"]
            encontro_sin_turno = False
            for msg in sin_turno:
                if msg in texto:
                    log.info(f"[{transcurrido}s] Sin turnos: '{msg}'")
                    encontro_sin_turno = True
                    break

            if encontro_sin_turno:
                return False

            # Buscar horarios en páginas #datetime o #agendas
            if "#datetime" in url or "#agendas" in url:
                log.info(f"[{transcurrido}s] En página de horarios/agendas...")

                # Buscar barras de horarios (como "08:50", "09:00")
                try:
                    all_links = self.page.locator("a:visible").all()
                    horarios = []
                    for link in all_links:
                        try:
                            text = link.inner_text().strip()
                            if ":" in text and len(text) <= 10 and any(c.isdigit() for c in text):
                                horarios.append((text, link))
                        except:
                            pass

                    if horarios:
                        log.info(f"¡{len(horarios)} horarios disponibles!")
                        for h, _ in horarios:
                            log.info(f"  - {h}")

                        # Seleccionar el primero
                        nombre_horario, elem = horarios[0]
                        log.info(f"Seleccionando: {nombre_horario}")
                        try:
                            elem.click()
                        except:
                            elem.evaluate("el => el.click()")
                        time.sleep(5)

                        if "#signupfirstappointment" in self.page.url:
                            log.info("¡Navegó al formulario!")
                            return True

                        # Fallback JS click
                        try:
                            elem.evaluate("el => el.click()")
                        except:
                            pass
                        time.sleep(5)
                        if "#signupfirstappointment" in self.page.url:
                            return True

                        return True
                    else:
                        log.info(f"[{transcurrido}s] Página cargando, no hay horarios todavía... esperando {INTERVALO_CHECK}s")
                except Exception as e:
                    log.info(f"[{transcurrido}s] Página aún cargando ({e})... esperando {INTERVALO_CHECK}s")

            elif "#services" in url or url.endswith("/"):
                log.info(f"[{transcurrido}s] Aún en servicios, esperando navegación...")

            else:
                log.info(f"[{transcurrido}s] Esperando que cargue la página...")

            time.sleep(INTERVALO_CHECK)

        log.info(f"Se agotó el tiempo de espera ({MAX_ESPERA}s)")
        return False

    # ------------------------------------------------------------------
    # PASO 5: Llenar formulario
    # ------------------------------------------------------------------
    def llenar_formulario(self, cliente: Cliente) -> bool:
        """
        Llena el formulario con los datos del cliente.

        Campos del formulario (de los screenshots):
        - * Nombre y Apellidos
        - * Email
        - +598 - Uru  * Móvil
        - Número de Pasaporte
        - * Fecha de nacimiento
        - DESCRIBA EL TRÁMITE A REALIZAR (textarea)
        - Checkbox política de privacidad
        """
        log.info(f"Llenando formulario para: {cliente.nombre}")
        time.sleep(2)

        # Esperar a que el formulario cargue
        try:
            self.page.wait_for_selector("input:visible", timeout=10000)
        except:
            log.warning("Timeout esperando inputs del formulario")

        # Listar todos los campos para debug
        inputs = self.page.locator("input:visible:not([type='checkbox']):not([type='hidden']), textarea:visible")
        count = inputs.count()
        log.info(f"Campos visibles: {count}")

        campos_info = []
        for i in range(count):
            try:
                info = inputs.nth(i).evaluate("""el => ({
                    tag: el.tagName, type: el.type || '', name: el.name || '',
                    placeholder: el.placeholder || '', id: el.id || '',
                    className: el.className || ''
                })""")
                campos_info.append(info)
                log.info(f"  [{i}] {info}")
            except:
                pass

        # Estrategia: los campos están en orden en el formulario
        # [0] Nombre y Apellidos
        # [1] Email
        # [2] Móvil (sin código de país, ya está incluido)
        # [3] Número de Pasaporte
        # [4] Fecha de nacimiento

        datos_en_orden = [
            (cliente.nombre, "Nombre"),
            (cliente.email, "Email"),
            (cliente.movil, "Móvil"),
            (cliente.pasaporte, "Pasaporte"),
            (cliente.nacimiento, "Fecha nacimiento"),
        ]

        # Llenar inputs por posición
        inputs_no_check = self.page.locator("input:visible:not([type='checkbox']):not([type='hidden'])")
        input_count = inputs_no_check.count()

        for i, (valor, nombre_campo) in enumerate(datos_en_orden):
            if i < input_count:
                try:
                    campo = inputs_no_check.nth(i)
                    campo.click()
                    campo.fill("")  # Limpiar primero
                    campo.fill(valor)
                    log.info(f"  [{i}] {nombre_campo}: {valor}")
                except Exception as e:
                    log.warning(f"  [{i}] Error llenando {nombre_campo}: {e}")
                    self._llenar_por_keyword(nombre_campo, valor)

        # Llenar textarea (descripción del trámite)
        try:
            textarea = self.page.locator("textarea:visible").first
            if textarea.is_visible(timeout=2000):
                textarea.fill(cliente.tramite)
                log.info(f"  Descripción: {cliente.tramite}")
        except:
            log.warning("  No se encontró textarea")

        # Marcar checkbox de privacidad
        try:
            checkbox = self.page.locator("input[type='checkbox']:visible").first
            if checkbox.is_visible(timeout=2000):
                if not checkbox.is_checked():
                    checkbox.check()
                    log.info("  Checkbox privacidad: marcado")
        except:
            log.warning("  No se encontró checkbox")

        log.info("Formulario completado")
        return True

    def _llenar_por_keyword(self, keyword: str, valor: str):
        """Fallback: busca campo por placeholder o name."""
        keywords = {
            "Nombre": ["Nombre", "nombre", "name"],
            "Email": ["Email", "email", "correo"],
            "Móvil": ["Móvil", "Movil", "phone", "tel"],
            "Pasaporte": ["Pasaporte", "pasaporte", "passport"],
            "Fecha nacimiento": ["nacimiento", "Fecha", "birth"],
        }
        for kw in keywords.get(keyword, [keyword]):
            try:
                c = self.page.locator(f"input[placeholder*='{kw}']:visible").first
                if c.is_visible(timeout=1000):
                    c.fill(valor)
                    return
            except:
                pass

    # ------------------------------------------------------------------
    # PASO 6: Confirmar reserva
    # ------------------------------------------------------------------
    def confirmar_reserva(self) -> str:
        """
        Hace clic en Confirmar y verifica el éxito.
        Retorna la fecha/hora de la cita si fue exitoso, o "" si falló.
        """
        log.info("Confirmando reserva...")

        # Capturar info de la cita antes de confirmar
        info_cita = ""
        try:
            header = self.page.inner_text("body")
            for line in header.split("\n"):
                if any(mes in line for mes in ["Enero", "Febrero", "Marzo", "Abril",
                       "Mayo", "Junio", "Julio", "Agosto", "Septiembre",
                       "Octubre", "Noviembre", "Diciembre", "2026", "2027"]):
                    info_cita = line.strip()
                    break
        except:
            pass

        try:
            btn = self.page.locator("a:has-text('Confirmar'), button:has-text('Confirmar'), input[value*='Confirmar']").first
            if btn.is_visible(timeout=5000):
                btn.evaluate("el => el.click()")
                log.info("Botón Confirmar clickeado")
                time.sleep(5)
                self.page.wait_for_load_state("networkidle", timeout=30000)

                texto = self.page.inner_text("body").lower()
                if "éxito" in texto or "exito" in texto or "realizado" in texto or "#summary" in self.page.url:
                    log.info("=" * 50)
                    log.info("¡¡¡ RESERVA EXITOSA !!!")
                    log.info(f"Cita: {info_cita}")
                    log.info("=" * 50)
                    return info_cita or "CONFIRMADA"

            log.warning("No se pudo confirmar")
            return ""
        except Exception as e:
            log.error(f"Error confirmando: {e}")
            return ""

    # ------------------------------------------------------------------
    # FLUJO COMPLETO PARA UN CLIENTE
    # ------------------------------------------------------------------
    def intentar_reserva(self, cliente: Cliente) -> str:
        """
        Intenta reservar para un cliente específico.
        Retorna info de la cita si fue exitoso, "" si no.
        """
        try:
            # Si el navegador se cerró, abrir uno nuevo
            self.asegurar_browser()
            self.navegar_al_widget()
            time.sleep(2)

            if not self.click_continuar():
                return ""
            time.sleep(2)

            if not self.seleccionar_servicio():
                return ""
            time.sleep(2)

            if not self.buscar_y_seleccionar_horario():
                return ""
            time.sleep(2)

            if not self.llenar_formulario(cliente):
                self.tomar_screenshot("error_form")
                return ""

            self.tomar_screenshot("form_llenado")

            resultado = self.confirmar_reserva()
            if resultado:
                self.tomar_screenshot("reserva_ok")
            else:
                self.tomar_screenshot("error_confirm")

            return resultado

        except Exception as e:
            log.error(f"Error: {e}")
            self.tomar_screenshot("error")
            return ""

    # ------------------------------------------------------------------
    # MODO MONITOREO: Procesa la cola de clientes de Google Sheets
    # ------------------------------------------------------------------
    def monitorear(self, intervalo: int = POLL_INTERVAL):
        """
        Loop principal:
        1. Lee clientes pendientes de Google Sheets
        2. Para cada cliente, intenta reservar
        3. Si hay turno, reserva y guarda en reservados.json
        4. Si no hay turno, espera e intenta de nuevo
        """
        ciclo = 0

        log.info("=" * 50)
        log.info("MONITOREO ACTIVO")
        log.info(f"Servicio: {SERVICIO}")
        log.info(f"Intervalo: {intervalo}s")
        log.info(f"Google Sheet: {GOOGLE_SHEET_ID}")
        log.info("=" * 50)

        while True:
            ciclo += 1

            # Leer clientes pendientes (re-descarga Google Sheets cada ciclo)
            clientes = leer_clientes_google_sheets()

            if not clientes:
                log.info("No hay clientes pendientes. Esperando...")
                time.sleep(intervalo)
                continue

            # Intentar reservar para el primer cliente en cola
            cliente = clientes[0]
            log.info(f"\n--- Ciclo #{ciclo} | {datetime.now().strftime('%H:%M:%S')} ---")
            log.info(f"Intentando para: {cliente.nombre}")

            try:
                resultado = self.intentar_reserva(cliente)
            except Exception as e:
                log.error(f"Error en intento: {e}")
                log.info("Reintentando en el próximo ciclo...")
                resultado = ""

            if resultado:
                # ¡Éxito! Guardar en registro local
                guardar_reservado(cliente, resultado)
                log.info(f"\n¡RESERVA COMPLETADA para {cliente.nombre}!")
                log.info(f"Cita: {resultado}")

                # Pausa breve antes del siguiente cliente
                time.sleep(5)

                # Verificar si hay más clientes
                clientes_restantes = leer_clientes_google_sheets()
                if not clientes_restantes:
                    log.info("\n¡Todos los clientes han sido reservados!")
                    break
                else:
                    log.info(f"Quedan {len(clientes_restantes)} clientes pendientes")
                    continue
            else:
                log.info(f"Sin turnos. Reintentando en {intervalo}s...")
                time.sleep(intervalo)

    # ------------------------------------------------------------------
    # MODO DEBUG
    # ------------------------------------------------------------------
    def modo_debug(self):
        """Corre un intento completo mostrando todo paso a paso."""
        clientes = leer_clientes_google_sheets()
        if not clientes:
            log.info("No hay clientes pendientes en Google Sheets.")
            return

        cliente = clientes[0]
        log.info("=" * 50)
        log.info("MODO DEBUG")
        log.info(f"Cliente: {cliente.nombre}")
        log.info(f"Email: {cliente.email}")
        log.info(f"Móvil: {cliente.movil}")
        log.info(f"Pasaporte: {cliente.pasaporte}")
        log.info(f"Nacimiento: {cliente.nacimiento}")
        log.info(f"Trámite: {cliente.tramite}")
        log.info("=" * 50)

        self.navegar_al_widget()
        self.tomar_screenshot("01_inicio")
        time.sleep(2)

        self.click_continuar()
        self.tomar_screenshot("02_continuar")
        time.sleep(2)

        log.info(f"URL: {self.page.url}")

        self.seleccionar_servicio()
        self.tomar_screenshot("03_servicio")
        time.sleep(2)

        log.info(f"URL: {self.page.url}")
        texto = self.page.inner_text("body")
        log.info(f"Contenido:\n{texto[:500]}")

        disponible = self.buscar_y_seleccionar_horario()
        self.tomar_screenshot("04_horario")
        log.info(f"¿Hay horario? {disponible}")

        if disponible and "#signupfirstappointment" in self.page.url:
            log.info("\n--- FORMULARIO ---")
            self.llenar_formulario(cliente)
            self.tomar_screenshot("05_formulario")

            log.info("\nModo debug: NO se confirma la reserva")
            log.info("Para reservar de verdad, usá modo monitoreo (opción 2)")

        log.info("\nDebug completado.")


# ============================================================================
# PUNTO DE ENTRADA
# ============================================================================

def main():
    bot = CitaBot()

    try:
        # Verificar conexión con Google Sheets
        log.info("Verificando conexión con Google Sheets...")
        clientes = leer_clientes_google_sheets()
        if clientes:
            log.info(f"\nSe encontraron {len(clientes)} clientes pendientes:")
            for i, c in enumerate(clientes, 1):
                log.info(f"  {i}. {c.nombre} - {c.email}")
        else:
            log.info("No hay clientes pendientes por ahora.")
            log.info("El bot va a seguir monitoreando por si se agregan nuevos clientes.")

        bot.iniciar_browser()

        # Arrancar directo en modo monitoreo
        log.info("\nIniciando modo MONITOREO automático...")
        log.info("El bot reserva solo si hay clientes pendientes en la hoja.")
        log.info("Para detener: cerrá esta ventana o presioná Ctrl+C\n")
        bot.monitorear(intervalo=POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("\nDetenido (Ctrl+C)")
    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
    finally:
        if os.environ.get("RAILWAY_ENVIRONMENT"):
            log.info("Cerrando bot...")
        else:
            input("\nPresioná Enter para cerrar...")
        bot.cerrar_browser()


if __name__ == "__main__":
    main()
