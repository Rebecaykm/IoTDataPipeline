"""
SQL de órdenes de producción (shop orders).

El modelo: la producción ya no se acumula por día y turno, sino que LLENA
ÓRDENES. Cada avance del contador busca la orden abierta más antigua de esa
parte en esa estación, la llena, y cuando llega a lo planeado la cierra y sigue
con la siguiente.

Prioridad al buscar dónde registrar, siempre dentro de la VENTANA de la
producción que se está registrando (ver ventana_de()): su mismo mes, partido en
dos por el corte de la estación si ese corte cae dentro del mes. Nunca se
avanza ni se retrocede de mes, y nunca se cruza el corte:

    1º  status 7   la orden que ya se estaba llenando, dentro de la ventana
    2º  status 3/8 con orden, dentro de la ventana, de la más antigua a la más nueva
    3º  status 7/24 o 7/14  el acumulador de esa parte+estación, dentro de la ventana
    4º               se crea el acumulador, también En progreso

El corte es `sync_from`, una fecha por estación en infor_sync_work_centers. Con
corte el 20-nov, lo producido hasta el 19 llena solo órdenes del 1 al 19 y su
sobrante va a No verificado; lo producido desde el 20 llena solo órdenes del 20
al 30 y su sobrante va a No planeado. En diciembre ya no parte nada: el mes
completo, como siempre. Sin corte, el mes completo y No verificado.

Estados de production_records:
    3  = Pendiente (el programa de producción)
    4  = Completado (llegó a planned_quantity; NO se vuelve a tocar)
    7  = En progreso
    8  = Detenido (la corrida terminó sin llenar la orden)
    24 = No planeado (producción sin orden, de una estación con `sync_from`
         ya vigente — dentro del flujo externo que reparte y cierra el mes)
    14 = No verificado (producción sin orden, de una estación sin `sync_from`
         configurado o antes de esa fecha — cuarentena, ajena a ese flujo)

Mientras una corrida está viva su registro está En progreso, tenga orden o no.
Al terminar la corrida cada uno toma el estado que le corresponde: las órdenes
pasan a Detenido, y el acumulador sin orden pasa a No planeado o a No
verificado según el `sync_from` de la estación en ese momento (ver detener()).

El reparto de un incremento entre varias órdenes NO vive aquí: es una decisión
pura y está en domain/ordenes.py. Aquí solo se lee y se escribe.

Nota de vocabulario: `incremento` es lo que avanzó el contador del PLC, sin
unidad: son GOLPES en Estampado, donde production_records lleva el incremento
multiplicado por pieces_per_shot, y PIEZAS en las demás áreas, donde el
multiplicador es 1 y ambas tablas van en piezas.
"""

import logging
from datetime import date, datetime
from typing import NamedTuple

from domain.ordenes import Capacidad, repartir
from persistence.errores import es_interbloqueo
from persistence.repositorio import obtener_part_number_id

logger = logging.getLogger("supervisor")

PENDIENTE = 3
COMPLETADO = 4
EN_PROGRESO = 7
DETENIDO = 8
NO_PLANEADO = 24
#: Cuarentena: producción sin orden de una parte que aún no se ha validado en
#: el catálogo (estación sin `sync_from` configurado, o antes de esa fecha).
#: El proceso externo que reparte y cierra el acumulador "No planeado" (24)
#: ignora este status por completo: nunca lo llena, nunca lo consolida, y su
#: cierre de mes diario solo escanea status_id IN (3, 7, 8, 24).
NO_VERIFICADO = 14

#: Tope de órdenes que se traen de una vez. Un incremento que cruce más de
#: estas órdenes es un caso patológico; el sobrante cae en el acumulador y
#: queda el aviso en el log.
MAX_ORDENES = 50


def limites_mes(fecha_plan):
    """
    [primer_día_del_mes, primer_día_del_mes_siguiente) que contiene fecha_plan.

    Es el marco de ventana_de(): nunca se cruza de mes, hacia adelante ni hacia
    atrás, sea cual sea el corte de la estación. `>=` y `<` sobre planned_date
    son sargables; YEAR()/MONTH() sobre la columna no lo serían.
    """
    inicio = fecha_plan.replace(day=1)
    mes_siguiente = inicio.month % 12 + 1
    anio_siguiente = inicio.year + (1 if inicio.month == 12 else 0)
    fin = inicio.replace(year=anio_siguiente, month=mes_siguiente)
    return inicio, fin


class Ventana(NamedTuple):
    """
    Dónde puede quedar la producción de un día: planned_date en [desde, hasta),
    y el régimen (`flavor`) de lo que no quepa en ninguna orden.
    """
    desde: date
    hasta: date
    flavor: int

    @property
    def corta_el_mes(self):
        """El corte de la estación cae dentro de este mes y le quita un pedazo."""
        return (self.desde, self.hasta) != limites_mes(self.desde)


def ventana_de(fecha_plan, sync_from):
    """
    La ventana de la producción de `fecha_plan` (la fecha del TURNO) según el
    corte de su estación, `sync_from` (None si la estación no tiene).

    El mes es siempre el marco; el corte solo sube el piso o baja el techo
    dentro de él, sin que las dos mitades se traslapen:

        sin corte                    el mes completo          No verificado
        fecha_plan antes del corte   del 1 al día previo      No verificado
        fecha_plan desde el corte    del corte al fin de mes  No planeado

    Ej., corte el 20-nov: lo del 19 (incluida la madrugada del 20, que es del
    turno del 19) va a órdenes del 1 al 19; lo del 20 al 30, a órdenes del 20 al
    30. Un corte de un mes pasado o futuro deja el mes completo.
    """
    inicio, fin = limites_mes(fecha_plan)
    if sync_from is None:
        return Ventana(inicio, fin, NO_VERIFICADO)
    if fecha_plan < sync_from:
        return Ventana(inicio, min(fin, sync_from), NO_VERIFICADO)
    return Ventana(max(inicio, sync_from), fin, NO_PLANEADO)


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
  AND pr.planned_date >= ? AND pr.planned_date < ?
  AND ( pr.status_id = {EN_PROGRESO}
        OR ( pr.status_id IN ({PENDIENTE}, {DETENIDO})
             AND pr.shop_order_number IS NOT NULL ) )
ORDER BY CASE WHEN pr.status_id = {EN_PROGRESO} THEN 0 ELSE 1 END,
         pr.planned_date,
         pr.shift_id,
         pr.created_at,
         pr.id
"""


def capacidades(cursor, estacion, numero_parte, ventana):
    """
    Órdenes abiertas de esta parte en esta estación, en orden de prioridad.

    El filtro de fecha es SIEMPRE la ventana (ver ventana_de()): el mismo mes, y
    del lado del corte que le toca. Sin excepción, ni siquiera para la orden
    que ya se estaba llenando (status 7):
      - si esa orden queda en un mes distinto, el recolector simplemente deja
        de verla; no la cierra ni la toca, eso es responsabilidad del proceso
        externo que cierra el mes;
      - si queda del otro lado del corte, en el mismo mes, ya la cerró
        cerrar_fuera_de_ventana().

    Devuelve la lista que consume domain.ordenes.repartir(). Vacía significa
    "no hay dónde registrar": la producción es sobrante (acumulador).
    """
    cursor.execute(SQL_CAPACIDADES, (estacion, numero_parte, ventana.desde, ventana.hasta))
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
    production_start = CASE WHEN status_id <> {EN_PROGRESO} OR production_start IS NULL
                            THEN ? ELSE production_start END,
    production_end = ?,
    status_id = {EN_PROGRESO}
OUTPUT inserted.produced_quantity
WHERE id = ? AND shop_order_number IS NULL
  AND status_id IN ({EN_PROGRESO}, ?)
"""


def sumar_a_no_planeado(cursor, id_registro, piezas, fecha_fmt, fecha_plan,
                        flavor=NO_PLANEADO):
    """
    Suma al acumulador de producción sin orden (No planeado o No verificado).

    Aquí NO hay tope: no existe cantidad planeada contra la cual recortar, y
    recortar contra un planned_quantity vacío borraría producción real.

    El acumulador es uno por parte+estación y ventana (ver ventana_de()) y vive
    en status En progreso mientras recibe producción, sin importar el régimen
    (`flavor`); solo se cierra —a No planeado o a No verificado, según
    corresponda— cuando la corrida termina (ver detener()). Sus fechas se
    mueven con él:

        planned_date       el día en que se produjo lo último
        production_start   cuándo empezó la corrida actual: si el registro
                           venía cerrado (en CUALQUIER status final), esta
                           suma abre una corrida nueva
        production_end     lo último que entró

    El CASE lee el status ANTERIOR al UPDATE: "no estaba ya corriendo" es lo
    que distingue "vengo cerrado, empieza corrida nueva" de "sigo en la misma
    corrida", sin importar en cuál de los dos status finales venía cerrado.

    `flavor` decide qué status CERRADO cuenta también como "mío" al buscar
    este registro (NO_PLANEADO o NO_VERIFICADO); mientras está vivo siempre
    es EN_PROGRESO, eso no cambia con el flavor.

    Devuelve el acumulado nuevo, o None si el registro ya no admitía la suma
    porque otro proceso reclamó ese acumulado para una orden. Distinguirlo
    importa: si se toma por bueno, esas piezas no quedan en ningún lado.
    """
    cursor.execute(SQL_SUMAR_A_NO_PLANEADO,
                   (piezas, fecha_plan, fecha_fmt, fecha_fmt, id_registro, flavor))
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


SQL_DETENER = f"""
UPDATE pr
SET pr.status_id = CASE
                      WHEN pr.shop_order_number IS NOT NULL THEN {DETENIDO}
                      WHEN pr.planned_date >= isw.sync_from THEN {NO_PLANEADO}
                      ELSE {NO_VERIFICADO}
                    END,
    pr.production_end = ?
FROM production_records pr
JOIN part_numbers pn ON pr.part_number_id = pn.id
LEFT JOIN infor_sync_work_centers isw ON isw.work_center_id = pn.work_center_id
WHERE pr.id = ? AND pr.status_id = {EN_PROGRESO}
"""

SQL_DETENER_ESTACION = f"""
UPDATE pr
SET pr.status_id = CASE
                      WHEN pr.shop_order_number IS NOT NULL THEN {DETENIDO}
                      WHEN pr.planned_date >= isw.sync_from THEN {NO_PLANEADO}
                      ELSE {NO_VERIFICADO}
                    END,
    pr.production_end = ?
FROM production_records pr
JOIN part_numbers pn ON pr.part_number_id = pn.id
JOIN work_centers wc ON pn.work_center_id = wc.id
LEFT JOIN infor_sync_work_centers isw ON isw.work_center_id = wc.id
WHERE wc.name = ? AND pr.status_id = {EN_PROGRESO}
"""


def detener(cursor, id_registro, fecha_fmt):
    """
    Cierra el registro de una corrida que terminó.

    El estado final lo decide el propio registro:
      - con orden  → Detenido, porque quedó a medio llenar y se puede retomar
      - sin orden, estación con sync_from ya pasado → No planeado
      - sin orden, sin sync_from o antes de esa fecha → No verificado

    El `LEFT JOIN` a infor_sync_work_centers es lo que decide esto último sin
    parámetros: si la estación no tiene fila, `isw.sync_from` es NULL, la
    comparación `>=` nunca es verdadera, y el CASE cae al No verificado.

    Solo desde En progreso: una orden Pendiente que nunca se empezó sigue
    siendo plan, y una Completada ya no se toca.
    """
    cursor.execute(SQL_DETENER, (fecha_fmt, id_registro))
    return cursor.rowcount


def detener_estacion(cursor, estacion, fecha_fmt):
    """
    Cierra todo lo que la estación tenía en progreso (dejó de reportar).

    Igual que detener(): las órdenes a medias quedan Detenidas, y el
    acumulador sin orden queda en No planeado o No verificado según el
    `sync_from` de la estación.
    """
    cursor.execute(SQL_DETENER_ESTACION, (fecha_fmt, estacion))
    return cursor.rowcount


SQL_VIVOS_FUERA_DE_VENTANA = f"""
SELECT pr.id, pr.shop_order_number
FROM production_records pr
JOIN part_numbers pn ON pr.part_number_id = pn.id
JOIN work_centers wc ON pn.work_center_id = wc.id
WHERE wc.name = ?
  AND REPLACE(pn.number, ' ', '') = ?
  AND pr.status_id = {EN_PROGRESO}
  AND pr.planned_date >= ? AND pr.planned_date < ?
  AND NOT (pr.planned_date >= ? AND pr.planned_date < ?)
"""


def cerrar_fuera_de_ventana(cursor, estacion, numero_parte, ventana, fecha_fmt, log=logger):
    """
    Al cruzar el corte de la estación, cierra lo que esta parte dejó En progreso
    del otro lado del corte, dentro del mismo mes, con detener(): la orden pasa
    a Detenido y el acumulador a No verificado (o a No planeado), igual que en
    cualquier fin de corrida. Su producción no se mueve.

    Primero solo busca (una lectura, como la de capacidades()), y escribe
    únicamente si encontró algo: eso pasa una vez, al cruzar el corte.

    Ej., corte el 20-nov: a las 08:00 del 20 la prensa venía llenando la orden
    del 18 (En progreso). Desde ahí ya no puede llenarla, así que se cierra
    Detenida y lo que sigue va a las órdenes del 20 en adelante. Sin esto se
    quedaría en 7, porque el fin de corrida solo cierra el registro que se está
    llenando.

    Lo de OTRO mes no se toca aquí. Una orden de otro mes la cierra el proceso
    externo del cierre de mes, y un acumulador de otro mes lo cierra
    acumulador_no_planeado() antes de abrir el de este.

    Solo hace falta cuando el corte cae dentro del mes (Ventana.corta_el_mes);
    quien llama se ahorra la consulta en cualquier otro caso.
    """
    mes_inicio, mes_fin = limites_mes(ventana.desde)
    cursor.execute(SQL_VIVOS_FUERA_DE_VENTANA,
                   (estacion, numero_parte, mes_inicio, mes_fin, ventana.desde, ventana.hasta))
    vivos = cursor.fetchall()
    for id_registro, orden in vivos:
        detener(cursor, id_registro, fecha_fmt)
        log.info(
            f"✂️ Corte de {estacion}: {numero_parte} cruzó la fecha de la estación; "
            f"{'la orden ' + orden if orden else 'el acumulador'} (registro {id_registro}) "
            f"quedó del otro lado y se cierra como fin de corrida."
        )
    return len(vivos)


# ───────────────────────────── no planeado ────────────────────────────────

SQL_SYNC_FROM = """
SELECT TOP(1) isw.sync_from
FROM infor_sync_work_centers isw
JOIN work_centers wc ON isw.work_center_id = wc.id
WHERE wc.name = ?
ORDER BY isw.updated_at DESC
"""


def obtener_sync_from(cursor, estacion):
    """
    Corte configurado para esta estación en infor_sync_work_centers, o None.

    Se consulta FRESCA en cada ciclo (no se cachea): el corte se edita desde
    una interfaz web ajena a este repo y debe surtir efecto sin reiniciar el
    servicio. None significa "sin fila" — hoy es el caso de casi toda la
    planta — y se trata como régimen No verificado (ver Prensas.py).
    """
    cursor.execute(SQL_SYNC_FROM, (estacion,))
    fila = cursor.fetchone()
    if not fila or fila[0] is None:
        return None
    valor = fila[0]
    return valor.date() if isinstance(valor, datetime) else valor


SQL_BUSCAR_NO_PLANEADO = f"""
SELECT TOP(1) pr.id
FROM production_records pr
JOIN part_numbers pn ON pr.part_number_id = pn.id
JOIN work_centers wc ON pn.work_center_id = wc.id
WHERE wc.name = ?
  AND REPLACE(pn.number, ' ', '') = ?
  AND pr.shop_order_number IS NULL
  AND pr.status_id IN ({EN_PROGRESO}, ?)
  AND pr.planned_date >= ? AND pr.planned_date < ?
ORDER BY pr.id DESC
"""

SQL_BUSCAR_VENCIDO = f"""
SELECT TOP(1) pr.id
FROM production_records pr
JOIN part_numbers pn ON pr.part_number_id = pn.id
JOIN work_centers wc ON pn.work_center_id = wc.id
WHERE wc.name = ?
  AND REPLACE(pn.number, ' ', '') = ?
  AND pr.shop_order_number IS NULL
  AND pr.status_id = {EN_PROGRESO}
  AND NOT (pr.planned_date >= ? AND pr.planned_date < ?)
"""

SQL_CREAR_NO_PLANEADO = f"""
INSERT INTO production_records
       (part_number_id, produced_quantity, shift_id, production_start,
        production_end, status_id, planned_date)
OUTPUT INSERTED.id
VALUES (?, 0, ?, ?, ?, {EN_PROGRESO}, ?)
"""


def acumulador_no_planeado(cursor, estacion, numero_parte, fecha_plan,
                           turno, fecha_fmt, ventana, log=logger):
    """
    Devuelve el acumulador de sobrante de esta parte+estación en su ventana,
    creándolo la primera vez.

    Es uno por parte+estación y ventana: el mes (antes era "para siempre"), o
    la mitad del mes que le toca si la estación tiene corte dentro de él. El
    rollover de mes es automático, nadie tiene que cerrar nada a mano — ver el
    paso 2 de abajo, que es justo lo que hace ese cierre cuando la corrida nunca
    "soltó" la parte en el PLC al cruzar de mes.

    `ventana.flavor` (NO_PLANEADO o NO_VERIFICADO) decide a cuál de los dos
    regímenes pertenece el acumulador que se busca/crea; mientras está vivo
    siempre es En progreso (ver sumar_a_no_planeado), y se cierra —a 24 o a 14,
    según el sync_from de la estación— exactamente igual que cualquier corrida
    (ver detener()).

    Pasos:
      1. ¿Ya hay uno en ESTA ventana y este régimen (vivo o ya cerrado)? Se
         reutiliza.
      2. ¿Quedó uno VIVO de un mes anterior, porque la corrida nunca se
         detectó como terminada? Se cierra con detener() (mismo criterio que
         cualquier fin de corrida) antes de seguir. El que quedó vivo del otro
         lado del corte, en este mismo mes, ya lo cerró cerrar_fuera_de_ventana().
      3. Se crea uno nuevo, En progreso, en cero, con planned_date = fecha_plan
         (dentro de la ventana).

    Devuelve (id_registro, error). El error es un código para el tablero de
    rechazos cuando la parte ni siquiera existe en el catálogo.
    """
    mes_inicio, mes_fin = limites_mes(fecha_plan)

    cursor.execute(SQL_BUSCAR_NO_PLANEADO,
                   (estacion, numero_parte, ventana.flavor, ventana.desde, ventana.hasta))
    fila = cursor.fetchone()
    if fila:
        return fila[0], None

    cursor.execute(SQL_BUSCAR_VENCIDO, (estacion, numero_parte, mes_inicio, mes_fin))
    vencido = cursor.fetchone()
    if vencido:
        log.info(
            f"📅 Acumulador {vencido[0]} de {numero_parte} en {estacion} quedó "
            f"de un mes anterior: se cierra antes de abrir el de este mes."
        )
        detener(cursor, vencido[0], fecha_fmt)

    try:
        part_number_id = obtener_part_number_id(cursor, numero_parte, estacion)
        if part_number_id is None:
            log.warning(f"⚠️ No se pudo crear el acumulador: {numero_parte} no existe en {estacion}")
            return None, "PART_NUMBER_NO_EXISTE_BD"
        cursor.execute(SQL_CREAR_NO_PLANEADO,
                       (part_number_id, turno, fecha_fmt, fecha_fmt, fecha_plan))
        fila = cursor.fetchone()
        log.info(f"🆕 Acumulador sin orden {fila[0]} para {numero_parte} en {estacion}")
        return fila[0], None
    except Exception as e:
        # El interbloqueo ya revirtió la transacción entera: devolverlo como
        # un error más dejaría seguir el ciclo y confirmar solo lo de después.
        if es_interbloqueo(e):
            raise
        log.error(f"❌ Error creando acumulador para {numero_parte}: {e}")
        return None, "DB_ERROR"


# ─────────────────────────────── orquestación ─────────────────────────────

class RegistroIncompleto(Exception):
    """
    Parte del incremento ya se escribió y el resto no encontró dónde quedar.

    Solo pasa por una carrera con otro proceso: una orden o el acumulador
    cambiaron entre la consulta y la escritura. Confirmar lo ya escrito sin
    mover la línea base haría que el siguiente ciclo lo registrara otra vez,
    así que quien llama debe revertir la transacción completa.
    """


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
                         fecha_plan, turno, fecha_fmt, sync_from,
                         anotar_history=None, log=logger):
    """
    Registra un incremento del contador llenando órdenes.

    Es el único camino por el que entra producción a la base. Lee las órdenes
    abiertas, deja que domain.ordenes.repartir() decida el reparto, y escribe.

    `sync_from` es el corte de la estación (None si no tiene), y lo lee quien
    llama, fresco, una vez por estación por ciclo. De él y de `fecha_plan` sale
    la ventana (ver ventana_de()): qué órdenes se pueden llenar y a qué régimen
    va el sobrante sin orden. Si el corte cae dentro del mes, antes de nada se
    cierra lo que esta parte dejó En progreso del otro lado
    (ver cerrar_fuera_de_ventana()).

    `anotar_history(id_registro, orden, incremento)` lo pone quien llama, porque
    el detalle de histories depende del área (el troquel en Estampado) y de datos
    que aquí no se conocen. Solo se invoca cuando hay algo que registrar: las
    piezas que sobran de una unidad ya contabilizada NO generan fila.

    Garantía: o se escribe el incremento COMPLETO —incremento x multiplicador,
    lo que no cabe en ninguna orden en el acumulador— o no se escribe nada:

      - si se sabe de antemano que no hay dónde poner el sobrante, se devuelve
        `error` sin haber tocado ninguna orden;
      - si se descubre a media escritura (una carrera con otro proceso), se
        lanza RegistroIncompleto para que quien llama revierta todo.

    Antes las órdenes se llenaban primero y el sobrante fallaba después: esas
    piezas quedaban confirmadas con la línea base sin mover, y el siguiente
    ciclo las volvía a registrar enteras.
    """
    resultado = Resultado()
    if incremento <= 0:
        return resultado

    ventana = ventana_de(fecha_plan, sync_from)
    if ventana.corta_el_mes:
        cerrar_fuera_de_ventana(cursor, estacion, numero_parte, ventana, fecha_fmt, log)
    disponibles = capacidades(cursor, estacion, numero_parte, ventana)
    reparto = repartir(incremento, multiplicador, disponibles)

    id_acumulador = None
    if any(a.es_no_planeado for a in reparto):
        id_acumulador, error = acumulador_no_planeado(
            cursor, estacion, numero_parte, fecha_plan, turno, fecha_fmt, ventana, log)
        if id_acumulador is None:
            return _sin_donde_registrar(resultado, error, incremento,
                                        numero_parte, estacion, log)

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

    # Lo que no cupo en ninguna orden. Si el reparto no lo preveía, es que una
    # orden se cerró entre la consulta y el UPDATE: el acumulador se pide aquí.
    if id_acumulador is None:
        id_acumulador, error = acumulador_no_planeado(
            cursor, estacion, numero_parte, fecha_plan, turno, fecha_fmt, ventana, log)
        if id_acumulador is None:
            return _sin_donde_registrar(resultado, error, incremento,
                                        numero_parte, estacion, log)

    if sumar_a_no_planeado(cursor, id_acumulador, piezas_sueltas,
                           fecha_fmt, fecha_plan, ventana.flavor) is None:
        # Otro proceso reclamó ese acumulado para una orden entre la búsqueda y
        # el UPDATE: le puso número de orden, así que ya no admite producción
        # suelta. Se pide otro, que nacerá limpio, y se reintenta UNA vez.
        log.warning(
            f"⚠️ El acumulador {id_acumulador} de {numero_parte} fue reclamado por "
            f"una orden; se abre otro para las {piezas_sueltas} piezas sueltas."
        )
        id_acumulador, error = acumulador_no_planeado(
            cursor, estacion, numero_parte, fecha_plan, turno,
            fecha_fmt, ventana, log)
        if id_acumulador is None or sumar_a_no_planeado(
                cursor, id_acumulador, piezas_sueltas, fecha_fmt, fecha_plan, ventana.flavor) is None:
            return _sin_donde_registrar(resultado, error or "ACUMULADOR_NO_DISPONIBLE",
                                        incremento, numero_parte, estacion, log)

    resultado.piezas += piezas_sueltas
    resultado.no_planeado = piezas_sueltas
    # Queda como lo que está abierto: si hubo sobrante es que ninguna orden
    # quedó a medias, y es este acumulador el que el fin de corrida debe cerrar.
    resultado.en_curso = id_acumulador

    if incremento_suelto and anotar_history:
        anotar_history(id_acumulador, None, incremento_suelto)
    resultado.incremento += incremento_suelto

    regimen = "No planeado" if ventana.flavor == NO_PLANEADO else "No verificado"
    log.info(
        f"📦 {piezas_sueltas} piezas de {numero_parte} en {estacion} sin orden "
        f"(de un avance de {incremento} este ciclo): van al acumulador "
        f"{id_acumulador} (régimen {regimen})."
    )
    return resultado


def _sin_donde_registrar(resultado, error, incremento, numero_parte, estacion, log):
    """
    El sobrante no tiene dónde quedar. Si todavía no se escribió nada, se
    reporta como error y la línea base no se mueve; si ya se escribió parte,
    se aborta la transacción (ver RegistroIncompleto).
    """
    if resultado.piezas:
        raise RegistroIncompleto(
            f"{numero_parte} en {estacion}: {resultado.piezas} piezas ya escritas "
            f"en órdenes y el resto sin dónde registrarse ({error}). Se revierte "
            f"el ciclo completo; el siguiente reintenta con el delta completo."
        )
    resultado.error = error
    log.error(
        f"❌ {numero_parte} en {estacion}: un avance de {incremento} sin dónde "
        f"registrarse ({error}). No se escribió nada: la línea base no se mueve "
        f"y el siguiente ciclo reintenta con el delta completo."
    )
    return resultado
