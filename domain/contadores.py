"""Conteo de producción a partir del contador del PLC."""

from typing import Tuple


def calcular_incremento(contador_previo: int, contador_actual: int) -> Tuple[int, bool]:
    """
    Lo que avanzó el contador del PLC entre dos lecturas: golpes en Estampado,
    piezas en las demás áreas.

    Devuelve (incremento, hubo_reset).

    El contador del PLC se reinicia cuando el operador termina una corrida y
    arranca otra con la misma parte. Si el contador bajó, lo producido antes del
    reinicio ya está acumulado en la base de datos, así que el valor nuevo es
    avance de la corrida nueva y se cuenta completo.

    >>> calcular_incremento(20, 23)
    (3, False)
    >>> calcular_incremento(64, 3)
    (3, True)
    """
    if contador_actual >= contador_previo:
        return contador_actual - contador_previo, False
    return contador_actual, True
