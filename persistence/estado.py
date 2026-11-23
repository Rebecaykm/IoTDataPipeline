"""
Persistencia del estado de contadores en disco (state_cache/).

Este archivo guarda la LÍNEA BASE de cada contador: sin él, al arrancar no se
sabe desde dónde contar y se pierde la producción del hueco. Por eso la
escritura es atómica.

Guarda DOS cosas, y la distinción es el corazón del archivo:

  partes  línea base POR PARTE: desde qué valor se cuentan los deltas de cada
          número de parte que la estación está produciendo.
  lados   último valor leído del registro POR (ESTACIÓN, LADO), sin importar
          qué parte lo estaba mostrando.

El registro del contador es del LADO, no de la parte: cuando el operador cambia
de número, el registro todavía trae el sobrante de la parte que salió y el
reinicio a 0 llega después. Sin `lados`, ese sobrante se registraba como una
corrida nueva de la parte que entraba —producción duplicada— y daba lo mismo
que la parte entrante tuviera línea base vieja (se leía como reinicio) o no la
tuviera (se leía como corrida nueva): el número registrado era el mismo.

Qué guarda cada entrada de `partes`:
    contador_registro  desde qué valor del contador se cuentan los deltas
    multiplicador      último pieces_per_shot usado (solo Estampado); sirve
                       para avisar en el log cuando alguien lo corrige
    id_en_curso        la orden que se está llenando, para poder cerrarla si la
                       corrida termina de golpe
    numero_original    lo que mandó el PLC, sin resolver
    lado               LH / RH / --
    part_number_id     id de la parte, resuelto una vez por corrida
    visto_en           epoch de la última lectura en que el PLC la reportó;
                       decide cuándo se puede olvidar su línea base

Qué guarda cada entrada de `lados` (clave "ESTACION_LADO"):
    contador           último valor leído del registro de ese lado
    visto_en           epoch de esa lectura

Los archivos escritos antes de `lados` son un diccionario plano de partes. Se
leen igual y `lados` arranca vacío: mientras no haya observación de un lado se
decide como antes, que es el comportamiento seguro.

Y una tercera sección, `tramos`, por la misma razón que `lados`. Mientras el PLC
manda en un lado un número rechazado (un MDI que no existe, una parte sin
catálogo) o el lado viene en blanco, nadie registra y `lados` no se mueve, pero
el registro sí avanza. Sin el tramo, la siguiente parte DISTINTA que llegaba al
lado arrancaba desde el último valor registrado y se llevaba todo ese avance.
Ver Prensas.IPDataProcessor._seguir_tramo.

Qué guarda cada entrada de `tramos` (clave "ESTACION_LADO"):
    tipo     TRAMO_RECHAZO o TRAMO_EN_BLANCO
    numero   lo que mandó el PLC (vacío si el lado venía en blanco)
    motivo   el código del rechazo, o LADO_EN_BLANCO
    antes    con el lado en blanco, la parte que lo ocupaba justo antes; si es
             ella la que vuelve, el tramo es suyo
    desde    cuándo empezó el tramo
    base     en cuánto venía el registro al empezar el tramo, o None si el lado
             no tenía observación; desde ahí cuenta la parte dueña del tramo
    ultimo   último valor leído del registro durante el tramo; desde ahí arranca
             cualquier otra parte
    golpes   cuánto avanzó el registro durante el tramo, en la unidad del área
    unidad   'golpes' o 'piezas', para el log

Un archivo sin `tramos` se lee igual y la sección arranca vacía. Así, quien
regrese a la versión anterior del recolector no tiene que tocar el archivo: esa
versión ignora la sección.
"""

import json
import logging
import os
import time
from datetime import datetime

logger = logging.getLogger("supervisor")

#: Versión del formato en disco. 1 era un diccionario plano de partes.
VERSION = 2

#: Los dos tipos de tramo sin registrar (ver `tramos` en el docstring).
TRAMO_RECHAZO = "rechazo"
TRAMO_EN_BLANCO = "blanco"

#: Campos que escribía el modelo por día y turno. Ya no los produce ni los lee
#: nadie, pero siguen en los archivos de estado anteriores al cambio.
OBSOLETOS = ("id_registro", "hora_cambio", "quantity_planeada",
             "necesita_production_start", "error_bd", "registro_creado",
             "delta_inicial", "cerrado_por_turno")


def guardar_estado(state_file, active_records, ip, contadores_lado=None, tramos_lado=None):
    """
    Guarda el estado de forma ATÓMICA: temporal + fsync + os.replace.

    Escribir directo sobre el archivo final (open 'w') lo trunca de inmediato:
    un corte de energía a media escritura dejaba un JSON incompleto y al
    arrancar se perdía la línea base de TODAS las estaciones de esa IP.

    Con os.replace el archivo final nunca queda a medias: o tiene el contenido
    viejo completo, o el nuevo completo.

    Las secciones se escriben JUNTAS, en el mismo archivo y la misma
    operación atómica: si los contadores por lado pudieran quedar de un ciclo
    distinto que las líneas base, la comparación entre ambos no significaría
    nada y volverían los fantasmas que `lados` existe para evitar.
    """
    tmp_path = state_file.with_suffix('.json.tmp')
    try:
        payload = {
            "version": VERSION,
            "partes": active_records,
            "lados": contadores_lado or {},
            "tramos": tramos_lado or {},
        }
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # forzar a disco antes del rename

        os.replace(tmp_path, state_file)  # atómico en Windows y POSIX
    except Exception as e:
        logger.error(f"Error guardando estado para {ip}: {e}")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def cargar_estado(state_file, ip):
    """
    Lee las LÍNEAS BASE por parte. Devuelve {clave: registro}, vacío si no hay.

    Acepta los dos formatos: el diccionario plano de antes de `lados` y el
    envuelto con versión. Si el archivo está corrupto se preserva con otro
    nombre en vez de sobrescribirlo en el siguiente guardado, y se avisa lo que
    implica.
    """
    data = _leer_archivo(state_file, ip)
    if data is None:
        return {}

    crudas = data["partes"] if _es_envuelto(data) else data
    if not isinstance(crudas, dict):
        logger.error(
            f"❌ El estado de {ip} no trae partes legibles. Se arranca sin líneas base."
        )
        return {}

    active_records = {}
    for clave, record in crudas.items():
        try:
            if not isinstance(record, dict):
                continue

            # Restos del modelo por día y turno: los archivos anteriores al
            # cambio los traen y nadie los lee. Se descartan al cargar, así
            # el siguiente guardado deja el archivo limpio solo.
            for obsoleto in OBSOLETOS:
                record.pop(obsoleto, None)

            # 🛡️ MIGRACIÓN DE JSON ANTIGUO: añadir '_GLOBAL' si la llave no trae lado
            if not any(clave.endswith(suf) for suf in
                       ['_GLOBAL', '_RH', '_LH', '_RH REAR', '_LH REAR', '_--']):
                clave = f"{clave}_GLOBAL"

            # Los archivos anteriores a este campo no dicen cuándo se vio
            # por última vez. Tomarlo como 0 las daría por ausentes desde
            # 1970 y el primer hueco del PLC borraría TODAS las líneas base
            # recién recuperadas. Se cuentan como vistas al arrancar.
            if not record.get('visto_en'):
                record['visto_en'] = time.time()

            if 'numero_original' not in record:
                validated_part = clave.split('_', 1)[1] if '_' in clave else ''
                record['numero_original'] = validated_part

            active_records[clave] = record
        except Exception as e:
            logger.error(f"Error al deserializar registro {clave}: {e}")
            continue

    logger.info(f"Estado recuperado para {ip}: {len(active_records)} registros cargados.")
    return active_records


def cargar_contadores_lado(state_file, ip):
    """
    Lee el último contador observado por (ESTACIÓN, LADO). {} si no hay.

    Un archivo del formato anterior no trae esta sección, y devolver {} es lo
    correcto: sin observación del lado se decide la línea base como siempre
    —preguntando a la base si había una orden en progreso— hasta que la primera
    lectura buena de ese lado la deje registrada. Inventar una observación de la
    nada sí podría tirar producción real.
    """
    data = _leer_archivo(state_file, ip)
    if data is None or not _es_envuelto(data):
        return {}

    crudas = data.get("lados")
    if not isinstance(crudas, dict):
        return {}

    contadores = {}
    for clave, record in crudas.items():
        if not isinstance(record, dict):
            continue
        contador = record.get('contador')
        # bool es subclase de int y un True aquí no significa nada.
        if not isinstance(contador, int) or isinstance(contador, bool):
            continue
        contadores[clave] = {
            'contador': contador,
            'visto_en': record.get('visto_en') or time.time(),
        }

    if contadores:
        logger.info(f"Contadores por lado recuperados para {ip}: {len(contadores)}.")
    return contadores


def cargar_tramos_lado(state_file, ip):
    """
    Lee los tramos sin registrar que quedaron abiertos por (ESTACIÓN, LADO).
    {} si no hay.

    Un archivo sin esta sección da {} y eso es seguro. El primer rechazo o lado
    en blanco que se lea después de arrancar abre su tramo desde `lados`, que
    sí está guardado: lo que el registro avanzó mientras el servicio estaba
    detenido queda dentro del tramo y no lo hereda nadie.

    Una entrada que no se entienda se descarta sola, sin tumbar las demás.
    """
    data = _leer_archivo(state_file, ip)
    if data is None or not _es_envuelto(data):
        return {}

    crudas = data.get("tramos")
    if not isinstance(crudas, dict):
        return {}

    def entero(valor):
        # bool es subclase de int y un True aquí no significa nada.
        return isinstance(valor, int) and not isinstance(valor, bool)

    tramos = {}
    for clave, tramo in crudas.items():
        if not isinstance(tramo, dict):
            continue
        if tramo.get('tipo') not in (TRAMO_RECHAZO, TRAMO_EN_BLANCO):
            continue
        if not entero(tramo.get('ultimo')) or not entero(tramo.get('golpes')):
            continue
        if tramo.get('base') is not None and not entero(tramo.get('base')):
            continue
        tramos[clave] = tramo

    if tramos:
        logger.info(f"Tramos sin registrar recuperados para {ip}: {len(tramos)}.")
    return tramos


def _es_envuelto(data):
    """Formato con versión (trae `partes`) vs. diccionario plano de partes."""
    return isinstance(data, dict) and isinstance(data.get("partes"), dict)


def _leer_archivo(state_file, ip):
    """
    JSON crudo del archivo de estado, o None si no hay o no se pudo leer.

    Se llama una vez por sección y solo al arrancar, así que leer el archivo
    varias veces no cuesta nada y evita acoplar las cargas.
    """
    if not state_file.exists():
        return None

    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(
            f"❌ No se pudo leer el estado de {ip}: {e}. "
            f"Se arranca SIN líneas base: los contadores se reestablecen desde "
            f"la lectura actual y no se contará la producción del hueco."
        )
        try:
            corrupto = state_file.with_suffix(
                f".json.corrupto-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
            state_file.rename(corrupto)
            logger.error(f"   Archivo preservado como {corrupto.name} para revisión.")
        except Exception:
            pass
        return None
