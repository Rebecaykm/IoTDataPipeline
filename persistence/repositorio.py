"""
SQL de catálogo y de detalle: part_numbers e histories.

Lo de production_records vive en persistence/ordenes.py, porque la producción
ya no se acumula por día y turno sino que LLENA ÓRDENES, y esa es una regla
con suficiente peso propio para tener su módulo.
"""

import logging

logger = logging.getLogger("supervisor")


def _sin_multiplicador(cursor, numero_parte, estacion, log):
    """Por defecto un golpe es una pieza. Estampado inyecta su propio lector."""
    return 1


# ─────────────────────────────── part_numbers ──────────────────────────────

def obtener_part_number_id(cursor, numero_parte, estacion):
    sql = ("SELECT pn.id FROM part_numbers pn "
           "JOIN work_centers wc ON pn.work_center_id = wc.id "
           "WHERE REPLACE(pn.number, ' ', '')=? AND wc.name=?")
    cursor.execute(sql, (numero_parte, estacion))
    res = cursor.fetchone()
    return res[0] if res else None


# ──────────────────────────────── histories ────────────────────────────────

def insertar_history(cursor, part_number_id, cantidad, fecha_fmt, tiempo,
                     sequence=None, shop_order_number=None):
    """
    Registra el detalle de cada avance del contador.

    `cantidad` es el incremento CRUDO del contador del PLC: golpes en Estampado,
    piezas en las demás áreas. production_records siempre guarda piezas.
    `sequence` lleva el troquel en Estampado; en las demás áreas va nulo.
    `shop_order_number` es la orden a la que se atribuyó el golpe, y va nulo
    cuando la producción no tenía orden que la respaldara.

    Las columnas ausentes no se escriben en vez de escribirse en NULL, para no
    pisar valores que pueda poner otro proceso.
    """
    columnas = ["part_number_id", "quantity", "created_at", "production_per_cycle"]
    valores = [part_number_id, cantidad, fecha_fmt, tiempo]

    if sequence is not None:
        columnas.append("sequence")
        valores.append(sequence)
    if shop_order_number is not None:
        columnas.append("shop_order_number")
        valores.append(shop_order_number)

    cursor.execute(
        f"INSERT INTO histories ({', '.join(columnas)}) "
        f"VALUES ({', '.join(['?'] * len(columnas))})",
        tuple(valores)
    )
