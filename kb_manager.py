"""
Knowledge Base Manager - RH Tramites Consulares
=================================================

Modulo central para CRUD de tramites, cache thread-safe,
regeneracion de README, audit log y feedback.

Toda mutacion a la base de datos pasa por este modulo.
Toda lectura de datos se hace a traves de este modulo.
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

log = logging.getLogger("KB")

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
TRAMITES_JSON = SCRIPT_DIR / "tramites_db.json"
TRAMITES_MD = SCRIPT_DIR / "README_TRAMITES.md"
FEEDBACK_FILE = SCRIPT_DIR / "feedback.json"
CAMBIOS_LOG_FILE = SCRIPT_DIR / "cambios_log.json"

MAX_FEEDBACK_ENTRIES = 1000

# ============================================================================
# CACHE THREAD-SAFE
# ============================================================================

_lock = threading.Lock()
_tramites_data: dict | None = None
_system_prompt: str | None = None


def get_tramites_data() -> dict:
    """Retorna los datos de tramites (con cache lazy, thread-safe)."""
    global _tramites_data
    with _lock:
        if _tramites_data is None:
            _tramites_data = _cargar_json(TRAMITES_JSON, default={"categorias": []})
            log.info(f"tramites_db.json cargado ({len(_tramites_data.get('categorias', []))} categorias)")
        return _tramites_data


def get_system_prompt() -> str:
    """Retorna el system prompt con README + correcciones recientes (cache lazy)."""
    global _system_prompt
    with _lock:
        if _system_prompt is None:
            _system_prompt = _construir_system_prompt()
            log.info(f"System prompt construido ({len(_system_prompt)} chars)")
        return _system_prompt


def invalidar_cache():
    """Invalida el cache. La proxima lectura recarga de disco."""
    global _tramites_data, _system_prompt
    with _lock:
        _tramites_data = None
        _system_prompt = None
    log.info("Cache invalidado")


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

def _construir_system_prompt() -> str:
    """Construye el system prompt completo."""
    contenido_tramites = ""
    if TRAMITES_MD.exists():
        with open(TRAMITES_MD, "r", encoding="utf-8") as f:
            contenido_tramites = f.read()

    prompt = f"""Eres el asistente virtual de RH Tramites Consulares, una empresa que gestiona
citas y tramites en el Consulado General de Espana en Montevideo, Uruguay.

Tu rol:
- Responder preguntas sobre tramites consulares de forma clara y precisa
- Informar sobre documentos necesarios, procedimientos y requisitos
- Ser amable, profesional y responder siempre en espanol
- Si no sabes algo o la informacion no esta en la base de datos, indicarlo honestamente
- Nunca inventar requisitos o procedimientos
- Cuando sea relevante, mencionar que el cliente puede contactar a RH Tramites Consulares
  por WhatsApp al +598 91 090 980 para gestion de citas

IMPORTANTE: Toda tu informacion proviene de la siguiente base de datos oficial:

{contenido_tramites}

Responde de forma concisa pero completa. Usa listas cuando enumeres documentos.
Si la pregunta no esta relacionada con tramites consulares, indica amablemente que
solo puedes ayudar con temas consulares."""

    # Inyectar correcciones recientes
    correcciones = obtener_correcciones_recientes(limit=10)
    if correcciones:
        prompt += "\n\nCORRECCIONES RECIENTES (usa esta info para corregir respuestas anteriores):\n"
        for c in correcciones:
            prompt += f"- Pregunta: {c.get('pregunta', '')}\n  Correccion: {c.get('correccion', '')}\n"

    return prompt


# ============================================================================
# UTILIDADES DE ARCHIVOS
# ============================================================================

def _cargar_json(path: Path, default=None):
    """Carga un archivo JSON. Retorna default si no existe o hay error."""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Error leyendo {path.name}: {e}")
    return default if default is not None else {}


def _guardar_json(path: Path, data):
    """Guarda datos a un archivo JSON."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Error escribiendo {path.name}: {e}")
        raise


def _buscar_tramite_por_id(tramite_id: str, data: dict = None):
    """
    Busca un tramite por ID. Retorna (categoria, tramite) o None.
    IMPORTANTE: llamar dentro del _lock si se usa data compartida.
    """
    if data is None:
        data = _cargar_json(TRAMITES_JSON, default={"categorias": []})

    for categoria in data.get("categorias", []):
        for tramite in categoria.get("tramites", []):
            if tramite.get("id") == tramite_id:
                return (categoria, tramite)
    return None


def _buscar_categoria(nombre: str, data: dict):
    """Busca categoria por nombre (case-insensitive, partial match)."""
    nombre_lower = nombre.lower().strip()
    for cat in data.get("categorias", []):
        if nombre_lower in cat.get("nombre", "").lower():
            return cat
    return None


def _listar_categorias(data: dict) -> list:
    """Retorna lista de nombres de categorias."""
    return [cat.get("nombre", "") for cat in data.get("categorias", [])]


# ============================================================================
# CRUD: AGREGAR INFO
# ============================================================================

def agregar_info(categoria_nombre: str, info: dict, usuario: str) -> dict:
    """
    Agrega un nuevo tramite a una categoria.

    info debe contener al menos: {"nombre": "...", "descripcion": "..."}
    Campos opcionales: documentos, procedimiento, notas, keywords, quien_puede_solicitarlo

    Retorna: {"ok": True, "id": "X.Y"} o {"ok": False, "error": "..."}
    """
    with _lock:
        data = _cargar_json(TRAMITES_JSON, default={"categorias": []})

        categoria = _buscar_categoria(categoria_nombre, data)
        if not categoria:
            categorias_validas = _listar_categorias(data)
            return {
                "ok": False,
                "error": f"Categoria '{categoria_nombre}' no encontrada. "
                         f"Categorias validas: {', '.join(categorias_validas)}"
            }

        # Validar campos requeridos
        nombre = info.get("nombre", "").strip()
        descripcion = info.get("descripcion", "").strip()
        if not nombre or not descripcion:
            return {"ok": False, "error": "Se requiere 'nombre' y 'descripcion'"}

        # Generar nuevo ID
        cat_id = categoria["id"]
        tramites_existentes = categoria.get("tramites", [])
        if tramites_existentes:
            ultimo_id = tramites_existentes[-1].get("id", f"{cat_id}.0")
            try:
                ultimo_num = int(ultimo_id.split(".")[-1])
            except ValueError:
                ultimo_num = len(tramites_existentes)
            nuevo_num = ultimo_num + 1
        else:
            nuevo_num = 1
        nuevo_id = f"{cat_id}.{nuevo_num}"

        # Construir nuevo tramite
        nuevo_tramite = {
            "id": nuevo_id,
            "nombre": nombre,
            "descripcion": descripcion,
            "quien_puede_solicitarlo": info.get("quien_puede_solicitarlo", None),
            "documentos": info.get("documentos", []),
            "procedimiento": info.get("procedimiento", ""),
            "notas": info.get("notas", []),
            "keywords": info.get("keywords", [kw.lower() for kw in nombre.split()])
        }

        categoria.setdefault("tramites", []).append(nuevo_tramite)

        # Persistir
        try:
            _guardar_json(TRAMITES_JSON, data)
            _regenerar_readme(data)
            _registrar_cambio("agregar", nuevo_id, usuario, {"nombre": nombre})
        except Exception as e:
            return {"ok": False, "error": f"Error guardando: {e}"}

        # Invalidar cache
        _invalidar_sin_lock()

        log.info(f"Tramite agregado: {nuevo_id} - {nombre} (por {usuario})")
        return {"ok": True, "id": nuevo_id}


# ============================================================================
# CRUD: CORREGIR TRAMITE
# ============================================================================

CAMPOS_EDITABLES = {"descripcion", "procedimiento", "quien_puede_solicitarlo", "documentos", "keywords", "notas", "nombre"}


def corregir_tramite(tramite_id: str, correccion: dict, usuario: str) -> dict:
    """
    Actualiza campos de un tramite existente.

    correccion: dict con los campos a modificar. Ej: {"descripcion": "nuevo texto"}
    Solo se permiten campos editables.

    Retorna: {"ok": True} o {"ok": False, "error": "..."}
    """
    with _lock:
        data = _cargar_json(TRAMITES_JSON, default={"categorias": []})

        resultado = _buscar_tramite_por_id(tramite_id, data)
        if not resultado:
            return {"ok": False, "error": f"Tramite '{tramite_id}' no encontrado"}

        _, tramite = resultado

        # Validar campos
        campos_invalidos = set(correccion.keys()) - CAMPOS_EDITABLES
        if campos_invalidos:
            return {
                "ok": False,
                "error": f"Campos no editables: {', '.join(campos_invalidos)}. "
                         f"Campos validos: {', '.join(CAMPOS_EDITABLES)}"
            }

        # Aplicar correccion
        cambios_detalle = {}
        for campo, valor in correccion.items():
            viejo = tramite.get(campo)
            tramite[campo] = valor
            cambios_detalle[campo] = {"antes": viejo, "despues": valor}

        # Persistir
        try:
            _guardar_json(TRAMITES_JSON, data)
            _regenerar_readme(data)
            _registrar_cambio("corregir", tramite_id, usuario, cambios_detalle)
        except Exception as e:
            return {"ok": False, "error": f"Error guardando: {e}"}

        _invalidar_sin_lock()

        log.info(f"Tramite corregido: {tramite_id} campos={list(correccion.keys())} (por {usuario})")
        return {"ok": True}


# ============================================================================
# CRUD: AGREGAR NOTA
# ============================================================================

def agregar_nota(tramite_id: str, nota: str, usuario: str) -> dict:
    """
    Agrega una nota a un tramite existente.

    Retorna: {"ok": True} o {"ok": False, "error": "..."}
    """
    nota = nota.strip()
    if not nota:
        return {"ok": False, "error": "La nota no puede estar vacia"}

    with _lock:
        data = _cargar_json(TRAMITES_JSON, default={"categorias": []})

        resultado = _buscar_tramite_por_id(tramite_id, data)
        if not resultado:
            return {"ok": False, "error": f"Tramite '{tramite_id}' no encontrado"}

        _, tramite = resultado
        tramite.setdefault("notas", []).append(nota)

        try:
            _guardar_json(TRAMITES_JSON, data)
            _regenerar_readme(data)
            _registrar_cambio("nota", tramite_id, usuario, {"nota": nota})
        except Exception as e:
            return {"ok": False, "error": f"Error guardando: {e}"}

        _invalidar_sin_lock()

        log.info(f"Nota agregada a {tramite_id} (por {usuario})")
        return {"ok": True}


# ============================================================================
# REGENERAR README DESDE JSON
# ============================================================================

def _regenerar_readme(data: dict):
    """Regenera README_TRAMITES.md desde la estructura JSON."""
    lineas = []
    lineas.append("BASE DE DATOS DE TRÁMITES CONSULARES")
    lineas.append(f"Consulado General de España en Montevideo, Uruguay")
    lineas.append("=" * 21)

    for cat in data.get("categorias", []):
        cat_id = cat.get("id", "")
        cat_nombre = cat.get("nombre", "").upper()
        lineas.append(f"CATEGORÍA {cat_id}: {cat_nombre}")

        for tramite in cat.get("tramites", []):
            t_id = tramite.get("id", "")
            t_nombre = tramite.get("nombre", "").upper()
            lineas.append(f"{t_id} {t_nombre}")

            if tramite.get("descripcion"):
                lineas.append(f"Descripción: {tramite['descripcion']}")

            if tramite.get("quien_puede_solicitarlo"):
                lineas.append(f"Quién puede solicitarlo: {tramite['quien_puede_solicitarlo']}")

            if tramite.get("documentos"):
                lineas.append("Documentos necesarios:")
                for doc in tramite["documentos"]:
                    lineas.append(doc)

            if tramite.get("procedimiento"):
                lineas.append(f"Procedimiento: {tramite['procedimiento']}")

            if tramite.get("notas"):
                for nota in tramite["notas"]:
                    lineas.append(f"Nota importante: {nota}")

            lineas.append("")  # Linea en blanco entre tramites

        lineas.append("=" * 21)

    with open(TRAMITES_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lineas))

    log.info("README_TRAMITES.md regenerado")


# ============================================================================
# AUDIT LOG
# ============================================================================

def _registrar_cambio(accion: str, tramite_id: str, usuario: str, detalles: dict):
    """Registra un cambio en el log de auditoria."""
    cambios = _cargar_json(CAMBIOS_LOG_FILE, default=[])

    cambios.append({
        "timestamp": datetime.now().isoformat(),
        "accion": accion,
        "tramite_id": tramite_id,
        "usuario": usuario,
        "detalles": detalles
    })

    # Limitar a ultimos 500 cambios
    if len(cambios) > 500:
        cambios = cambios[-500:]

    _guardar_json(CAMBIOS_LOG_FILE, cambios)


# ============================================================================
# FEEDBACK
# ============================================================================

def guardar_feedback(pregunta: str, respuesta: str, correccion: str, usuario: str) -> dict:
    """Guarda feedback de un usuario."""
    feedback_list = _cargar_json(FEEDBACK_FILE, default=[])

    feedback_id = str(uuid.uuid4())[:8]
    feedback_list.append({
        "id": feedback_id,
        "timestamp": datetime.now().isoformat(),
        "pregunta": pregunta,
        "respuesta": respuesta[:500],  # Truncar respuesta larga
        "correccion": correccion,
        "usuario": usuario,
        "estado": "pendiente"
    })

    # Limitar
    if len(feedback_list) > MAX_FEEDBACK_ENTRIES:
        feedback_list = feedback_list[-MAX_FEEDBACK_ENTRIES:]

    _guardar_json(FEEDBACK_FILE, feedback_list)
    log.info(f"Feedback guardado: {feedback_id} (por {usuario})")
    return {"ok": True, "id": feedback_id}


def obtener_feedback_pendiente() -> list:
    """Retorna feedback con estado 'pendiente'."""
    feedback_list = _cargar_json(FEEDBACK_FILE, default=[])
    return [f for f in feedback_list if f.get("estado") == "pendiente"]


def obtener_correcciones_recientes(limit: int = 10) -> list:
    """Retorna las ultimas correcciones aplicadas."""
    feedback_list = _cargar_json(FEEDBACK_FILE, default=[])
    aplicados = [f for f in feedback_list if f.get("estado") == "aplicado"]
    return aplicados[-limit:]


def marcar_feedback(feedback_id: str, nuevo_estado: str) -> dict:
    """
    Actualiza el estado de un feedback.
    Estados validos: 'aplicado', 'descartado', 'pendiente'
    """
    feedback_list = _cargar_json(FEEDBACK_FILE, default=[])

    for entry in feedback_list:
        if entry.get("id") == feedback_id:
            entry["estado"] = nuevo_estado
            entry["actualizado_en"] = datetime.now().isoformat()
            _guardar_json(FEEDBACK_FILE, feedback_list)
            log.info(f"Feedback {feedback_id} -> {nuevo_estado}")
            return {"ok": True}

    return {"ok": False, "error": f"Feedback '{feedback_id}' no encontrado"}


# ============================================================================
# INVALIDACION INTERNA (sin lock, para usar dentro de with _lock)
# ============================================================================

def _invalidar_sin_lock():
    """Invalida cache sin adquirir lock (usar solo dentro de 'with _lock')."""
    global _tramites_data, _system_prompt
    _tramites_data = None
    _system_prompt = None
