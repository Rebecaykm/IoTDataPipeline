import time as system_time
from datetime import datetime, time, timedelta, date
from collections import defaultdict
import asyncio, hashlib, pyodbc
import functools
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Tuple
import threading
from concurrent.futures import ThreadPoolExecutor
import re
import traceback
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

import signal

# Lógica de negocio pura (sin PLC ni BD). Ver domain/__init__.py
from domain.contadores import calcular_incremento
from domain import turnos as _turnos

# Comportamiento por área. Agregar un área = una clase + una línea. Ver areas/
from areas import obtener_pipeline, ContextoArea

# Decodificación de lo que manda el PLC. Ver plc/
from plc.decodificador import decodificar_bloque, parse_tag
from plc.cliente import ClienteMC, RespuestaInvalida

# Observabilidad: estado clasificado, métricas y servidor HTTP. Ver observabilidad/
from observabilidad import estado as obs_estado
from observabilidad import metricas as obs_metricas
from observabilidad import servidor as obs_servidor
from observabilidad.estado import REGISTRO, motivo_de_error
from observabilidad.rechazos import RechazosStore
from observabilidad.lecturas import LECTURAS

# Acceso a datos. El SQL vive en persistence/: repositorio.py lo general y
# ordenes.py todo lo de shop orders (buscar, llenar, cerrar).
from persistence import repositorio as repo
from persistence import ordenes
from persistence import estado as estado_store
from persistence import catalogo
from persistence.errores import es_interbloqueo
from persistence.repositorio import obtener_part_number_id

# ═══════════════════════════ CONFIGURACIÓN GLOBAL ═══════════════════════════

# Cargar variables del entorno desde .env
load_dotenv()

# Directorios
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
CSV_DIR = Path("part_numbers_not_found")
CSV_DIR.mkdir(exist_ok=True)
STATE_DIR = Path("state_cache")
STATE_DIR.mkdir(exist_ok=True)

# Los manejadores (archivo y consola) se ponen en configurar_logging(), al
# arrancar el servicio: no al importar este módulo.
logger = logging.getLogger("supervisor")
logger.setLevel(logging.INFO)
logger.propagate = False

# Rotación de logs: 10 MB por archivo y 7 respaldos = 80 MB por estación.
# Con 109 estaciones son ~8.5 GB de tope; antes eran 100 MB x 2 = 300 MB por
# estación, o sea 32 GB, y un disco lleno detiene el recolector.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_RESPALDOS = 7

#: Cuánto se conserva la línea base de una parte ausente CUANDO su lado
#: todavía no tiene observación propia (ver IPDataProcessor._lado_protegido).
#: Con observación, olvidarla es seguro de inmediato: el registro del lado le
#: da la base correcta en cuanto la parte vuelva, sin esperar nada. Esta
#: ventana solo cubre el caso sin esa red —un lado recién arrancado que aún no
#: se ha leído, o uno cuyo PLC no responde—, así que sigue siendo un turno
#: completo de margen.
VENTANA_OLVIDO_LINEA_BASE = 12 * 3600   # segundos

#: Cuánto se conserva una parte al ARRANCAR el servicio, sin importar nada más.
#: Las dos ventanas de arriba solo corren dentro de _process_estacion, y esa
#: función no se vuelve a llamar para una estación cuyo PLC dejó de mandar
#: paquetes del todo: sus partes se quedarían en el archivo de estado para
#: siempre. Esta purga corre una sola vez, al cargar el estado, y es la única
#: red para ese caso.
VENTANA_PURGA_ARRANQUE = 24 * 3600   # segundos

#: Intentos ante un interbloqueo de SQL Server. El propio motor lo pide:
#: "Rerun the transaction". Darlo por perdido al primer intento cuesta la
#: producción de ese ciclo, porque la transacción se revierte entera.
BD_INTENTOS_INTERBLOQUEO = 3
BD_ESPERA_REINTENTO = 0.25   # segundos, multiplicados por el número de intento

FORMATO_LOG = '%(asctime)s - %(levelname)s - %(message)s'


def configurar_logging():
    """
    Log del supervisor a logs/supervisor.log (con rotación) y a la consola.

    Se llama al arrancar el servicio (ver main), NO al importar el módulo: las
    pruebas importan Prensas, y con los manejadores puestos al importar cada
    corrida de pruebas escribía en el mismo logs/supervisor.log del servicio,
    mezclando líneas de prueba con las reales.
    """
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        return
    archivo = RotatingFileHandler(
        LOGS_DIR / "supervisor.log",
        mode='a',
        encoding='utf-8',
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_RESPALDOS,
    )
    for manejador in (archivo, logging.StreamHandler()):
        manejador.setLevel(logging.INFO)
        manejador.setFormatter(logging.Formatter(FORMATO_LOG))
        logger.addHandler(manejador)

station_loggers = {}
# Lo piden a la vez el hilo de los PLC, el de catálogo y el de BD: sin
# candado, dos hilos podían crear el mismo logger y escribir cada línea dos
# veces, con dos manejadores rotando el mismo archivo.
_station_loggers_lock = threading.Lock()

# Intervalo (en segundos) para volver a leer la configuración de la BD
POLL_INTERVAL = 60  # 🚀 Aumentado a 60s (se puede forzar update manual)

# Bitácora de rechazos en JSONL (reemplaza el CSV que se releía con pandas)
RECHAZOS = RechazosStore(CSV_DIR / "parts_not_found.jsonl")
HTTP_PORT = int(os.getenv("HTTP_PORT", "9100"))

#  CONFIGURACIÓN GLOBAL DE CONEXIÓN PLC
PLC_CONNECTION_TIMEOUT = 15   # segundos para el connect() inicial

#: Segundos que se le dan al PLC para responder una lectura. Se pasa a
#: ClienteMC.setaccessopt(timer_sec=...), que es quien sabe traducirlo: el PLC
#: recibe timer_sec*4 en unidades de 250ms (su propio aviso de "ya me tardé"),
#: y el socket espera timer_sec+1 —un segundo más, el margen que pide la
#: propia librería para que esa respuesta de timeout le dé tiempo de llegar.
#: Antes los dos se igualaban al mismo número crudo (plc.timer=10,
#: plc.soc_timeout=10): el PLC avisaba a los 2.5s pero el socket seguía
#: esperando hasta los 10, así que ese aviso nunca llegaba a tiempo de nada.
PLC_READ_TIMEOUT_SEC = 3

#: Espera entre reconexiones fallidas: arranca aquí y se DUPLICA en cada
#: fallo sucesivo (ver plc_reader), hasta el tope de abajo. Antes era fija:
#: con el PLC realmente caído, martillar cada 10s para siempre no acelera
#: nada y solo llena el log. Se reinicia en cuanto una conexión prospera.
RECONNECT_DELAY = 10
RECONNECT_DELAY_MAX = 60

# ═══════════════════════════ POOLS DE HILOS (I/O BLOQUEANTE) ═══════════════════════════
# pymcprotocol y pyodbc son bloqueantes: si se llaman directo desde una corrutina
# congelan el event loop entero y con él la lectura de TODOS los PLCs.
# Estos executors los sacan del loop. Las conexiones se indexan por hilo, así que
# dos hilos nunca comparten una conexión (ver ConnectionPool).
PLC_IO_WORKERS = int(os.getenv('PLC_IO_WORKERS', '16'))  # 1 hilo por PLC concurrente
DB_WORKERS = int(os.getenv('DB_WORKERS', '8'))           # limita conexiones SQL vivas

_plc_executor = ThreadPoolExecutor(max_workers=PLC_IO_WORKERS, thread_name_prefix="plc-io")
_db_executor = ThreadPoolExecutor(max_workers=DB_WORKERS, thread_name_prefix="db")

#: Traducción de lo que manda el PLC cuando hay que consultar el catálogo (el
#: MDI de Estampado). Aparte de _db_executor para que no espere detrás del
#: registro de producción, ni el registro detrás de ella.
CATALOGO_WORKERS = int(os.getenv('CATALOGO_WORKERS', '4'))
_catalogo_executor = ThreadPoolExecutor(max_workers=CATALOGO_WORKERS,
                                        thread_name_prefix="catalogo")

# ═══════════════════════════ POOL DE CONEXIONES ═══════════════════════════

def _cerrar_sin_error(conn):
    """Cerrar una conexión que puede estar ya muerta no debe tumbar a nadie."""
    try:
        conn.close()
    except Exception:
        pass


class ConnectionPool:
    """
    Una conexión a SQL Server POR HILO, reutilizada mientras sirva.

    Por hilo y no global: pyodbc no admite usar una conexión desde dos hilos a
    la vez, y con una sola compartida los commit de una estación confirmaban
    el trabajo a medias de otra. El total queda acotado por los executors.

    La conexión NO se prueba antes de entregarla. Antes cada petición lanzaba
    un SELECT 1 —una consulta extra por estación por segundo— para descubrir
    una conexión muerta. Ahora quien recibe un error del driver la descarta
    (ver descartar_conexion) y la siguiente petición abre otra: una conexión
    caída cuesta un ciclo fallido, que no pierde nada porque sin commit la
    línea base no se mueve.
    """

    _conexiones = {}            # {id de hilo: (conexión, último uso)}
    _lock = threading.Lock()
    _intervalo_limpieza = 300   # segundos entre revisiones de inactivas
    _max_inactiva = 600         # segundos sin uso antes de cerrarla
    _ultima_limpieza = system_time.monotonic()

    @classmethod
    def obtener(cls):
        """La conexión de este hilo, o una nueva. None si no se pudo conectar."""
        hilo = threading.get_ident()
        ahora = system_time.monotonic()
        with cls._lock:
            if ahora - cls._ultima_limpieza > cls._intervalo_limpieza:
                cls._cerrar_inactivas(ahora)
                cls._ultima_limpieza = ahora
            entrada = cls._conexiones.get(hilo)
            if entrada:
                cls._conexiones[hilo] = (entrada[0], ahora)
                return entrada[0]

        # Conectar puede tardar todo el Connect Timeout: fuera del candado,
        # para que una base lenta no detenga a los hilos que ya tienen la suya.
        conn = cls._conectar()
        if conn is not None:
            with cls._lock:
                cls._conexiones[hilo] = (conn, ahora)
        return conn

    @classmethod
    def descartar(cls):
        """Cierra la conexión de este hilo; la próxima petición abre otra."""
        with cls._lock:
            entrada = cls._conexiones.pop(threading.get_ident(), None)
        if entrada:
            _cerrar_sin_error(entrada[0])

    @classmethod
    def cerrar_todas(cls):
        """Para el apagado del servicio."""
        with cls._lock:
            conexiones = [conn for conn, _ in cls._conexiones.values()]
            cls._conexiones.clear()
        for conn in conexiones:
            _cerrar_sin_error(conn)
        logger.info("🔒 Todas las conexiones del pool cerradas")

    @classmethod
    def _cerrar_inactivas(cls, ahora):
        """Se llama con el candado tomado."""
        for hilo, (conn, uso) in list(cls._conexiones.items()):
            if ahora - uso > cls._max_inactiva:
                del cls._conexiones[hilo]
                _cerrar_sin_error(conn)

    @staticmethod
    def _conectar():
        server = os.getenv('DB_SERVER')
        database = os.getenv('DB_NAME')
        username = os.getenv('DB_USER')
        password = os.getenv('DB_PASSWORD')
        if not all([server, database, username, password]):
            return None
        try:
            return pyodbc.connect(
                f'DRIVER={{ODBC Driver 17 for SQL Server}};'
                f'SERVER={server};'
                f'DATABASE={database};'
                f'UID={username};'
                f'PWD={password};'
                f'Connect Timeout=10;'
            )
        except Exception:
            return None

# ═══════════════════════════ CONFIGURACIÓN DE TURNOS ═══════════════════════════

# Variable global para configuración de turnos
SHIFTS_CONFIG = {}

def load_shifts_config() -> Dict[int, Dict]:
    """Carga configuración de turnos desde la base de datos"""
    conn = create_connection()
    if conn is None:
        logger.warning("⚠️ No se pudo conectar para cargar turnos")
        return {}

    try:
        cursor = conn.cursor()
        sql = """
        SELECT id, name, start_time, end_time 
        FROM shifts 
        ORDER BY start_time
        """
        cursor.execute(sql)
        rows = cursor.fetchall()

        if not rows:
            logger.error("❌ No se encontraron turnos activos en BD")
            return {}

        shifts = {}
        for shift_id, name, start_time, end_time in rows:
            # Convertir datetime.time de pyodbc a datetime.time de Python
            if isinstance(start_time, str):
                start_time = datetime.strptime(start_time, "%H:%M:%S").time()
            if isinstance(end_time, str):
                end_time = datetime.strptime(end_time, "%H:%M:%S").time()

            shifts[shift_id] = {
                'start': start_time,
                'end': end_time,
                'name': name
            }

        logger.debug(f"Turnos leídos: {len(shifts)}")
        return shifts

    except Exception as e:
        logger.error(f"❌ Error cargando turnos: {e}")
        logger.error(traceback.format_exc())
        descartar_conexion()
        return {}

def _firma_turnos(cfg):
    """Firma comparable de la configuración de turnos."""
    return tuple(sorted(
        (sid, d['start'].strftime('%H:%M'), d['end'].strftime('%H:%M'))
        for sid, d in (cfg or {}).items()
    ))

def refresh_shifts_config():
    """
    Recarga los turnos y actualiza la variable global.

    Corre en cada ciclo del supervisor (60 s) para que un cambio de horario en
    la BD se tome solo, sin botón. Por eso SOLO registra en el log cuando algo
    cambió de verdad: si no, serían ~5 760 líneas diarias sin información.
    """
    global SHIFTS_CONFIG
    new_config = load_shifts_config()

    if new_config:
        cambio = _firma_turnos(new_config) != _firma_turnos(SHIFTS_CONFIG)
        SHIFTS_CONFIG = new_config
        if cambio:
            logger.info(f"🔄 Turnos actualizados desde BD: {len(new_config)} turno(s)")
            for shift_id, data in sorted(new_config.items()):
                logger.info(
                    f"   Turno {shift_id} ({data['name']}): "
                    f"{data['start'].strftime('%H:%M')} - {data['end'].strftime('%H:%M')}"
                )
        return True
    else:
        logger.error("❌ No se pudo actualizar configuración de turnos")
        return False

def get_current_shift(current_time: time) -> Tuple[int, date]:
    """
    Determina el turno y fecha planificada basado en la hora actual.

    Args:
        current_time: Hora actual

    Returns:
        Tuple[turno, fecha_plan]
    """
    # La lógica vive en domain/turnos.py; aquí solo se le pasa el estado del
    # entorno (configuración cargada de la BD y la fecha de hoy).
    return _turnos.get_current_shift(current_time, SHIFTS_CONFIG, date.today())

def safe_get_current_shift(current_time: time) -> Tuple[int, date]:
    """
    Versión segura que maneja cambios en configuración de turnos.

    Args:
        current_time: Hora actual

    Returns:
        Tuple[turno, fecha_plan]
    """
    try:
        return get_current_shift(current_time)
    except Exception as e:
        logger.error(f"❌ Error calculando turno: {e}")
        # Valores por defecto como fallback
        if time(8, 0) <= current_time < time(20, 0):
            return 1, date.today()
        else:
            return 2, date.today() if current_time >= time(20, 0) else date.today() - timedelta(days=1)

def registrar_error_validacion(estacion, numero_original, tipo_error,
                               lado="--", area="", mdi=None):
    """
    Anota un número de parte rechazado.

    Antes escribía a CSV releyendo el archivo COMPLETO con pandas en cada
    rechazo, dentro del hilo que atiende los PLCs. Ahora es una línea JSONL con
    el índice de duplicados en memoria. Ver observabilidad/rechazos.py
    """
    escribio = RECHAZOS.registrar(estacion, numero_original, tipo_error,
                                  lado=lado, area=area, mdi=mdi)
    try:
        obs_metricas.partes_rechazadas.labels(estacion, tipo_error).inc()
    except Exception:
        pass
    return escribio

# ═══════════════════════════ HELPERS LOGGING ═══════════════════════════

def get_station_logger(estacion):
    """
    Logger propio de la estación: solo a su archivo logs/<estación>.log, con
    rotación, sin consola y sin propagar al supervisor.
    """
    existente = station_loggers.get(estacion)
    if existente:
        return existente

    with _station_loggers_lock:
        if estacion in station_loggers:
            return station_loggers[estacion]

        station_logger = logging.getLogger(f"station.{estacion}")
        station_logger.setLevel(logging.INFO)
        station_logger.propagate = False

        file_handler = RotatingFileHandler(
            LOGS_DIR / f"{estacion}.log",
            mode='a',
            encoding='utf-8',
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_RESPALDOS,
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(FORMATO_LOG))
        station_logger.addHandler(file_handler)

        station_loggers[estacion] = station_logger
        return station_logger

# ═══════════════════════════ CONEXIONES BD ═══════════════════════════

def create_connection():
    """La conexión SQL de este hilo (ver ConnectionPool)."""
    return ConnectionPool.obtener()


def descartar_conexion():
    """
    Tras un error del driver: la próxima petición de este hilo abre una
    conexión nueva en vez de seguir usando una que puede estar muerta.
    """
    ConnectionPool.descartar()

# ═══════════════════════════ FUNCIONES BASE DE DATOS (COMUNES) ═══════════════════════════

def load_config():
    """Carga configuración incluyendo el NOMBRE DEL ÁREA"""
    conn = create_connection()
    if conn is None:
        logger.warning("⚠️ No se pudo establecer conexión para cargar configuración")
        return {}

    try:
        sql = """
        SELECT
          wc.name          AS work_center_name,
          wc.ip            AS work_center_ip,
          tt.name          AS tag_type_name,
          t.address        AS tag_address,
          t.long           AS tag_long,
          a.name           AS area_name
        FROM work_centers wc
        JOIN tags t        ON wc.id = t.work_center_id
        JOIN tag_types tt  ON t.tag_type_id = tt.id
        LEFT JOIN lines l  ON wc.line_id = l.id
        LEFT JOIN areas a  ON l.area_id = a.id
        """
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()

        if not rows:
            logger.warning("⚠️ No se encontraron configuraciones en la base de datos")
            return {}

        ip_groups = defaultdict(lambda: {
            'estaciones': [], 'port': 1025, 'serie': 'Q',
            'all_addresses': set(), 'station_configs': {}, 'area': 'Default'
        })

        for wc, ip, tag, addr, lng, area in rows:
            if not ip or not ip.strip():
                continue
            
            if area:
                ip_groups[ip]['area'] = area


            tag_lower = tag.lower()
            if tag_lower == "puerto":
                ip_groups[ip]['port'] = int(addr)
            elif tag_lower == "serie plc":
                ip_groups[ip]['serie'] = addr
            else:
                if wc not in ip_groups[ip]['station_configs']:
                    ip_groups[ip]['station_configs'][wc] = {}

                ip_groups[ip]['station_configs'][wc][tag] = {
                    "address": addr,
                    "long": int(lng)
                }
                # NUEVO: Guardar como bloque en lugar de romper direcciones en individuales
                ip_groups[ip]['all_addresses'].add((addr, int(lng)))

            if wc not in ip_groups[ip]['estaciones']:
                ip_groups[ip]['estaciones'].append(wc)

        # 🔄 Solo mostrar log si hay cambios significativos en la configuración
        config_hash = hashlib.md5(str(sorted(ip_groups.items())).encode()).hexdigest()

        if not hasattr(load_config, 'last_config_hash'):
            load_config.last_config_hash = None

        if config_hash != load_config.last_config_hash:
            logger.info(f"📋 Configuración cargada: {len(ip_groups)} IPs")
            if load_config.last_config_hash:
                logger.info("🔄 Configuración actualizada desde BD")
            load_config.last_config_hash = config_hash

        return dict(ip_groups)

    except Exception as e:
        logger.error(f"❌ Error cargando configuración: {e}")
        logger.error(traceback.format_exc())
        descartar_conexion()
        return {}

# ═══════════════════════════ CATÁLOGO DE MDI (ESTAMPADO) ═══════════════════════════

#: Cuánto se reutiliza la traducción de un MDI antes de volver a consultarla.
#: Sin caducidad, un MDI dado de alta o corregido en el catálogo seguía
#: rechazado hasta reiniciar el servicio. Un fallo caduca antes que un
#: acierto: es justo lo que alguien puede estar corrigiendo, y si fue la base
#: la que no respondió, se reintenta pronto sin martillarla cada segundo.
VIGENCIA_MDI_RESUELTO = 600   # segundos
VIGENCIA_MDI_FALLIDO = 30     # segundos

#: {(estacion, mdi): ((numeros, error), vence_en)}. Lo usan los hilos de
#: _catalogo_executor: leer y escribir una clave del dict es atómico, y una
#: carrera entre dos hilos solo cuesta repetir una consulta.
_mdi_resueltos = {}


def validar_numeros_parte_estampado(mdi: str, estacion: str, log) -> tuple:
    """
    Traduce el MDI del troquel a los números de parte de esa estación.

    Devuelve (numeros_activos, error). El error va al tablero de rechazos y
    distingue MDI_NO_EXISTE, MDI_DE_OTRA_ESTACION y PART_NUMBER_OBSOLETO del
    catálogo, y SIN_CONEXION_BD / DB_ERROR cuando no respondió la base. Ver
    persistence/catalogo.py

    Consulta la base, así que corre en _catalogo_executor y nunca en el hilo
    que lee los PLC (ver IPDataCollector.collect_and_enqueue).
    """
    clave = (estacion, mdi)
    ahora = system_time.monotonic()
    guardado = _mdi_resueltos.get(clave)
    if guardado and guardado[1] > ahora:
        return guardado[0]

    resultado = _resolver_mdi(mdi, estacion, log)
    vigencia = VIGENCIA_MDI_RESUELTO if resultado[0] else VIGENCIA_MDI_FALLIDO
    _mdi_resueltos[clave] = (resultado, ahora + vigencia)
    return resultado


def _resolver_mdi(mdi, estacion, log):
    conn = create_connection()
    if conn is None:
        log.error(f"❌ Sin conexión a BD para resolver MDI={mdi}")
        return [], "SIN_CONEXION_BD"
    try:
        with conn.cursor() as cursor:
            return catalogo.resolver_mdi(cursor, mdi, estacion, log)
    except Exception as e:
        log.error(f"❌ Error resolviendo MDI={mdi}: {e}")
        if isinstance(e, pyodbc.Error):
            descartar_conexion()
        return [], "DB_ERROR"

# ═══════════════════════════ PATRONES STRATEGY & FACTORY ═══════════════════════════

def anotar_estado(estacion, lado, motivo, **campos):
    """Deja constancia del motivo para /estaciones/estado y para las alertas."""
    try:
        REGISTRO.anotar(estacion, lado, motivo, **campos)
        obs_metricas.marcar_motivo(estacion, lado, motivo, obs_estado.MOTIVOS_POSIBLES,
                                   area=campos.get('area', ''))
    except Exception as e:
        logger.debug(f"No se pudo anotar estado de {estacion}/{lado}: {e}")


def registrar_history(cursor, pipeline, part_number_id, cantidad, fecha_fmt, tiempo, dato, log,
                      shop_order_number=None):
    """
    Inserta el detalle en histories con las columnas extra que decida el área.

    Estampado agrega `sequence` con el troquel; las demás áreas no agregan nada.
    `shop_order_number` va aparte porque no depende del área: es la orden a la
    que se atribuyó lo registrado, y es lo que permite reconciliar el detalle
    contra el plan. En la producción no planeada va nulo.

    Ojo con la unidad de `cantidad`: son GOLPES en Estampado y PIEZAS en las
    demás áreas. Es el incremento crudo del contador, sin multiplicar.
    """
    try:
        repo.insertar_history(cursor, part_number_id, cantidad, fecha_fmt, tiempo,
                              shop_order_number=shop_order_number,
                              **pipeline.extras_history(dato or {}))
    except Exception as e:
        # El interbloqueo ya revirtió la transacción entera: tragárselo aquí
        # dejaba seguir el ciclo y avanzar la línea base sobre producción que
        # ya no estaba en la base. Tiene que subir al bucle de reintentos.
        if es_interbloqueo(e):
            raise
        log.error(f"Error insert history: {e}")

# ═══════════════════════════ RECOLECTOR DINÁMICO ═══════════════════════════

class IPDataCollector:
    def __init__(self, ip, estaciones, area):
        self.ip = ip
        self.estaciones = estaciones
        self.area = area
        # El pipeline del área se resuelve una vez, no en cada lectura.
        self._pipeline = obtener_pipeline(area)
        # Última resolución con varias partes por lado, para no repetir el mismo
        # renglón en el log en cada ciclo (ver _process_station_data).
        self._ultima_division = {}
        # Lados que el PLC respondió SIN número de parte en la última lectura,
        # por estación. No son producción ni viajan al procesador: solo
        # alimentan el tablero de lecturas en vivo (ver collect_and_enqueue).
        self._sin_parte = {}

    def _parse_tag(self, tag_name):
        """Detecta tipo y lado del tag. La lógica vive en plc/decodificador.py"""
        return parse_tag(tag_name)

    def _merge_blocks(self, blocks, max_gap=15):
        """Agrupa bloques contiguos/cercanos para minimizar lecturas al PLC."""
        from collections import defaultdict

        parsed_blocks = defaultdict(list)

        for addr, lng in blocks:
            match = re.match(r'^([A-Za-z]+)([0-9A-Fa-f]+)$', addr)
            if not match:
                parsed_blocks[f"UNKNOWN_{addr}"].append({
                    'start': 0,
                    'len': lng,
                    'orig': (addr, lng)
                })
                continue

            prefix, num_str = match.groups()
            prefix = prefix.upper()
            base = 16 if prefix in ('W', 'B', 'X', 'Y') else 10
            start = int(num_str, base)

            parsed_blocks[f"{prefix}:{base}"].append({
                'start': start,
                'len': lng,
                'orig': (addr, lng)
            })

        merged_requests = []

        for group_key, items in parsed_blocks.items():
            if group_key.startswith('UNKNOWN_'):
                for item in items:
                    merged_requests.append({
                        'head': item['orig'][0],
                        'len': item['orig'][1],
                        'sub_blocks': [{'orig': item['orig'], 'offset': 0}]
                    })
                continue

            items.sort(key=lambda x: x['start'])
            current = None

            for item in items:
                if current is None:
                    current = {
                        'head': item['orig'][0],
                        'start': item['start'],
                        'end': item['start'] + item['len'] - 1,
                        'sub_blocks': [{'orig': item['orig'], 'offset': 0}]
                    }
                    continue

                gap = item['start'] - current['end'] - 1
                new_end = max(current['end'], item['start'] + item['len'] - 1)
                total_len = new_end - current['start'] + 1

                if gap <= max_gap and gap >= 0 and total_len <= 960:
                    current['end'] = new_end
                    current['sub_blocks'].append({
                        'orig': item['orig'],
                        'offset': item['start'] - current['start']
                    })
                elif item['start'] <= current['end']:
                    current['end'] = new_end
                    current['sub_blocks'].append({
                        'orig': item['orig'],
                        'offset': item['start'] - current['start']
                    })
                else:
                    merged_requests.append({
                        'head': current['head'],
                        'len': current['end'] - current['start'] + 1,
                        'sub_blocks': current['sub_blocks']
                    })
                    current = {
                        'head': item['orig'][0],
                        'start': item['start'],
                        'end': item['start'] + item['len'] - 1,
                        'sub_blocks': [{'orig': item['orig'], 'offset': 0}]
                    }

            if current is not None:
                merged_requests.append({
                    'head': current['head'],
                    'len': current['end'] - current['start'] + 1,
                    'sub_blocks': current['sub_blocks']
                })

        return merged_requests

    def _lectura_degradada(self, cfg, block_data):
        """
        True si algún bloque que usa la estación falló en la lectura (quedó en None).
        Permite distinguir "la estación dejó de producir" de "no pude leer la estación":
        en el segundo caso el estado en caché debe conservarse intacto.
        """
        for info in cfg.values():
            key = (info.get('address'), info.get('long'))
            if key in block_data and block_data[key] is None:
                return True
        return False

    def _process_station_data(self, estacion, cfg, block_data, timestamp):
        """Procesa datos de una estación específica obtenida de los bloques"""
        from collections import defaultdict

        groups_data = defaultdict(dict)

        for tag_name, info in cfg.items():
            tipo, grupo = self._parse_tag(tag_name)
            
            addr, lng = info['address'], info['long']
            # block_vals obtiene directamente el bloque leído, llenando con 0 si algo falla
            block_vals = block_data.get((addr, lng))

            if block_vals is None:

                continue

            val = None
            if tipo == "contador":
                val = block_vals[0] if block_vals else 0
            elif tipo == "tiempo":
                try:
                    val = abs(int(block_vals[0])/1000.0)
                except:
                    val = 0.0
            elif tipo == "parte":
                #  SIMPLIFICADO: Decodificar
                orig, limpios, meta = decodificar_bloque(block_vals)
                val = {'orig': orig, 'list': limpios, 'meta': meta}
            elif tipo == "troquel":
                val = block_vals[0] if block_vals else 0

            if val is not None:
                groups_data[grupo][tipo] = val

        # 🆕 NUEVO: Obtener el logger de la estación para usar en validación
        log = get_station_logger(estacion)

        datos_estacion = []
        self._sin_parte[estacion] = []
        for grp, data in groups_data.items():
            if 'contador' not in data:
                continue

            partes = data.get('parte', {'orig': '', 'list': [], 'meta': {}})
            troquel_id = data.get('troquel', None)

            # El área decide cómo se traduce lo que manda el PLC a números de
            # parte. Estampado resuelve el MDI contra el catálogo; las demás
            # expanden las alternativas con '/'. Ver areas/
            raw = partes.get('orig') or ''
            ctx_area = ContextoArea(
                estacion=estacion, log=log,
                validar_estampado=validar_numeros_parte_estampado,
            )
            nombres_parte, error_parte = self._pipeline.resolver_partes(raw, ctx_area)

            if nombres_parte:
                # Solo se registra cuando CAMBIA. Antes salía en cada ciclo:
                # con un MDI de dos partes eso son 86,400 renglones idénticos al
                # día por lado, ~10 MB que tapan lo que sí hay que ver.
                clave_division = (estacion, grp)
                if len(nombres_parte) > 1:
                    if self._ultima_division.get(clave_division) != raw:
                        self._ultima_division[clave_division] = raw
                        log.info(
                            f"🔀 {estacion}/{grp} [{self._pipeline.nombre}]: "
                            f"'{raw}' -> {nombres_parte}"
                        )
                else:
                    self._ultima_division.pop(clave_division, None)

                for p_nombre in nombres_parte:
                    datos_estacion.append({
                        'parte': p_nombre,
                        'original': partes['orig'],
                        'contador': data['contador'],
                        'tiempo': data.get('tiempo', 0.0),
                        'troquel_id': troquel_id,
                        'validado': True,
                        'error_validacion': None,
                        'lado': grp  # 🆕 Identificar el lado/grupo
                    })
            elif raw.strip():
                log.warning(
                    f"❌ {estacion}/{grp} [{self._pipeline.nombre}]: no se resolvió "
                    f"ningún número de parte para '{raw}' ({error_parte})"
                )
                datos_estacion.append({
                    'parte': None,
                    'original': partes['orig'],
                    'contador': data['contador'],
                    'tiempo': data.get('tiempo', 0.0),
                    'troquel_id': troquel_id,
                    'validado': False,
                    'error_validacion': error_parte,
                    'lado': grp  # 🆕 Identificar el lado/grupo
                })
            else:
                # El PLC respondió y el lado viene en blanco. Para el conteo no
                # es nada —por eso no entra a datos_estacion—, pero el tablero
                # de lecturas en vivo tiene que verlo o se queda mostrando el
                # último número que ese lado tuvo.
                self._sin_parte[estacion].append({
                    'lado': grp,
                    'contador': data['contador'],
                    'tiempo': data.get('tiempo', 0.0),
                    'troquel_id': troquel_id,
                })

        return datos_estacion

    async def collect_and_enqueue(self, plc, group_info, _blocks=None):
        """Método optimizado con lectura merge-batchread por bloques."""
        try:
            loop = asyncio.get_running_loop()
            blocks = _blocks if _blocks is not None else list(group_info.get('all_addresses', []))
            merged_reqs = self._merge_blocks(blocks, max_gap=15)
            block_data = {}
            for req in merged_reqs:
                head = req['head']
                length = req['len']
                try:
                    # batchread_wordunits es una llamada de socket BLOQUEANTE: se ejecuta
                    # en un hilo para no congelar la lectura del resto de los PLCs.
                    # Las lecturas de un mismo PLC siguen siendo secuenciales (await),
                    # que es lo que exige pymcprotocol.
                    vals = await loop.run_in_executor(
                        _plc_executor,
                        functools.partial(plc.batchread_wordunits, headdevice=head, readsize=length)
                    )
                    # ClienteMC garantiza la respuesta completa y del tamaño
                    # pedido, o lanza: aquí no hay lecturas a medias.
                    for sub in req['sub_blocks']:
                        _, largo = sub['orig']
                        offset = sub['offset']
                        block_data[sub['orig']] = vals[offset:offset + largo]
                except OSError as batch_error:
                    winerr = getattr(batch_error, 'winerror', None) or batch_error.errno
                    if winerr == 10054:
                        logger.warning(f"🔌 WinError 10054 leyendo '{head}' en PLC {self.ip}.")
                    elif winerr == 10061:
                        logger.warning(f"🔌 WinError 10061 leyendo '{head}' en PLC {self.ip}.")
                    else:
                        logger.warning(f"⚠️ OSError [{winerr}] leyendo '{head}' en PLC {self.ip}: {batch_error}")
                    for sub in req['sub_blocks']:
                        block_data[sub['orig']] = None
                    raise
                except Exception as batch_error:
                    logger.warning(f"Error leyendo bloque '{head}' (len {length}) en PLC {self.ip}: {batch_error}")
                    for sub in req['sub_blocks']:
                        block_data[sub['orig']] = None
            # Última lectura CRUDA para /debug/plc/<ip>: los words tal como
            # llegan, antes de decodificar. Es donde se ve si viene basura.
            try:
                obs_servidor.registrar_lectura_cruda(self.ip, ", ".join(self.estaciones), {
                    str(k): (v[:16] if isinstance(v, list) else v)
                    for k, v in block_data.items()
                })
            except Exception:
                pass

            batch = []
            now = datetime.now()
            for est in self.estaciones:
                cfg = group_info['station_configs'].get(est, {})
                # plc_ok=False marca lectura incompleta: el consumidor conserva el
                # caché en vez de cerrar registros por ausencia de datos.
                lectura_ok = not self._lectura_degradada(cfg, block_data)
                if self._pipeline.resuelve_en_catalogo:
                    # Traducir consulta la base: fuera de este hilo, que es el
                    # que lee TODOS los PLC. Con la base lenta o caída, esa
                    # espera congelaba la lectura de la planta entera.
                    datos_estacion = await loop.run_in_executor(
                        _catalogo_executor, self._process_station_data,
                        est, cfg, block_data, now)
                else:
                    datos_estacion = self._process_station_data(est, cfg, block_data, now)
                if datos_estacion:
                    batch.append({'estacion': est, 'datos': datos_estacion, 'ts': now,
                                  'area': self.area, 'plc_ok': lectura_ok})
                else:
                    if not lectura_ok:
                        try:
                            obs_metricas.plc_lecturas.labels(self.ip, "parcial").inc()
                        except Exception:
                            pass
                        logger.warning(
                            f"⚠️ Lectura incompleta de {est} en PLC {self.ip}: "
                            f"se preserva el estado en caché (no se cierran registros)."
                        )
                    batch.append({'estacion': est, 'datos': [], 'ts': now, 'area': self.area, 'plc_ok': lectura_ok})

            # Lecturas en vivo por estación/lado, con la MISMA conexión al PLC.
            # Alimenta /estaciones/lecturas: ver los valores en tiempo real sin
            # abrir una segunda conexión. Se registra lo que el PLC mandó, antes
            # de cualquier decisión de negocio: aunque la parte se rechace o el
            # contador no avance, aquí se ve.
            #
            # Un lado que respondió SIN número se anota igual, en blanco: si
            # no, la fila se quedaba con el último número y contador que tuvo
            # y parecía que el PLC lo seguía mandando. Con lectura completa,
            # los lados que ya no vienen se quitan; con lectura parcial no se
            # toca nada y la antigüedad de la fila es la que avisa.
            for pkg in batch:
                try:
                    est = pkg['estacion']
                    vistos = set()
                    for d in pkg.get('datos', []):
                        lado = d.get('lado', '--')
                        vistos.add(lado)
                        LECTURAS.anotar(
                            est, lado,
                            ip=self.ip, area=self.area,
                            numero_plc=d.get('original'),
                            numero_validado=d.get('parte'),
                            contador=d.get('contador'),
                            tiempo_ciclo=d.get('tiempo'),
                            troquel=d.get('troquel_id'),
                            validado=d.get('validado'),
                            error=d.get('error_validacion'),
                        )
                    for v in self._sin_parte.get(est, []):
                        if v['lado'] in vistos:
                            continue
                        vistos.add(v['lado'])
                        LECTURAS.anotar(
                            est, v['lado'],
                            ip=self.ip, area=self.area,
                            contador=v.get('contador'),
                            tiempo_ciclo=v.get('tiempo'),
                            troquel=v.get('troquel_id'),
                            sin_parte=True,
                        )
                    if pkg.get('plc_ok'):
                        LECTURAS.conservar_lados(est, vistos)
                except Exception:
                    pass

            if batch and self.ip in ip_data_queues:
                try:
                    ip_data_queues[self.ip].put_nowait(batch)
                except asyncio.QueueFull:
                    logger.warning(f"Cola llena para {self.ip}, descartando batch")
        except OSError:
            raise  # Ya fue logueado en el bloque interno, evitar log duplicado
        except Exception as e:
            logger.error(f"Error en collect_and_enqueue para {self.ip}: {str(e)[:100]}")
            raise
# ═══════════════════════════ PROCESADOR (CONSUMIDOR) ═══════════════════════════

class IPDataProcessor:
    def __init__(self, ip):
        self.ip = ip
        self.active_records = {}
        # Último contador visto por (ESTACIÓN, LADO), sin importar qué parte lo
        # mostraba. El registro del PLC es del lado y lo comparten las partes que
        # pasan por él; ver persistence/estado.py para por qué hace falta.
        self.contadores_lado = {}
        self.last_scanned_parts = {}  # 🆕 Polling optimization cache
        # Estaciones paradas (lectura completa, sin partes) a las que ya se les
        # cerró lo que tenían abierto. Mientras sigan así no se vuelve a tocar
        # la base: antes era un UPDATE + commit por estación por segundo, para
        # siempre. Vive solo en memoria: al reiniciar el servicio se cierra una
        # vez más, que es lo correcto si algo quedó abierto entretanto.
        self._estaciones_cerradas = set()
        self.state_file = STATE_DIR / f"state_{self.ip.replace('.', '_')}.json"
        self.load_state()

    def load_state(self):
        """Carga el estado previo. La lectura vive en persistence/estado.py"""
        self.active_records = estado_store.cargar_estado(self.state_file, self.ip)
        self.contadores_lado = estado_store.cargar_contadores_lado(
            self.state_file, self.ip)
        self._purgar_huerfanas_al_arrancar()

    def save_state(self):
        """Guarda el estado. La escritura atómica vive en persistence/estado.py"""
        estado_store.guardar_estado(self.state_file, self.active_records, self.ip,
                                    self.contadores_lado)

    def _purgar_huerfanas_al_arrancar(self):
        """
        Olvida, una sola vez al arrancar, las partes que lleven más de
        VENTANA_PURGA_ARRANQUE sin aparecer y SIN una orden abierta.

        _process_estacion solo limpia cuando llega un paquete de esa estación;
        si su PLC dejó de responder del todo, eso nunca vuelve a pasar y sus
        partes se quedarían en el archivo de estado para siempre. Esta es la
        única red para ese caso, así que corre independientemente de si el
        lado está protegido o no.

        Si la entrada tiene `id_en_curso`, NO se toca: este método solo lee
        el archivo, sin cursor ni conexión a BD, así que no puede detener esa
        orden como hace _process_estacion. Olvidar la entrada de todos modos
        dejaría la orden en EN_PROGRESO para siempre sin que nadie se entere
        —justo lo que no se debe hacer—, así que se avisa y se deja intacta
        para que alguien la revise a mano.
        """
        ahora_epoch = system_time.time()
        vencidas = [
            (clave, reg) for clave, reg in self.active_records.items()
            if ahora_epoch - reg.get('visto_en', 0) > VENTANA_PURGA_ARRANQUE
        ]
        if not vencidas:
            return

        huerfanas = [clave for clave, reg in vencidas if reg.get('id_en_curso') is None]
        con_orden_abierta = [(clave, reg['id_en_curso']) for clave, reg in vencidas
                              if reg.get('id_en_curso') is not None]

        if con_orden_abierta:
            logger.warning(
                f"⚠️ {self.ip}: {len(con_orden_abierta)} parte(s) sin aparecer hace más "
                f"de {VENTANA_PURGA_ARRANQUE / 3600:.0f}h con una orden todavía abierta; "
                f"no se olvidan solas, revisar a mano: " +
                ", ".join(f"{clave} (orden {orden})"
                          for clave, orden in sorted(con_orden_abierta))
            )

        if not huerfanas:
            return
        for clave in huerfanas:
            self.active_records.pop(clave, None)
        logger.info(
            f"🧹 {self.ip}: {len(huerfanas)} parte(s) sin aparecer hace más de "
            f"{VENTANA_PURGA_ARRANQUE / 3600:.0f}h al arrancar, se olvidan: "
            f"{', '.join(sorted(huerfanas))}"
        )
        self.save_state()

    def _lado_protegido(self, estacion, lado):
        """
        True si ese lado ya tiene observación propia del registro.

        Con ella, borrar la línea base de una parte ausente es seguro de
        inmediato: si vuelve, _linea_base_de_corrida le da la base correcta
        desde el registro del lado, sin necesidad de lo que esta parte tenía
        guardado. Sin observación todavía no hay esa red, y hay que seguir
        conservando la línea base con la ventana de siempre.
        """
        return self._contador_del_lado(estacion, lado) is not None

    def _contador_del_lado(self, estacion, lado):
        """
        Último valor leído del registro de ese lado, o None si nunca se vio.

        None NO es cero: significa "no hay observación", y quien pregunta debe
        caer al comportamiento de siempre en vez de suponer que el registro
        venía en 0, que inventaría una corrida nueva.
        """
        obs = self.contadores_lado.get(f"{estacion}_{lado}")
        return obs.get('contador') if obs else None

    def _anotar_contador_lado(self, estacion, lado, contador):
        """
        Deja constancia de en cuánto quedó el registro de ese lado.

        Se llama SOLO desde lo diferido, junto con el avance de la línea base de
        la parte: las dos tienen que moverse en el mismo commit. Si la
        observación del lado avanzara y la línea base no —un interbloqueo, o una
        parte que no tuvo dónde registrarse—, el próximo ciclo compararía contra
        un registro ya avanzado y ese delta se perdería en silencio.
        """
        self.contadores_lado[f"{estacion}_{lado}"] = {
            'contador': contador,
            'visto_en': system_time.time(),
        }

    def _linea_base_de_corrida(self, cursor, estacion, num, cnt, log,
                               corridas_previas, contador_lado=None):
        """
        Desde qué valor del contador se empieza a contar una parte recién vista.

        Si se sabe en cuánto venía el registro de ESE LADO, esa es la respuesta
        y no hay que preguntar nada: el registro lo comparten las partes que
        pasan por el lado, así que lo que marca ya está registrado a nombre de
        quien lo produjo. Solo se cuenta lo que avance a partir de ahí.

        Sin observación del lado —primer ciclo tras arrancar, o un lado que
        nunca se ha leído— el contador del PLC admite dos lecturas y elegir mal
        cuesta producción real:

          - Ya había una orden EN PROGRESO: alguien venía contando y lo que
            marca el contador ya está registrado. La base es el valor actual y
            solo se registra lo que venga de aquí en adelante.
          - No había ninguna: es una corrida nueva, y lo que marca el contador
            es producción de esta corrida que todavía no está en ningún lado.

        Esa segunda lectura es la que inflaba producción en cada cambio de
        número: el registro todavía traía el sobrante de la parte que salía y se
        daba por corrida nueva de la que entraba.

        Si la consulta a la base falla, el error sube y el ciclo se reintenta:
        suponer cualquiera de las dos respuestas cuesta producción, sea
        perdiéndola o inventándola.

        `corridas_previas` memoriza la respuesta POR CICLO y es imprescindible:
        cuando LH y RH producen la misma parte, el primer lado en registrarse
        deja la orden En progreso, y sin la memoria el segundo lado creería que
        su contador ya estaba contado y tiraría su arranque a la basura.
        """
        if contador_lado is not None:
            if contador_lado:
                log.info(
                    f"🔗 {estacion}/{num}: el registro del lado NO se reinició, "
                    f"viene en {contador_lado}; se cuenta desde ahí."
                )
            else:
                log.info(f"🔗 {estacion}/{num}: el registro del lado venía en 0, corrida limpia.")
            return contador_lado

        if num not in corridas_previas:
            corridas_previas[num] = ordenes.hay_corrida_en_progreso(cursor, estacion, num)

        if corridas_previas[num]:
            log.info(
                f"↩️ {estacion}/{num}: ya había una orden en progreso, "
                f"se cuenta desde cnt={cnt}."
            )
            return cnt

        log.info(f"▶️ {estacion}/{num}: corrida nueva, el contador trae {cnt} por registrar.")
        return 0

    def _rechazar_en_bd(self, estacion, lado, num_orig, num, cnt, area, error, log):
        """
        La parte no tuvo dónde registrarse. La línea base NO se mueve: cuando
        el contador vuelva a avanzar, el siguiente ciclo reintenta con el
        delta completo en vez de perderlo.
        """
        log.warning(f"⚠️ Número de parte RECHAZADO EN BD: {num_orig} - {error}")
        registrar_error_validacion(estacion, num_orig, error, lado=lado, area=area)
        anotar_estado(estacion, lado, motivo_de_error(error),
                      area=area, ip=self.ip, numero_plc=num_orig,
                      numero_validado=num, contador=cnt, detalle=error)

    async def process_continuously(self):
        if self.ip not in ip_data_queues: return
        loop = asyncio.get_running_loop()
        while True:
            try:
                batch = await ip_data_queues[self.ip].get()

                # Todo el batch corre en UN hilo del pool de BD.
                # Antes se lanzaban corrutinas "concurrentes" con un semáforo, pero
                # como ninguna cedía el control (pyodbc es bloqueante) corrían en
                # serie de todos modos, congelando el event loop mientras tanto.
                # Un hilo por batch mantiene el loop libre y deja active_records
                # bajo un solo hilo a la vez, sin necesidad de locks.
                await loop.run_in_executor(_db_executor, self._process_batch, batch)

                ip_data_queues[self.ip].task_done()

            except asyncio.CancelledError:
                logger.info(f"🛑 Procesador de {self.ip} detenido")
                raise
            except RuntimeError as e:
                # El intérprete está apagando los executors (cierre de la app):
                # seguir intentando solo genera ruido y nunca va a funcionar.
                if 'shutdown' in str(e).lower():
                    logger.info(f"🛑 Procesador de {self.ip} detenido: el pool de hilos se cerró")
                    return
                logger.error(f"Error en loop de procesamiento para {self.ip}: {e}")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error en loop de procesamiento para {self.ip}: {e}")
                await asyncio.sleep(0.5)

    def _process_batch(self, batch):
        """Procesa secuencialmente las estaciones de un batch. Corre en hilo de BD."""
        hubo_cambios = False
        for pkg in batch:
            try:
                if self._process_estacion(pkg):
                    hubo_cambios = True
            except Exception as e:
                est = pkg.get('estacion', '?')
                logger.error(f"Error procesando {est} en {self.ip}: {e}")
                logger.error(traceback.format_exc())

        # Un solo guardado por batch. Antes se escribía el archivo completo de la IP
        # una vez POR ESTACIÓN, así que con muchas estaciones se reescribía decenas
        # de veces por ciclo; ahora es una sola vez, lo que paga el costo del fsync.
        if hubo_cambios:
            self.save_state()

    def _process_estacion(self, pkg):
        """
        Registra lo que produjo una estación desde la lectura anterior.

        El modelo es LLENAR ÓRDENES: cada incremento del contador busca la orden
        abierta más antigua de esa parte, la llena, y al completarse sigue con la
        siguiente. El turno ya no parte los registros — una corrida que cruza el
        cambio de turno sigue llenando la misma orden. El turno solo se guarda
        como dato en el registro de producción no planeada.
        """
        estacion = pkg['estacion']
        datos = pkg['datos']
        area = pkg.get('area', 'Default')
        plc_ok = pkg.get('plc_ok', True)
        now = pkg['ts']

        log = get_station_logger(estacion)

        # Las dos ramas sin partes se resuelven ANTES de pedir conexión: ni la
        # lectura incompleta ni una estación parada que ya se cerró tienen
        # nada que hacer en la base.
        if not datos:
            if not plc_ok:
                REGISTRO.anotar_estacion(estacion, obs_estado.LECTURA_PARCIAL)
                # Lectura incompleta: NO se puede concluir que la estación dejó
                # de producir. Se conserva la línea base de contadores para que
                # al reconectar el delta recupere lo producido en el hueco.
                log.warning(
                    f"⚠️ Lectura incompleta del PLC para {estacion}. "
                    f"Manteniendo estado en caché sin cambios."
                )
                return False

            REGISTRO.anotar_estacion(estacion, obs_estado.ESTACION_SIN_PARTES)
            if estacion in self._estaciones_cerradas:
                return False
        else:
            self._estaciones_cerradas.discard(estacion)

        conn = create_connection()
        if conn is None:
            log.error("❌ No se pudo obtener conexión a BD para procesar estación")
            return False

        pipeline = obtener_pipeline(area)
        # El ciclo completo se reintenta ante un interbloqueo: la victima de
        # SQL Server pierde TODA su transaccion, y sin reintento se perdia la
        # produccion de ese ciclo para siempre (ver es_interbloqueo).
        for intento in range(1, BD_INTENTOS_INTERBLOQUEO + 1):
            state_changed = False

            try:
                with conn.cursor() as cursor:
                    hora = now.time().replace(microsecond=0)
                    turno, fecha_plan = safe_get_current_shift(hora)
                    fecha_fmt = now.strftime('%Y-%m-%d %H:%M:%S')
                    # Qué partes venían ya en corrida ANTES de este ciclo. Se resuelve
                    # una sola vez por parte para que los dos lados de la estación vean
                    # el mismo panorama (ver _linea_base_de_corrida).
                    corridas_previas = {}

                    # Cambios de estado en memoria que SOLO se aplican si el commit
                    # prospera. Antes se aplicaban al vuelo: cuando SQL Server elegía
                    # esta transacción como víctima de un interbloqueo, la base se
                    # revertía pero la línea base ya había avanzado, y esa produccion
                    # no se volvía a intentar nunca. Se perdía en silencio.
                    diferidos = []

                    if not datos:
                        # Lectura buena y sin partes: la estación dejó de
                        # producir. Se cierra lo que quedó abierto UNA sola vez
                        # (ver _estaciones_cerradas); si el commit falla, no se
                        # marca y el siguiente ciclo lo vuelve a intentar.
                        ordenes.detener_estacion(cursor, estacion, fecha_fmt)

                        # La línea base se conserva o no según el mismo
                        # criterio que el bucle de obsoletas más abajo: ver
                        # _lado_protegido. Lo que se conserve (un lado sin
                        # observación, dentro de su ventana) se resuelve cuando
                        # la estación vuelva a mandar partes, o en la purga
                        # del arranque.
                        ahora_epoch = system_time.time()
                        for k in [k for k in self.active_records
                                  if k.startswith(f"{estacion}_")]:
                            reg_ausente = self.active_records[k]
                            if reg_ausente.get('id_en_curso') is not None:
                                def cerrar_de_estacion(clave=k):
                                    self.active_records[clave]['id_en_curso'] = None
                                diferidos.append(cerrar_de_estacion)
                                state_changed = True

                            if self._lado_protegido(estacion, reg_ausente.get('lado')):
                                olvidar_ya = True
                            else:
                                ausente = ahora_epoch - reg_ausente.get('visto_en', 0)
                                olvidar_ya = ausente > VENTANA_OLVIDO_LINEA_BASE

                            if olvidar_ya:
                                def olvidar_de_estacion(clave=k):
                                    self.active_records.pop(clave, None)
                                diferidos.append(olvidar_de_estacion)
                                state_changed = True

                        diferidos.append(lambda: self._estaciones_cerradas.add(estacion))
                        conn.commit()

                        # Esta rama tiene su propio commit, así que también le
                        # toca aplicar lo diferido. Olvidarlo dejaba los cierres
                        # y los olvidos sin efecto.
                        for aplicar in diferidos:
                            aplicar()
                        return state_changed

                    claves_actuales_en_plc = {
                        f"{estacion}_{d['parte']}_{d.get('lado', '--')}"
                        for d in datos if d['parte']
                    }

                    # Marca de "la vi en esta lectura". No cuenta producción ni
                    # depende del commit: solo sirve para saber cuánto lleva
                    # ausente una parte cuando haya que decidir si su línea base
                    # ya no dice nada.
                    ahora_epoch = system_time.time()
                    for k in claves_actuales_en_plc:
                        if k in self.active_records:
                            self.active_records[k]['visto_en'] = ahora_epoch

                    # Fin de corrida por ausencia, SOLO con lectura completa: en una lectura
                    # parcial (falló el bloque de un lado) la ausencia de una parte no
                    # significa que dejó de producirse, y borrarla perdería su línea base.
                    if plc_ok:
                        obsoletas = [k for k in self.active_records
                                     if k.startswith(f"{estacion}_") and k not in claves_actuales_en_plc]
                        for k in obsoletas:
                            reg_ausente = self.active_records[k]
                            abierta = reg_ausente.get('id_en_curso')
                            if abierta:
                                try:
                                    ordenes.detener(cursor, abierta, fecha_fmt)
                                    log.info(
                                        f"⏹️ Corrida terminada en {k}: orden {abierta} "
                                        f"Detenida. El registro quedó en "
                                        f"{reg_ausente.get('contador_registro')}."
                                    )
                                except Exception as e:
                                    # El interbloqueo SÍ sube: lo atrapa el bucle de
                                    # reintentos. Lo demás se tolera como siempre.
                                    if es_interbloqueo(e):
                                        raise
                                    log.error(f"Error deteniendo la orden {abierta}: {e}")

                                def cerrar_la_ausente(clave=k):
                                    self.active_records[clave]['id_en_curso'] = None
                                diferidos.append(cerrar_la_ausente)
                                state_changed = True

                            # La línea base ya no necesita sobrevivir a ciegas:
                            # con el lado protegido (ver _lado_protegido),
                            # olvidarla de inmediato es seguro, porque si esta
                            # parte vuelve el registro del lado le da la base
                            # correcta sin esperar nada. Sin esa observación
                            # todavía, se conserva con la ventana de siempre.
                            if self._lado_protegido(estacion, reg_ausente.get('lado')):
                                log.info(f"🧹 {k}: su lado ya está protegido, se olvida de inmediato.")
                                olvidar_ya = True
                            else:
                                ausente = ahora_epoch - reg_ausente.get('visto_en', 0)
                                olvidar_ya = ausente > VENTANA_OLVIDO_LINEA_BASE
                                if olvidar_ya:
                                    log.info(
                                        f"🧹 {k}: sin aparecer hace {ausente / 3600:.1f} h "
                                        f"y su lado sin observación, se olvida su línea base."
                                    )

                            if olvidar_ya:
                                def olvidar(clave=k):
                                    self.active_records.pop(clave, None)
                                diferidos.append(olvidar)
                                state_changed = True
                    else:
                        log.warning(
                            f"⚠️ Lectura parcial del PLC en {estacion}: se omite el fin de "
                            f"corrida de las partes ausentes para no perder su contador base."
                        )

                    # Corte de la estación para este ciclo (ver más abajo): se
                    # lee perezosamente, una sola vez, justo antes del primer
                    # incremento real que haya que registrar. Leerlo aquí
                    # arriba costaría una consulta en cada ciclo, incluso en
                    # los que ninguna parte avanzó.
                    sync_from, sync_from_leido = None, False

                    for d in datos:
                        num = d['parte']
                        num_orig = d['original']
                        cnt = d['contador']
                        tiempo = d['tiempo']
                        troquel_id = d.get('troquel_id')
                        lado_actual = d.get('lado', '--')

                        cache_key = f"{estacion}_{num_orig}_{num}_{lado_actual}"
                        current_state = {"parte_original": num_orig, "contador": cnt}

                        # 🚀 Contador congelado desde la lectura anterior: no hay nada que
                        # registrar, pero la estación SIGUE viva y hay que dejar constancia
                        # o desaparecería del tablero justo cuando está parada.
                        if self.last_scanned_parts.get(cache_key) == current_state:
                            if num:
                                anotar_estado(estacion, lado_actual, obs_estado.CONTADOR_DETENIDO,
                                              area=area, ip=self.ip, numero_plc=num_orig,
                                              numero_validado=num, contador=cnt)
                            continue

                        def marcar_escaneado(clave=cache_key, estado=current_state):
                            self.last_scanned_parts[clave] = estado
                        diferidos.append(marcar_escaneado)

                        if d.get('validado') is False:
                            error_val = d.get('error_validacion')
                            log.warning(f"⚠️ Número de parte NO VALIDADO: {num_orig} - Error: {error_val}")
                            anotar_estado(estacion, lado_actual, motivo_de_error(error_val),
                                          area=area, ip=self.ip, numero_plc=num_orig,
                                          contador=cnt, detalle=error_val)
                            if error_val:
                                registrar_error_validacion(estacion, num_orig, error_val,
                                                           lado=lado_actual, area=area)
                            continue

                        clave = f"{estacion}_{num}_{lado_actual}"
                        reg = self.active_records.get(clave)

                        # En cuánto venía el registro de ESTE LADO la lectura
                        # pasada. Se lee del estado SIN TOCAR, porque las
                        # actualizaciones van diferidas: así las varias partes que
                        # comparten un lado —un troquel que saca cuatro números
                        # por golpe— ven todas el mismo punto de partida dentro
                        # del ciclo, en vez de que la primera se lleve el
                        # incremento y las demás vean cero.
                        prev_lado = self._contador_del_lado(estacion, lado_actual)

                        if reg is None:
                            base = self._linea_base_de_corrida(
                                cursor, estacion, num, cnt, log, corridas_previas,
                                contador_lado=prev_lado)
                            reg = {
                                'contador_registro': base,
                                'numero_original': num_orig,
                                'lado': lado_actual,
                                'id_en_curso': None,
                                # Sin esto, una parte que aparece y desaparece en el
                                # mismo ciclo se daría por ausente desde 1970 y
                                # perdería su línea base recién calculada.
                                'visto_en': ahora_epoch,
                            }
                            self.active_records[clave] = reg
                            state_changed = True

                        prev_parte = reg.get('contador_registro', cnt)

                        # El registro es del LADO y lo comparten las partes que
                        # pasan por él, así que cuando hay observación del lado
                        # ESA es el punto de partida. Si coincide con la base de
                        # la parte da lo mismo cuál se use; si discrepa, es que
                        # otra parte estuvo contando en ese lado y la base de
                        # esta ya no describe el registro — usarla registraría
                        # como corrida nueva lo que la otra parte ya registró.
                        prev = prev_lado if prev_lado is not None else prev_parte

                        if prev_lado is not None and prev_lado != prev_parte:
                            # Diagnóstico: cuántas piezas se habrían inflado.
                            habria, _ = calcular_incremento(prev_parte, cnt)
                            toca, _ = calcular_incremento(prev_lado, cnt)
                            evitadas = max(habria - toca, 0)
                            log.info(
                                f"🔗 {estacion}/{num}/{lado_actual}: su base decía "
                                f"{prev_parte} pero el registro del lado venía en "
                                f"{prev_lado}. Se cuenta desde {prev_lado} "
                                f"(evita {evitadas} pieza(s) de otra parte)."
                            )
                            try:
                                obs_metricas.linea_base_por_lado.labels(
                                    estacion, lado_actual).inc()
                                if evitadas:
                                    obs_metricas.piezas_fantasma_evitadas.labels(
                                        estacion, lado_actual).inc(evitadas)
                            except Exception:
                                pass

                            # Se realinea la base de la parte con el registro, o
                            # el aviso se repetiría en cada ciclo mientras la
                            # estación esté parada. Diferido como todo lo demás.
                            def realinear(r=reg, c=prev_lado):
                                r['contador_registro'] = c
                            diferidos.append(realinear)
                            state_changed = True

                        if cnt == prev:
                            # La prensa está parada; no es una falla.
                            anotar_estado(estacion, lado_actual, obs_estado.CONTADOR_DETENIDO,
                                          area=area, ip=self.ip, numero_plc=num_orig,
                                          numero_validado=num, contador=cnt)

                            # Sin esto, un lado que arranca detenido (recién
                            # desplegado el servicio, o uno que simplemente no
                            # ha producido todavía) se queda SIN observación
                            # mientras no avance — y mientras no la tiene se
                            # comporta como el código de antes de este arreglo
                            # (ver _linea_base_de_corrida): un cambio de número
                            # de parte en ese tramo vuelve a contarse de más.
                            # Anotarlo también aquí cierra el hueco en un ciclo
                            # en vez de esperar al primer golpe.
                            if prev_lado is None:
                                log.info(
                                    f"🔒 {estacion}/{lado_actual}: primera observación "
                                    f"del registro tras el arranque ({cnt}). Queda protegido."
                                )

                                def anotar_lado_detenido(est=estacion, lad=lado_actual, c=cnt):
                                    self._anotar_contador_lado(est, lad, c)
                                diferidos.append(anotar_lado_detenido)
                                state_changed = True
                            continue

                        incremento, hubo_reset = calcular_incremento(prev, cnt)

                        if hubo_reset:
                            # El contador volvió a empezar: la corrida anterior terminó.
                            # La orden que quedó a medias se marca Detenida. Si la producción
                            # continúa, el siguiente incremento la retoma donde se quedó:
                            # nada de lo ya registrado se pierde ni se duplica.
                            log.warning(
                                f"⚠️ Reset de contador en {estacion}/{num}/{lado_actual}: "
                                f"{prev} → {cnt}. Fin de corrida; lo registrado queda intacto."
                            )
                            abierta = reg.get('id_en_curso')
                            if abierta:
                                try:
                                    ordenes.detener(cursor, abierta, fecha_fmt)
                                except Exception as e:
                                    if es_interbloqueo(e):
                                        raise
                                    log.error(f"Error deteniendo la orden {abierta}: {e}")

                                def cerrar_en_curso(r=reg):
                                    r['id_en_curso'] = None
                                diferidos.append(cerrar_en_curso)
                            try:
                                obs_metricas.resets_contador.labels(estacion, lado_actual).inc()
                            except Exception:
                                pass

                        if incremento <= 0:
                            def avanzar_sin_incremento(r=reg, c=cnt, est=estacion,
                                                       lad=lado_actual):
                                r['contador_registro'] = c
                                self._anotar_contador_lado(est, lad, c)
                            diferidos.append(avanzar_sin_incremento)
                            state_changed = True
                            continue

                        # El id de la parte no cambia mientras dure la corrida, así que
                        # se resuelve una vez y se guarda con la línea base. Consultarlo
                        # en cada incremento costaba lo mismo que buscar las órdenes,
                        # para devolver siempre el mismo número.
                        part_number_id = reg.get('part_number_id')
                        if not part_number_id:
                            part_number_id = obtener_part_number_id(cursor, num, estacion)
                            if part_number_id:
                                reg['part_number_id'] = part_number_id
                                state_changed = True

                        multiplicador = 1
                        if pipeline.usa_multiplicador:
                            # Solo Estampado. Sale de la MISMA fila de part_numbers a
                            # la que se registra (ver catalogo.obtener_multiplicador),
                            # y se relee en cada incremento: pieces_per_shot puede
                            # corregirse en caliente, y no hacerlo registraría toda
                            # una corrida con un valor viejo que después nadie cuadra.
                            if not part_number_id:
                                # Sin fila vigente no hay de dónde leerlo, y
                                # suponer 1 pieza por golpe registraría de menos.
                                self._rechazar_en_bd(estacion, lado_actual, num_orig, num, cnt,
                                                     area, "PART_NUMBER_NO_EXISTE_BD", log)
                                continue
                            multiplicador = catalogo.obtener_multiplicador(cursor, part_number_id)
                            anterior = reg.get('multiplicador')
                            if anterior != multiplicador:
                                if anterior is not None:
                                    log.info(f"🔧 {estacion}/{num}: piezas por golpe "
                                             f"{anterior} → {multiplicador}")
                                reg['multiplicador'] = multiplicador
                                state_changed = True

                        def anotar_historial(id_registro, orden_num, cantidad,
                                             _pid=part_number_id, _troquel=troquel_id, _t=tiempo):
                            """
                            Una fila de histories por lo que se atribuyó a esa orden.

                            `cantidad` va en la unidad del contador del área: golpes
                            en Estampado, piezas en las demás.
                            """
                            if not _pid:
                                return
                            registrar_history(cursor, pipeline, _pid, cantidad, fecha_fmt, _t,
                                              {'troquel_id': _troquel}, log,
                                              shop_order_number=orden_num)

                        if not sync_from_leido:
                            # Corte por estación, leído fresco en cada ciclo: se
                            # edita desde una interfaz web ajena a este repo y
                            # debe surtir efecto sin reiniciar el servicio. Decide
                            # qué órdenes se pueden llenar y a qué régimen va el
                            # sobrante (ver ordenes.ventana_de()).
                            sync_from = ordenes.obtener_sync_from(cursor, estacion)
                            sync_from_leido = True

                        resultado = ordenes.registrar_incremento(
                            cursor, estacion, num, incremento, multiplicador,
                            fecha_plan=fecha_plan, turno=turno, fecha_fmt=fecha_fmt,
                            sync_from=sync_from, anotar_history=anotar_historial, log=log,
                        )

                        if resultado.error:
                            self._rechazar_en_bd(estacion, lado_actual, num_orig, num, cnt,
                                                 area, resultado.error, log)
                            continue

                        def avanzar(r=reg, c=cnt, curso=resultado.en_curso,
                                    est=estacion, lad=lado_actual):
                            r['contador_registro'] = c
                            r['id_en_curso'] = curso
                            self._anotar_contador_lado(est, lad, c)
                        diferidos.append(avanzar)
                        state_changed = True

                        if resultado.cerradas:
                            log.info(
                                f"🏁 {estacion}/{num}/{lado_actual} completó "
                                f"{len(resultado.cerradas)} orden(es): {', '.join(resultado.cerradas)}"
                            )

                        anotar_estado(estacion, lado_actual, obs_estado.PRODUCIENDO,
                                      area=area, ip=self.ip, numero_plc=num_orig,
                                      numero_validado=num, contador=cnt)
                        try:
                            obs_metricas.plc_contador.labels(estacion, lado_actual).set(cnt)
                            obs_metricas.produccion_piezas.labels(
                                estacion, lado_actual, str(area)).inc(resultado.piezas)
                            obs_metricas.produccion_conteo.labels(
                                estacion, lado_actual, str(area)).inc(resultado.incremento)
                            if resultado.cerradas:
                                obs_metricas.ordenes_completadas.labels(
                                    estacion, str(area)).inc(len(resultado.cerradas))
                            if tiempo:
                                obs_metricas.plc_tiempo_ciclo.labels(estacion, lado_actual).set(tiempo)
                        except Exception:
                            pass

                    conn.commit()

                    # Ya está en disco: ahora sí se puede dar por registrado.
                    for aplicar in diferidos:
                        aplicar()
                    return state_changed

            except Exception as e:
                # La transacción no quedó, así que NADA de lo diferido se aplica: la
                # línea base se queda donde estaba y el próximo ciclo reintenta con
                # el delta completo en vez de darlo por perdido.
                try:
                    conn.rollback()
                except Exception:
                    pass

                if es_interbloqueo(e) and intento < BD_INTENTOS_INTERBLOQUEO:
                    # SQL Server lo pide literalmente: "Rerun the transaction".
                    # Como nada avanzó en memoria, reintentar es limpio.
                    log.warning(
                        f"🔁 Interbloqueo en {estacion} (intento {intento} de "
                        f"{BD_INTENTOS_INTERBLOQUEO}): se reintenta el ciclo completo."
                    )
                    system_time.sleep(BD_ESPERA_REINTENTO * intento)
                    continue

                if es_interbloqueo(e):
                    log.error(
                        f"❌ {estacion}: interbloqueo tras {BD_INTENTOS_INTERBLOQUEO} "
                        f"intentos. La línea base NO se mueve: el próximo ciclo "
                        f"reintenta con el delta completo."
                    )
                elif isinstance(e, ordenes.RegistroIncompleto):
                    log.error(f"❌ {e}")
                else:
                    log.error(f"❌ Error procesando {estacion}: {e}")
                    log.error(traceback.format_exc())
                    if isinstance(e, pyodbc.Error):
                        descartar_conexion()
                return False

        # NOTA: No cerramos la conexión aquí, el pool la maneja
        # El guardado del estado lo hace _process_batch, una vez por batch.
        return False

# ═══════════════════════════ ASYNCIO & MAIN LOOPS ═══════════════════════════

ip_data_queues = {}
ip_processors = {}

#  NUEVO: Función para conectar al PLC con timeout
async def connect_plc_with_timeout(plc, ip, port, timeout=PLC_CONNECTION_TIMEOUT):
    """Conecta al PLC con timeout controlado"""
    # Se suelta la conexión anterior ANTES de abrir otra. ClienteMC cierra con
    # RST: el PLC la libera en el momento en vez de dejarla ocupando un lugar
    # de su tabla de conexiones (ver plc/cliente.py).
    try:
        plc.close()
    except Exception:
        pass

    try:
        # Usar run_in_executor para evitar bloquear el event loop
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, plc.connect, ip, port),
            timeout=timeout
        )
        return True
    except asyncio.TimeoutError:
        logger.warning(f"⏱️ Timeout al conectar con PLC {ip}:{port}")
        return False
    except OSError as e:
        # FIX: captura específica para errores de socket Windows
        winerr = getattr(e, 'winerror', None) or e.errno
        if winerr == 10061:
            logger.warning(f"🔌 WinError 10061 – PLC {ip}:{port} rechazó la conexión (Connection Refused). Reintentando en {RECONNECT_DELAY}s...")
        elif winerr == 10054:
            logger.warning(f"🔌 WinError 10054 – PLC {ip}:{port} cerró la conexión (Connection Reset). Reintentando en {RECONNECT_DELAY}s...")
        else:
            logger.warning(f"⚠️ OSError [{winerr}] conectando con PLC {ip}:{port}: {str(e)[:100]}")
        return False
    except Exception as e:
        logger.warning(f"⚠️ Error conectando con PLC {ip}:{port}: {str(e)[:100]}")
        return False

async def plc_reader(ip, port, group_info):
    """
    Lector del PLC.

    `group_info` es el dict VIVO que mantiene el supervisor: cuando cambia la
    configuración de esta IP, el supervisor lo actualiza en su lugar y sube
    '_version'. Aquí se detecta y se reconstruye el colector sin cerrar la
    conexión al PLC, así que las estaciones de las demás IPs ni se enteran.
    """

    version_config = group_info.get('_version', 0)
    collector = IPDataCollector(ip, group_info['estaciones'], group_info.get('area', 'Default'))

    if ip not in ip_data_queues:
        ip_data_queues[ip] = asyncio.Queue(maxsize=1000)

    #  NUEVO: Configuración inicial del PLC
    def create_plc_instance():
        """Crea una nueva instancia del PLC"""
        plc = ClienteMC()
        plc.network = 0
        plc.pc = 0xFF
        # setaccessopt toma segundos y deriva timer (250ms) y soc_timeout
        # (timer_sec + 1) correctamente — ver PLC_READ_TIMEOUT_SEC.
        plc.setaccessopt(timer_sec=PLC_READ_TIMEOUT_SEC)
        return plc

    plc = create_plc_instance()
    connected = False
    consecutive_failures = 0
    max_consecutive_failures = 3

    #  NUEVO: Intervalo de lectura más inteligente
    last_read_time = datetime.now()
    read_interval = 1.0  # Leer cada 1 segundo

    #  NUEVO: Direcciones para lectura
    addrs = list(group_info['all_addresses']) if 'all_addresses' in group_info else []

    try:
        while True:
            try:
                # ¿Cambió la configuración de esta IP desde el último ciclo?
                if group_info.get('_version', 0) != version_config:
                    version_config = group_info.get('_version', 0)
                    collector = IPDataCollector(
                        ip, group_info['estaciones'], group_info.get('area', 'Default')
                    )
                    addrs = list(group_info.get('all_addresses', []))
                    # Una estación que ya no cuelga de esta IP no debe quedarse en
                    # el tablero de lecturas en vivo con su último dato.
                    LECTURAS.olvidar_ip(ip, excepto=group_info['estaciones'])

                    nuevo_puerto = group_info.get('port', port)
                    if nuevo_puerto != port:
                        # El puerto sí obliga a reconectar; los tags no.
                        logger.info(f"🔌 {ip}: el puerto cambió {port} → {nuevo_puerto}, reconectando")
                        port = nuevo_puerto
                        try: plc.close()
                        except Exception: pass
                        connected = False
                        plc = create_plc_instance()
                    else:
                        logger.info(
                            f"♻️ {ip}: configuración recargada (v{version_config}) "
                            f"sin cerrar la conexión — {len(addrs)} bloque(s), "
                            f"{len(group_info['estaciones'])} estación(es)"
                        )

                if not connected:
                    logger.info(f"🔌 Intentando conectar a PLC {ip}:{port}")

                    # Intentar conexión con timeout
                    connected = await connect_plc_with_timeout(plc, ip, port)

                    if connected:
                        consecutive_failures = 0
                        logger.info(f"✅ Conectado a PLC {ip}:{port}")
                        try:
                            obs_metricas.plc_conectado.labels(ip).set(1)
                            obs_metricas.config_version.labels(ip).set(version_config)
                        except Exception:
                            pass

                        # Actualizar monitor

                        # Notificar a las estaciones
                        for est in group_info['estaciones']:
                            get_station_logger(est).info(f"✅ PLC {ip} conectado")
                    else:
                        consecutive_failures += 1
                        try:
                            obs_metricas.plc_conectado.labels(ip).set(0)
                        except Exception:
                            pass
                        for _est in group_info.get('estaciones', []):
                            REGISTRO.anotar_estacion(_est, obs_estado.PLC_DESCONECTADO)
                        if consecutive_failures >= max_consecutive_failures:
                            # Escalonado: se duplica por cada fallo de más allá del
                            # umbral, con tope. Machacar cada RECONNECT_DELAY fijo
                            # mientras el PLC está realmente caído no lo revive más
                            # rápido y solo llena el log sin decir nada nuevo.
                            de_mas = consecutive_failures - max_consecutive_failures
                            espera = min(RECONNECT_DELAY * (2 ** de_mas), RECONNECT_DELAY_MAX)
                            logger.error(
                                f"❌ {consecutive_failures} fallos consecutivos de conexión "
                                f"con PLC {ip}, esperando {espera}s"
                            )
                            await asyncio.sleep(espera)
                        else:
                            await asyncio.sleep(2)  # Espera corta entre intentos
                        continue

                #  LECTURA DEL PLC
                current_time = datetime.now()
                time_since_last_read = (current_time - last_read_time).total_seconds()

                if time_since_last_read >= read_interval:
                    try:
                        with obs_metricas.plc_duracion_lectura.labels(ip).time():
                            await collector.collect_and_enqueue(plc, group_info)
                        last_read_time = current_time
                        try:
                            obs_metricas.plc_lecturas.labels(ip, "ok").inc()
                            obs_metricas.plc_ultima_lectura.labels(ip).set(current_time.timestamp())
                        except Exception:
                            pass

                    except OSError as read_error:
                        winerr = getattr(read_error, 'winerror', None) or read_error.errno
                        err_str = str(read_error)
                        es_timeout = "timed out" in err_str and winerr is None
                        try:
                            obs_metricas.plc_lecturas.labels(
                                ip, "timeout" if es_timeout else "error").inc()
                            obs_metricas.plc_conectado.labels(ip).set(0)
                        except Exception:
                            pass

                        # Cualquier error de lectura abandona la conexión, también
                        # un timeout. Seguir en el mismo socket deja que la
                        # respuesta tardía se tome como la de la petición
                        # siguiente, y desde ahí todas quedan recorridas un lugar:
                        # el contador recibe el número de parte. Pasó en Estampado
                        # del 12 al 17 de noviembre de 2026 (la 2500T leyó "DG",
                        # 18,244, como contador: 17,079 golpes fantasma). Una
                        # conexión nueva no puede recibir respuestas de la vieja, y
                        # ClienteMC la cierra con RST para que el PLC la suelte.
                        if es_timeout:
                            logger.warning(f"⏱️ Timeout leyendo PLC {ip} — se abre una conexión nueva")
                            espera = 2
                        elif isinstance(read_error, RespuestaInvalida):
                            logger.warning(f"🔀 {read_error} — se abre una conexión nueva")
                            espera = 2
                        elif winerr == 10054:
                            logger.warning(f"🔌 WinError 10054 – PLC {ip} cerró la conexión durante lectura (Connection Reset by Peer). Reconectando...")
                            espera = RECONNECT_DELAY
                        elif winerr == 10061:
                            logger.warning(f"🔌 WinError 10061 – PLC {ip} rechazó la lectura (Connection Refused). Reconectando...")
                            espera = RECONNECT_DELAY
                        else:
                            logger.error(f"⚠️ OSError [{winerr}] en lectura PLC {ip}: {err_str[:120]}")
                            espera = RECONNECT_DELAY
                        plc.close()
                        connected = False
                        plc = create_plc_instance()
                        await asyncio.sleep(espera)
                    except Exception as read_error:
                        logger.error(f"❌ Error en lectura PLC {ip}: {str(read_error)[:100]}")
                        plc.close()
                        connected = False
                        plc = create_plc_instance()
                        await asyncio.sleep(1)

                #  ESPERA INTELIGENTE
                time_to_wait = max(0, read_interval - (datetime.now() - current_time).total_seconds())
                if time_to_wait > 0:
                    await asyncio.sleep(time_to_wait)

            except Exception as e:
                logger.error(f"❌ Error crítico en plc_reader para {ip}: {str(e)[:200]}")
                connected = False
                plc = create_plc_instance()
                await asyncio.sleep(RECONNECT_DELAY)
    finally:
        # Al cancelar el lector (apagado del servicio, o la IP salió de la
        # configuración) se suelta la conexión con RST. Antes el socket se
        # abandonaba y el sistema lo cerraba después con FIN: un módulo que
        # no completa ese cierre (MK05) se quedaba con la conexión ocupada.
        plc.close()


def _huella_config(group_info):
    """
    Firma del contenido relevante de una IP. Si cambia, hay que recargar.
    Se ignora '_version' para no compararse consigo misma.
    """
    relevante = {
        'port': group_info.get('port'),
        'serie': group_info.get('serie'),
        'area': group_info.get('area'),
        'estaciones': sorted(group_info.get('estaciones', [])),
        'all_addresses': sorted(map(str, group_info.get('all_addresses', []))),
        'station_configs': {
            est: sorted((t, str(v.get('address')), v.get('long'))
                        for t, v in cfg.items())
            for est, cfg in sorted(group_info.get('station_configs', {}).items())
        },
    }
    return hashlib.md5(str(relevante).encode()).hexdigest()


async def supervisor():
    tasks = {}
    configs_vivas = {}   # ip -> group_info mutable compartido con su lector
    last_successful_config = {}
    config_failures = 0
    max_config_failures = 5
    last_status_log = datetime.now()

    loop = asyncio.get_running_loop()

    # 🔄 Cargar turnos al inicio (consulta bloqueante → hilo)
    await loop.run_in_executor(_db_executor, refresh_shifts_config)

    # Dejar asentado en qué base se está escribiendo: al correr en paralelo
    # servidor/local es lo que evita confundir pruebas con producción.
    logger.info(f"💾 Escribiendo en {os.getenv('DB_SERVER')}/{os.getenv('DB_NAME')}")

    # Servidor de monitoreo: estado clasificado, métricas y bloque crudo del PLC.
    obs_servidor.usar_store_rechazos(RECHAZOS)
    obs_servidor.iniciar(HTTP_PORT)

    logger.info("🚀 Supervisor iniciado")

    while True:
        try:
            # load_config consulta la BD: también fuera del event loop.
            # Los turnos se recargan en cada ciclo: si alguien cambia un horario
            # en la BD, se toma solo, sin botón y sin reiniciar el servicio.
            config = await loop.run_in_executor(_db_executor, load_config)
            await loop.run_in_executor(_db_executor, refresh_shifts_config)

            if not config:
                config_failures += 1
                if config_failures >= max_config_failures:
                    logger.warning(f"⚠️ {config_failures} fallos consecutivos cargando config. Reintentando en {POLL_INTERVAL}s...")
                    if last_successful_config:
                        # Solo mostrar cada 60 segundos para no saturar logs
                        current_time = datetime.now()
                        if (current_time - last_status_log).total_seconds() > 60:
                            logger.info("📋 Usando última configuración válida")
                            last_status_log = current_time
                        config = last_successful_config
                    else:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                else:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
            else:
                if config_failures > 0:
                    logger.info("✅ Configuración cargada exitosamente después de fallos")
                config_failures = 0
                last_successful_config = config

            ips_actuales = set(config.keys())

            # Configuración VIVA: en vez de pasarle una copia al lector y olvidarla,
            # se guarda un dict por IP que se actualiza EN SU LUGAR. El lector
            # compara '_version' en cada ciclo y se recarga solo. Así un cambio de
            # tag se toma sin reiniciar el proceso y sin tocar las otras IPs.
            for ip, nuevo in config.items():
                if ip not in configs_vivas:
                    configs_vivas[ip] = dict(nuevo, _version=1)
                    continue

                if _huella_config(nuevo) != _huella_config(configs_vivas[ip]):
                    version = configs_vivas[ip].get('_version', 0) + 1
                    configs_vivas[ip].clear()
                    configs_vivas[ip].update(nuevo)
                    configs_vivas[ip]['_version'] = version
                    logger.info(
                        f"🔄 Configuración de {ip} actualizada (v{version}): "
                        f"{len(nuevo.get('estaciones', []))} estación(es), "
                        f"{len(nuevo.get('all_addresses', []))} bloque(s). "
                        f"El lector la tomará en el siguiente ciclo."
                    )

            for ip in list(configs_vivas):
                if ip not in ips_actuales:
                    del configs_vivas[ip]

            # Iniciar nuevas tareas para IPs que no existen
            for ip in ips_actuales:
                if ip not in tasks:
                    port = config[ip]['port']
                    logger.info(f"🔌 Iniciando PLC reader para {ip}:{port}")

                    #  CORRECCIÓN CRÍTICA: Crear cola ANTES del procesador
                    # Esto evita la condición de carrera
                    if ip not in ip_data_queues:
                        ip_data_queues[ip] = asyncio.Queue(maxsize=1000)

                    # Crear procesador y su tarea
                    if ip not in ip_processors:
                        processor = IPDataProcessor(ip)
                        ip_processors[ip] = processor

                        proc_task = asyncio.create_task(processor.process_continuously())
                        tasks[f"{ip}_processor"] = proc_task
                    
                    # Crear tarea de lectura
                    reader_task = asyncio.create_task(plc_reader(ip, port, configs_vivas[ip]))
                    tasks[ip] = reader_task

            # Detener tareas para IPs que ya no existen
            for ip in list(tasks.keys()):
                if ip.endswith('_processor'):
                    continue

                if ip not in ips_actuales:
                    logger.info(f"🛑 Deteniendo PLC reader para {ip}")
                    tasks[ip].cancel()
                    del tasks[ip]
                    LECTURAS.olvidar_ip(ip)

                    # Detener procesador también
                    proc_key = f"{ip}_processor"
                    if proc_key in tasks:
                        tasks[proc_key].cancel()
                        del tasks[proc_key]

            # La configuración se recarga sola en cada ciclo; ya no hace falta
            # el evento que disparaba el botón de la ventana.
            await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"❌ Error en supervisor: {e}")
            logger.error(traceback.format_exc())
            await asyncio.sleep(POLL_INTERVAL)

# ═══════════════════════════ MAIN OPTIMIZADO ═══════════════════════════

def _configurar_apagado(loop):
    """Ctrl+C y la señal de parada del servicio terminan el loop ordenadamente."""
    def parar(*_):
        logger.info("👋 Señal de detención recibida")
        for task in asyncio.all_tasks(loop):
            task.cancel()
    try:
        signal.signal(signal.SIGINT, parar)
        signal.signal(signal.SIGTERM, parar)
    except (ValueError, AttributeError):
        pass  # sin consola (servicio de Windows): no hay señales que capturar


def main():
    """
    Arranca el servicio. Sin interfaz gráfica.

    Antes el proceso principal era la ventana de Tkinter y la adquisición vivía
    en un hilo daemon: cerrar la ventana —o cerrar sesión de Windows— detenía el
    conteo sin que nadie se enterara. Ahora el servicio es el proceso principal
    y el monitoreo se consulta por HTTP (Grafana o curl).
    """
    configurar_logging()

    faltantes = [v for v in ('DB_SERVER', 'DB_NAME', 'DB_USER', 'DB_PASSWORD')
                 if not os.getenv(v)]
    if faltantes:
        logger.error(f"❌ Faltan variables de entorno críticas: {faltantes}")
        logger.error("   Revisa el archivo .env en la carpeta del proyecto")
        return 1

    logger.info("=" * 62)
    logger.info(f"🏭 IoTDataPipeline · Python {sys.version.split()[0]}")
    logger.info(f"   Monitoreo: http://localhost:{HTTP_PORT}/estaciones/estado")
    logger.info("=" * 62)
    logger.info(f"📋 Bitácora de rechazos: {RECHAZOS.combinaciones} combinación(es) previas")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _configurar_apagado(loop)

    def handle_exception(loop, context):
        msg = context.get("exception", context["message"])
        logger.error(f"🚨 Excepción no capturada en loop asyncio: {msg}")

    loop.set_exception_handler(handle_exception)

    salida = 0
    try:
        loop.run_until_complete(supervisor())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("👋 Detención solicitada")
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}")
        logger.error(traceback.format_exc())
        salida = 1
    finally:
        logger.info("🛑 Cerrando...")
        pendientes = asyncio.all_tasks(loop)
        for task in pendientes:
            task.cancel()
        if pendientes:
            loop.run_until_complete(asyncio.gather(*pendientes, return_exceptions=True))

        # Los executors antes que el pool: sus hilos pueden estar usando conexiones
        _plc_executor.shutdown(wait=True, cancel_futures=True)
        _catalogo_executor.shutdown(wait=True, cancel_futures=True)
        _db_executor.shutdown(wait=True, cancel_futures=True)
        ConnectionPool.cerrar_todas()

        loop.close()
        logger.info("✅ Servicio detenido correctamente")

    return salida


if __name__ == '__main__':
    sys.exit(main())
