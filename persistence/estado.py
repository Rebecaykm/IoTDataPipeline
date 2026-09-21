"""
Persistencia del estado de contadores en disco (state_cache/).

Este archivo guarda la LÍNEA BASE de cada contador: sin él, al arrancar no se
sabe desde dónde contar y se pierde la producción del hueco. Por eso la
escritura es atómica.

Qué guarda cada entrada:
    contador_registro  desde qué valor del contador se cuentan los deltas
    multiplicador      piezas por golpe vigente (solo cambia en Estampado)
    id_en_curso        la orden que se está llenando, para poder cerrarla si la
                       corrida termina de golpe
    numero_original    lo que mandó el PLC, sin resolver
    lado               LH / RH / --
    part_number_id     id de la parte, resuelto una vez por corrida
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger("supervisor")

#: Campos que escribía el modelo por día y turno. Ya no los produce ni los lee
#: nadie, pero siguen en los archivos de estado anteriores al cambio.
OBSOLETOS = ("id_registro", "hora_cambio", "quantity_planeada",
             "necesita_production_start", "error_bd", "registro_creado",
             "delta_inicial", "cerrado_por_turno")


def guardar_estado(state_file, active_records, ip):
    """
    Guarda el estado de forma ATÓMICA: temporal + fsync + os.replace.

    Escribir directo sobre el archivo final (open 'w') lo trunca de inmediato:
    un corte de energía a media escritura dejaba un JSON incompleto y al
    arrancar se perdía la línea base de TODAS las estaciones de esa IP.

    Con os.replace el archivo final nunca queda a medias: o tiene el contenido
    viejo completo, o el nuevo completo.
    """
    tmp_path = state_file.with_suffix('.json.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(active_records, f, indent=4, ensure_ascii=False)
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
    Lee el estado previo. Devuelve {clave: registro} (vacío si no hay archivo).

    Si el archivo está corrupto se preserva con otro nombre en vez de
    sobrescribirlo en el siguiente guardado, y se avisa lo que implica.
    """
    if not state_file.exists():
        return {}

    active_records = {}
    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        for clave, record in data.items():
            try:
                # Restos del modelo por día y turno: los archivos anteriores al
                # cambio los traen y nadie los lee. Se descartan al cargar, así
                # el siguiente guardado deja el archivo limpio solo.
                for obsoleto in OBSOLETOS:
                    record.pop(obsoleto, None)

                # 🛡️ MIGRACIÓN DE JSON ANTIGUO: añadir '_GLOBAL' si la llave no trae lado
                if not any(clave.endswith(suf) for suf in
                           ['_GLOBAL', '_RH', '_LH', '_RH REAR', '_LH REAR', '_--']):
                    clave = f"{clave}_GLOBAL"

                if 'numero_original' not in record:
                    validated_part = clave.split('_', 1)[1] if '_' in clave else ''
                    record['numero_original'] = validated_part

                active_records[clave] = record
            except Exception as e:
                logger.error(f"Error al deserializar registro {clave}: {e}")
                continue

        logger.info(f"Estado recuperado para {ip}: {len(active_records)} registros cargados.")
        return active_records

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
        return {}
