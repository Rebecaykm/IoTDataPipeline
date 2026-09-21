"""
SQL de órdenes de producción (shop orders).

El modelo: la producción ya no se acumula por día y turno, sino que LLENA
ÓRDENES. Cada avance del contador busca la orden abierta más antigua de esa
parte en esa estación, la llena, y cuando llega a lo planeado la cierra y sigue
con la siguiente.

Prioridad al buscar dónde registrar:

    1º  status 7   la orden que ya se estaba llenando  (sin importar su fecha)
    2º  status 3/8 con orden, de la más antigua a la más nueva
    3º  status 7/24  el acumulador de esa parte, uno solo y para siempre
    4º               se crea el acumulador, también En progreso

Estados de production_records:
    3  = Pendiente (el programa de producción)
    4  = Completado (llegó a planned_quantity; NO se vuelve a tocar)
    7  = En progreso
    8  = Detenido (la corrida terminó sin llenar la orden)
    24 = No planeado (producción sin orden que la respalde, ya terminada)

Mientras una corrida está viva su registro está En progreso, tenga orden o no.
Al terminar la corrida cada uno toma el estado que le corresponde: las órdenes
pasan a Detenido y el acumulador sin orden pasa a No planeado.

El reparto de un incremento entre varias órdenes NO vive aquí: es una decisión
pura y está en domain/ordenes.py. Aquí solo se lee y se escribe.

Nota de vocabulario: `incremento` es lo que avanzó el contador del PLC, sin
unidad: son GOLPES en Estampado, donde production_records lleva el incremento
multiplicado por pieces_per_shot, y PIEZAS en las demás áreas, donde el
multiplicador es 1 y ambas tablas van en piezas.
"""

import logging

from domain.ordenes import Capacidad, repartir

logger = logging.getLogger("supervisor")

PENDIENTE = 3
COMPLETADO = 4
EN_PROGRESO = 7
DETENIDO = 8
NO_PLANEADO = 24

#: Tope de órdenes que se traen de una vez. Un incremento que cruce más de
#: estas órdenes es un caso patológico; el sobrante cae en No planeado y queda
#: el aviso en el log.
MAX_ORDENES = 50


# ──────────────────────────────── lectura ─────────────────────────────────

SQL_CAPACIDADES = f"""
SELECT TOP({MAX_ORDENES})
       pr.id,
       pr.shop_order_number,
       pr.planned_quantity - pr.produced_quantity AS falta
FROM production_records pr
JOIN part_numbers pn ON pr.part_number_id = pn.id
JOIN work_centers wc ON pn.work_center_id = wc.id
WHERE wc.name = ?
  AND REPLACE(pn.number, ' ', '') = ?
  AND ISNULL(pr.synced_to_infor, 0) <> 1
  AND pr.produced_quantity < pr.planned_quantity
  AND ( pr.status_id = {EN_PROGRESO}
        OR ( pr.status_id IN ({PENDIENTE}, {DETENIDO})
             AND pr.shop_order_number IS NOT NULL
             AND pr.planned_date >= ? ) )
ORDER BY CASE WHEN pr.status_id = {EN_PROGRESO} THEN 0 ELSE 1 END,
         pr.planned_date,
         pr.shift_id,
         pr.created_at,
         pr.id
"""


def capacidades(cursor, estacion, numero_parte, desde_fecha):
    """
    Órdenes abiertas de esta parte en esta estación, en orden de prioridad.

    `desde_fecha` acota qué tan atrás se rescatan órdenes pendientes; la orden
    que ya se estaba llenando (status 7) se retoma sin importar su fecha,
    porque abandonarla a media corrida dejaría producción huérfana.

    Devuelve la lista que consume domain.ordenes.repartir(). Vacía significa
    "no hay dónde registrar": la producción es No planeado.
    """
    cursor.execute(SQL_CAPACIDADES, (estacion, numero_parte, desde_fecha))
    return [Capacidad(id_registro=f[0], orden=f[1], falta=int(f[2] or 0))
            for f in cursor.fetchall()]


SQL_CORRIDA_EN_PROGRESO = f"""
SELECT TOP(1) pr.id
FROM production_records pr
JOIN part_numbers pn ON pr.part_number_id = pn.id
JOIN work_centers wc ON pn.work_center_id = wc.id
WHERE wc.name = ?
  AND REPLACE(pn.number, ' ', '') = ?
  AND pr.status_id = {EN_PROGRESO}
"""


def hay_corrida_en_progreso(cursor, estacion, numero_parte):
    """
    ¿Ya había alguien contando esta parte en esta estación?

    Decide la línea base cuando una parte aparece sin estado previo:

      - SÍ  → el contador del PLC ya venía contando para una orden abierta;
              la línea base es el valor actual y solo se registra lo que siga.
      - NO  → es una corrida nueva; el valor del contador ES producción de esta
              corrida y hay que registrarlo.

    Sin esto, un reinicio del servicio a media corrida duplicaría lo ya
    registrado o, al revés, perdería la producción de un arranque en frío.
    """
    cursor.execute(SQL_CORRIDA_EN_PROGRESO, (estacion, numero_parte))
    return cursor.fetchone() is not None


# ─────────────────────────────── escritura ────────────────────────────────

SQL_SUMAR_A_ORDEN = f"""
UPDATE production_records
SET produced_quantity = CASE
        WHEN produced_quantity + ? > planned_quantity THEN planned_quantity
        ELSE produced_quantity + ?
    END,
    production_start = ISNULL(production_start, ?),
    production_end = ?,
    status_id = {EN_PROGRESO}
OUTPUT deleted.produced_quantity, inserted.produced_quantity, inserted.planned_quantity
WHERE id = ? AND status_id IN ({PENDIENTE}, {EN_PROGRESO}, {DETENIDO})
"""


def sumar_a_orden(cursor, id_registro, piezas, fecha_fmt):
    """
    Suma piezas a una orden sin dejar que se pase de lo planeado.

    El tope lo pone SQL Server dentro del mismo UPDATE, no Python: si los dos
    lados de la estación llenan la misma orden, el segundo ve el hueco que
    dejó el primero y no hay forma de que entre uno y otro se cuele un exceso.

    Devuelve (aplicadas, lleno):
      - `aplicadas` puede ser MENOR que `piezas` si el tope recortó; el sobrante
        es responsabilidad de quien llama (va a la siguiente orden o a
        No planeado), nunca se descarta.
      - `lleno` indica que la orden alcanzó lo planeado y toca cerrarla.
    """
    cursor.execute(SQL_SUMAR_A_ORDEN,
                   (piezas, piezas, fecha_fmt, fecha_fmt, id_registro))
    fila = cursor.fetchone()
    if fila is None:
        # La orden cambió de estado entre la consulta y el UPDATE (otro proceso
        # la cerró). Nada se aplicó; el llamador reubica las piezas.
        return 0, False
    antes, despues, planeada = int(fila[0] or 0), int(fila[1] or 0), int(fila[2] or 0)
    return despues - antes, despues >= planeada


SQL_SUMAR_A_NO_PLANEADO = f"""
UPDATE production_records
SET produced_quantity = produced_quantity + ?,
    planned_date = ?,
    production_start = CASE WHEN status_id = {NO_PLANEADO} OR production_start IS NULL
                            THEN ? ELSE production_start END,
    production_end = ?,
    status_id = {EN_PROGRESO}
OUTPUT inserted.produced_quantity
WHERE id = ? AND shop_order_number IS NULL
  AND status_id IN ({EN_PROGRESO}, {NO_PLANEADO})
"""


def sumar_a_no_planeado(cursor, id_registro, piezas, fecha_fmt, fecha_plan):
    """
    Suma al acumulador de producción sin orden.

    Aquí NO hay tope: no existe cantidad planeada contra la cual recortar, y
    recortar contra un planned_quantity vacío borraría producción real.

    El acumulador es UNO por parte y no se cierra nunca del todo, así que sus
    fechas se mueven con él:

        planned_date       el día en que se produjo lo último
        production_start   cuándo empezó la corrida actual: si el registro
                           venía cerrado, esta suma abre una corrida nueva
        production_end     lo último que entró

    El CASE lee el status ANTERIOR al UPDATE, que es justo lo que distingue
    "vengo cerrado, empieza corrida nueva" de "sigo en la misma corrida".

    Devuelve el acumulado nuevo, o None si el registro ya no admitía la suma
    porque otro proceso reclamó ese acumulado para una orden. Distinguirlo
    importa: si se toma por bueno, esas piezas no quedan en ningún lado.
    """
    cursor.execute(SQL_SUMAR_A_NO_PLANEADO,
                   (piezas, fecha_plan, fecha_fmt, fecha_fmt, id_registro))
    fila = cursor.fetchone()
    return int(fila[0] or 0) if fila else None


def completar(cursor, id_registro, fecha_fmt):
    """
    Cierra una orden llena (→ Completado).

    La condición `produced >= planned` va en el WHERE a propósito: si el tope
    del UPDATE recortó la suma por una carrera, la orden no está llena y no se
    debe cerrar.
    """
    cursor.execute(
        f"UPDATE production_records SET status_id = {COMPLETADO}, production_end = ? "
        f"WHERE id = ? AND produced_quantity >= planned_quantity",
        (fecha_fmt, id_registro)
    )
    return cursor.rowcount


def detener(cursor, id_registro, fecha_fmt):
    """
    Cierra el registro de una corrida que terminó.

    El estado final lo decide el propio registro:
      - con orden  → Detenido, porque quedó a medio llenar y se puede retomar
      - sin orden  → No planeado, que es lo que de verdad fue esa producción

    Solo desde En progreso: una orden Pendiente que nunca se empezó sigue
    siendo plan, y una Completada ya no se toca.
    """
    cursor.execute(
        f"UPDATE production_records "
        f"SET status_id = CASE WHEN shop_order_number IS NULL "
        f"                     THEN {NO_PLANEADO} ELSE {DETENIDO} END, "
        f"    production_end = ? "
        f"WHERE id = ? AND status_id = {EN_PROGRESO}",
        (fecha_fmt, id_registro)
    )
    return cursor.rowcount


def detener_estacion(cursor, estacion, fecha_fmt):
    """
    Cierra todo lo que la estación tenía en progreso (dejó de reportar).

    Igual que detener(): las órdenes a medias quedan Detenidas y el acumulador
    sin orden queda como No planeado.
    """
    cursor.execute(
        f"UPDATE pr "
        f"SET pr.status_id = CASE WHEN pr.shop_order_number IS NULL "
        f"                        THEN {NO_PLANEADO} ELSE {DETENIDO} END, "
        f"    pr.production_end = ? "
        f"FROM production_records pr "
        f"JOIN part_numbers pn ON pr.part_number_id = pn.id "
        f"JOIN work_centers wc ON pn.work_center_id = wc.id "
        f"WHERE wc.name = ? AND pr.status_id = {EN_PROGRESO}",
        (fecha_fmt, estacion)
    )
    return cursor.rowcount


# ───────────────────────────── no planeado ────────────────────────────────

SQL_BUSCAR_NO_PLANEADO = f"""
SELECT TOP(1) pr.id
FROM production_records pr
JOIN part_numbers pn ON pr.part_number_id = pn.id
JOIN work_centers wc ON pn.work_center_id = wc.id
WHERE wc.name = ?
  AND REPLACE(pn.number, ' ', '') = ?
  AND pr.status_id IN ({EN_PROGRESO}, {NO_PLANEADO})
  AND pr.shop_order_number IS NULL
  AND pr.planned_date >= ?
ORDER BY pr.id DESC
"""

SQL_CREAR_NO_PLANEADO = f"""
INSERT INTO production_records
       (part_number_id, produced_quantity, shift_id, production_start,
        production_end, status_id, planned_date)
OUTPUT INSERTED.id
SELECT pn.id, 0, ?, ?, ?, {EN_PROGRESO}, ?
FROM part_numbers pn
JOIN work_centers wc ON pn.work_center_id = wc.id
WHERE REPLACE(pn.number, ' ', '') = ? AND wc.name = ? AND pn.is_obsolete = 0
"""


def acumulador_no_planeado(cursor, estacion, numero_parte, desde_fecha,
                           fecha_plan, turno, fecha_fmt, log=logger):
    """
    Devuelve el acumulador de esta parte, creándolo la primera vez.

    Es UNO por parte y estación: la producción sin orden de un número de parte
    se junta toda en el mismo registro, sin importar el día. Así, para saber
    cuánto se ha producido sin plan de una parte, se lee un renglón en vez de
    sumar decenas.

    `desde_fecha` es la misma fecha de corte que acota las órdenes (ORDENES_DESDE):
    los acumuladores anteriores a ella no se adoptan. La base arrastra cientos
    de registros sin orden de años pasados, de cuando se creaba uno por día;
    seguir llenando uno de esos mezclaría producción vieja con la de hoy.

    Sus fechas se van moviendo con la producción (ver sumar_a_no_planeado):
    planned_date queda en el último día con producción, y production_start /
    production_end acotan la última corrida.

    Nace En progreso, igual que cualquier corrida viva, y vuelve a
    No planeado cuando la corrida termina (ver detener()). Así, mirando la base
    en cualquier momento, lo que está corriendo se ve corriendo.

    Devuelve (id_registro, error). El error es un código para el tablero de
    rechazos cuando la parte ni siquiera existe en el catálogo.
    """
    cursor.execute(SQL_BUSCAR_NO_PLANEADO, (estacion, numero_parte, desde_fecha))
    fila = cursor.fetchone()
    if fila:
        return fila[0], None

    try:
        cursor.execute(SQL_CREAR_NO_PLANEADO,
                       (turno, fecha_fmt, fecha_fmt, fecha_plan, numero_parte, estacion))
        fila = cursor.fetchone()
        if fila:
            log.info(f"🆕 Acumulador sin orden {fila[0]} para {numero_parte} en {estacion}")
            return fila[0], None
        log.warning(f"⚠️ No se pudo crear No planeado: {numero_parte} no existe en {estacion}")
        return None, "PART_NUMBER_NO_EXISTE_BD"
    except Exception as e:
        log.error(f"❌ Error creando registro No planeado para {numero_parte}: {e}")
        return None, "DB_ERROR"


# ─────────────────────────────── orquestación ─────────────────────────────

class Resultado:
    """Qué acabó pasando con un incremento del contador."""

    __slots__ = ("piezas", "incremento", "cerradas", "en_curso", "no_planeado", "error")

    def __init__(self, piezas=0, incremento=0, cerradas=None, en_curso=None,
                 no_planeado=0, error=None):
        self.piezas = piezas            #: piezas escritas en production_records
        self.incremento = incremento    #: lo escrito en histories, en la unidad del área
        self.cerradas = cerradas or []  #: órdenes que llegaron a lo planeado
        self.en_curso = en_curso        #: orden que quedó abierta, si quedó alguna
        self.no_planeado = no_planeado  #: piezas que no cupieron en ninguna orden
        self.error = error

    def __repr__(self):
        return (f"Resultado(piezas={self.piezas}, incremento={self.incremento}, "
                f"cerradas={self.cerradas}, en_curso={self.en_curso}, "
                f"no_planeado={self.no_planeado}, error={self.error!r})")


def registrar_incremento(cursor, estacion, numero_parte, incremento, multiplicador,
                         desde_fecha, fecha_plan, turno, fecha_fmt,
                         anotar_history=None, log=logger):
    """
    Registra un incremento del contador llenando órdenes.

    Es el único camino por el que entra producción a la base. Lee las órdenes
    abiertas, deja que domain.ordenes.repartir() decida el reparto, y escribe.

    `anotar_history(id_registro, orden, incremento)` lo pone quien llama, porque
    el detalle de histories depende del área (el troquel en Estampado) y de datos
    que aquí no se conocen. Solo se invoca cuando hay algo que registrar: las
    piezas que sobran de una unidad ya contabilizada NO generan fila.

    Garantía: la suma de lo escrito es siempre incremento x multiplicador. Lo
    que no cabe en ninguna orden termina en el acumulador No planeado, nunca en
    la basura.
    """
    resultado = Resultado()
    if incremento <= 0:
        return resultado

    disponibles = capacidades(cursor, estacion, numero_parte, desde_fecha)
    reparto = repartir(incremento, multiplicador, disponibles)

    piezas_sueltas = 0   # lo que rebotó de alguna orden y hay que reubicar
    incremento_suelto = 0

    for asignacion in reparto:
        if asignacion.es_no_planeado:
            piezas_sueltas += asignacion.piezas
            incremento_suelto += asignacion.incremento
            continue

        aplicadas, lleno = sumar_a_orden(
            cursor, asignacion.id_registro, asignacion.piezas, fecha_fmt)

        if aplicadas == 0:
            # La orden se cerró entre la consulta y el UPDATE: la producción no
            # cayó aquí después de todo, así que tampoco se anota su historial.
            log.warning(
                f"⚠️ La orden {asignacion.orden} (registro {asignacion.id_registro}) "
                f"ya no admitía producción: se reubican {asignacion.piezas} piezas."
            )
            piezas_sueltas += asignacion.piezas
            incremento_suelto += asignacion.incremento
            continue

        if aplicadas < asignacion.piezas:
            log.warning(
                f"⚠️ La orden {asignacion.orden} solo admitió {aplicadas} de "
                f"{asignacion.piezas} piezas: el resto pasa a la siguiente."
            )
            piezas_sueltas += asignacion.piezas - aplicadas

        resultado.piezas += aplicadas

        if asignacion.incremento and anotar_history:
            anotar_history(asignacion.id_registro, asignacion.orden, asignacion.incremento)
        resultado.incremento += asignacion.incremento

        if lleno:
            completar(cursor, asignacion.id_registro, fecha_fmt)
            resultado.cerradas.append(asignacion.orden)
            log.info(
                f"✅ Orden {asignacion.orden} completada en {estacion} "
                f"con {numero_parte} (registro {asignacion.id_registro})."
            )
        else:
            resultado.en_curso = asignacion.id_registro

    if piezas_sueltas <= 0 and incremento_suelto <= 0:
        return resultado

    # Lo que no cupo en ninguna orden.
    id_acumulador, error = acumulador_no_planeado(
        cursor, estacion, numero_parte, desde_fecha, fecha_plan, turno, fecha_fmt, log)

    if id_acumulador is None:
        resultado.error = error
        log.error(
            f"❌ {piezas_sueltas} piezas de {numero_parte} en {estacion} sin dónde "
            f"registrarse ({error})."
        )
        return resultado

    if sumar_a_no_planeado(cursor, id_acumulador, piezas_sueltas,
                           fecha_fmt, fecha_plan) is None:
        # Otro proceso reclamó ese acumulado para una orden entre la búsqueda y
        # el UPDATE: le puso número de orden, así que ya no admite producción
        # suelta. Se pide otro, que nacerá limpio, y se reintenta UNA vez.
        log.warning(
            f"⚠️ El acumulador {id_acumulador} de {numero_parte} fue reclamado por "
            f"una orden; se abre otro para las {piezas_sueltas} piezas sueltas."
        )
        id_acumulador, error = acumulador_no_planeado(
            cursor, estacion, numero_parte, desde_fecha, fecha_plan, turno,
            fecha_fmt, log)
        if id_acumulador is None or sumar_a_no_planeado(
                cursor, id_acumulador, piezas_sueltas, fecha_fmt, fecha_plan) is None:
            resultado.error = error or "ACUMULADOR_NO_DISPONIBLE"
            log.error(
                f"❌ {piezas_sueltas} piezas de {numero_parte} en {estacion} sin dónde "
                f"registrarse ({resultado.error}). La línea base no se mueve: "
                f"el siguiente ciclo reintenta con el delta completo."
            )
            return resultado

    resultado.piezas += piezas_sueltas
    resultado.no_planeado = piezas_sueltas
    # Queda como lo que está abierto: si hubo sobrante es que ninguna orden
    # quedó a medias, y es este acumulador el que el fin de corrida debe cerrar.
    resultado.en_curso = id_acumulador

    if incremento_suelto and anotar_history:
        anotar_history(id_acumulador, None, incremento_suelto)
    resultado.incremento += incremento_suelto

    log.info(
        f"📦 {piezas_sueltas} piezas de {numero_parte} en {estacion} sin orden: "
        f"van al acumulador {id_acumulador} (quedará como No planeado al terminar)."
    )
    return resultado
