"""
Catálogo de números de parte: MDI (troquel) y piezas por golpe.

Los dos datos son atributos de la tabla `attributes` (patrón EAV de Laravel),
colgados de una fila de part_numbers:

    attributes.[key] = 'mid'              el MDI que manda el PLC de Estampado
    attributes.[key] = 'pieces_per_shot'  cuántas piezas saca un golpe
"""

import logging

logger = logging.getLogger("supervisor")

TIPO_ATRIBUTO = 'App\\Models\\PartNumber'

# Motivos de rechazo. Alimentan el CSV/JSONL y el tablero de partes rechazadas.
MDI_NO_EXISTE = "MDI_NO_EXISTE"
MDI_DE_OTRA_ESTACION = "MDI_DE_OTRA_ESTACION"
PART_NUMBER_OBSOLETO = "PART_NUMBER_OBSOLETO"


def resolver_mdi(cursor, mdi, estacion, log=None):
    """
    Traduce el MDI que manda el PLC a los números de parte que debe producir.

    Devuelve (numeros_activos, error):
      - numeros_activos: lista de números no obsoletos de ESA estación
      - error: None si hubo resultados; si no, el motivo clasificado

    La consulta NO filtra por estación ni por obsoleto: se traen todas las filas
    del MDI para poder distinguir *por qué* falló, que es lo que necesita el
    tablero de partes rechazadas.
    """
    log = log or logger
    mdi_limpio = (mdi or "").replace(" ", "")
    if not mdi_limpio:
        return [], MDI_NO_EXISTE

    cursor.execute("""
        SELECT pn.number, pn.is_obsolete, wc.name
        FROM part_numbers pn
        JOIN work_centers wc ON pn.work_center_id = wc.id
        JOIN attributes a_mid
             ON a_mid.attributable_id = pn.id
            AND a_mid.attributable_type = ?
            AND a_mid.[key] = 'mid'
        WHERE REPLACE(a_mid.value, ' ', '') = ?
    """, (TIPO_ATRIBUTO, mdi_limpio))

    filas = cursor.fetchall()

    if not filas:
        log.warning(f"⚠️ MDI {mdi} no existe en el catálogo")
        return [], MDI_NO_EXISTE

    de_la_estacion = [f for f in filas if f[2] == estacion]
    if not de_la_estacion:
        otras = sorted({f[2] for f in filas})
        log.warning(f"⚠️ MDI {mdi} existe pero pertenece a {otras}, no a {estacion}")
        return [], MDI_DE_OTRA_ESTACION

    activos = [f for f in de_la_estacion if not f[1]]
    if not activos:
        log.warning(f"⚠️ MDI {mdi}: los {len(de_la_estacion)} números de {estacion} están OBSOLETOS")
        return [], PART_NUMBER_OBSOLETO

    obsoletos = [f[0] for f in de_la_estacion if f[1]]
    if obsoletos:
        log.warning(f"⚠️ Filtrados {len(obsoletos)} número(s) obsoleto(s) del MDI {mdi}: {obsoletos}")

    # SIN ESPACIOS: todas las consultas aguas abajo comparan contra
    # REPLACE(pn.number, ' ', ''), así que un número como 'BDTS28BFC -P'
    # debe salir de aquí como 'BDTS28BFC-P' o no empatará con nada.
    numeros = [_sin_espacios(f[0]) for f in activos]

    log.info(f"✅ MDI {mdi} -> {numeros} en {estacion}")
    return numeros, None


def obtener_multiplicador(cursor, part_number_id):
    """
    Piezas por golpe de esa fila de part_numbers. 1 si no tiene el atributo.

    Va por el id ya resuelto (persistence.repositorio.obtener_part_number_id),
    no por número+estación: así sale de la MISMA fila a la que se le registra
    la producción. El catálogo llega a tener el mismo número dos veces en la
    misma estación —la vigente y una dada de baja—, y buscar por número podía
    tomar el pieces_per_shot de la de baja.

    No atrapa errores: si la base falla, el ciclo no se registra y el
    siguiente lo reintenta completo. Suponer 1 ante un error registraba 10
    golpes de un troquel de 4 piezas como 10 piezas en vez de 40.
    """
    cursor.execute("""
        SELECT TOP(1) a.value
        FROM attributes a
        WHERE a.attributable_type = ?
          AND a.[key] = 'pieces_per_shot'
          AND a.attributable_id = ?
    """, (TIPO_ATRIBUTO, part_number_id))
    fila = cursor.fetchone()
    return _a_entero(fila[0]) if fila else 1


def _sin_espacios(numero):
    """'BDTS28BFC -P' -> 'BDTS28BFC-P'. Ver el comentario en resolver_mdi."""
    return str(numero or "").replace(" ", "")


def _a_entero(valor):
    """El atributo se guarda como texto; un valor raro no debe tumbar el conteo."""
    try:
        n = int(str(valor).strip())
        return n if n > 0 else 1
    except (TypeError, ValueError):
        return 1
