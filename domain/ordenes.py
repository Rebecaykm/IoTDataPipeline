"""
Reparto de la producción entre órdenes (shop orders).

Un incremento del contador puede cruzar la frontera de una orden: si a la orden
le faltan 60 piezas y llegan 80, las primeras 60 la cierran y las 20 restantes
van a la siguiente.

Qué cuenta el contador del PLC depende del área, y de ahí sale toda la
complicación de este módulo:

    Estampado          el contador cuenta GOLPES del troquel. Cada golpe saca
                       varias piezas (pieces_per_shot). Por eso
                           histories          <- golpes
                           production_records <- golpes x multiplicador = piezas

    Las demás áreas    el contador cuenta PIEZAS directamente. El multiplicador
                       es 1 y ambas tablas van en piezas. Aquí no existen los
                       golpes.

Para no dar por hecho una u otra cosa, en este módulo el incremento crudo del
contador se llama `incremento`, sin unidad. La regla que le da forma a todo:

    Una unidad del contador es INDIVISIBLE: se registra entera en la orden que
    tenía hueco cuando cayó.

En Estampado eso significa que un golpe no se parte: si de un golpe salen 2
piezas y a la orden solo le cabía 1, el golpe completo queda atribuido a esa
orden en `histories` y la pieza sobrante pasa a la siguiente orden SIN generar
otra fila de historial. En las demás áreas el multiplicador es 1, nunca hay
sobrantes, y la regla no se nota.
"""

from dataclasses import dataclass
from math import ceil
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Capacidad:
    """Una orden abierta y cuántas piezas le faltan para cerrarse."""
    id_registro: int
    orden: Optional[str]
    falta: int


@dataclass(frozen=True)
class Asignacion:
    """
    Lo que le toca a una orden de este incremento.

    - `piezas`      suma a production_records.produced_quantity
    - `incremento`  es la fila de histories (golpes en Estampado, piezas en las
                    demás áreas); 0 significa que NO se registra fila, porque
                    son piezas sobrantes de una unidad ya contabilizada antes
    - `cierra`      la orden llegó a su cantidad planeada y pasa a Completado
    - `orden` e `id_registro` en None significan "no planeado": no había
      ninguna orden con hueco y hay que crear o usar el registro status 24
    """
    id_registro: Optional[int]
    orden: Optional[str]
    incremento: int
    piezas: int
    cierra: bool

    @property
    def es_no_planeado(self) -> bool:
        return self.id_registro is None


def repartir(incremento: int,
             multiplicador: int,
             capacidades: Sequence[Capacidad]) -> List[Asignacion]:
    """
    Reparte un incremento del contador entre las órdenes abiertas.

    `capacidades` viene ya ordenada por la consulta (la más antigua primero).
    Devuelve una asignación por orden tocada; la última puede ser "no planeado"
    si el incremento no cupo en ninguna.

    Con multiplicador 1 —todas las áreas menos Estampado— el reparto es directo:
    tantas piezas como marque el contador.

    >>> repartir(20, 4, [Capacidad(1, "A", 60), Capacidad(2, "B", 1000)])
    [Asignacion(id_registro=1, orden='A', incremento=15, piezas=60, cierra=True),
     Asignacion(id_registro=2, orden='B', incremento=5, piezas=20, cierra=False)]
    """
    mult = max(int(multiplicador or 1), 1)
    restante = max(int(incremento or 0), 0)
    if restante == 0:
        return []

    asignaciones: List[Asignacion] = []
    sueltas = 0   # piezas de una unidad YA registrada que aún no encuentran orden

    for cap in capacidades:
        if restante == 0 and sueltas == 0:
            break
        falta = max(int(cap.falta or 0), 0)
        if falta == 0:
            continue

        piezas = 0
        aqui = 0

        # 1) Primero acomodar las piezas sueltas de la unidad anterior.
        if sueltas > 0:
            toma = min(sueltas, falta)
            piezas += toma
            sueltas -= toma
            falta -= toma

        # 2) Luego unidades completas: las que alcanzan a EMPEZAR con hueco.
        if falta > 0 and restante > 0:
            caben = ceil(falta / mult)
            aqui = min(restante, caben)
            producidas = aqui * mult
            toma = min(producidas, falta)
            piezas += toma
            sueltas += producidas - toma
            restante -= aqui
            falta -= toma

        if piezas or aqui:
            asignaciones.append(Asignacion(
                id_registro=cap.id_registro, orden=cap.orden,
                incremento=aqui, piezas=piezas, cierra=(falta == 0),
            ))

    # 3) Lo que no cupo en ninguna orden va al registro No planeado.
    if restante > 0 or sueltas > 0:
        asignaciones.append(Asignacion(
            id_registro=None, orden=None,
            incremento=restante, piezas=restante * mult + sueltas, cierra=False,
        ))

    return asignaciones


def totales(asignaciones: Sequence[Asignacion]) -> Tuple[int, int]:
    """(incremento, piezas) de un reparto. Sirve para comprobar que nada se pierde."""
    return (sum(a.incremento for a in asignaciones),
            sum(a.piezas for a in asignaciones))
