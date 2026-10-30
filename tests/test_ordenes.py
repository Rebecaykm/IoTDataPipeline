"""
Reparto de producción entre órdenes.

Cada caso es un acuerdo explícito sobre qué debe quedar en la base. Los números
salen de órdenes reales de la planta; si alguno cambia, es que cambió la regla.

Recordatorio de la regla del golpe:
    un golpe se registra ENTERO en la orden que tenía hueco cuando cayó.
"""

import pytest

from domain.ordenes import Asignacion, Capacidad, repartir, totales


def cap(id_reg, orden, falta):
    return Capacidad(id_registro=id_reg, orden=orden, falta=falta)


class TestCasoNormal:
    """Todo el incremento cabe en la orden activa."""

    def test_carroceria_sin_multiplicador(self):
        a = repartir(incremento=3, multiplicador=1, capacidades=[cap(1, "4051137", 500)])
        assert a == [Asignacion(1, "4051137", incremento=3, piezas=3, cierra=False)]

    def test_estampado_con_multiplicador(self):
        """VA4053913-1, orden 4051137: faltan 1286 piezas, mult 2."""
        a = repartir(incremento=10, multiplicador=2, capacidades=[cap(1, "4051137", 1286)])
        assert a == [Asignacion(1, "4051137", incremento=10, piezas=20, cierra=False)]

    def test_incremento_cero_no_produce_nada(self):
        assert repartir(0, 2, [cap(1, "A", 100)]) == []


class TestFronteraExacta:
    """El incremento llena la orden justo, sin sobrante."""

    def test_cierra_sin_sobrante(self):
        a = repartir(incremento=15, multiplicador=4, capacidades=[cap(1, "A", 60)])
        assert a == [Asignacion(1, "A", incremento=15, piezas=60, cierra=True)]

    def test_cierra_y_el_resto_pasa_a_la_siguiente(self):
        """BDTS53411-BK, orden 4051130: faltan 60 de 900, mult 4, llegan 20 golpes."""
        a = repartir(incremento=20, multiplicador=4,
                     capacidades=[cap(1, "4051130", 60), cap(2, "4051131", 1000)])
        assert a == [
            Asignacion(1, "4051130", incremento=15, piezas=60, cierra=True),
            Asignacion(2, "4051131", incremento=5, piezas=20, cierra=False),
        ]


class TestGolpePartido:
    """
    El golpe no se puede partir: va entero a la orden que tenía hueco, y las
    piezas sobrantes pasan a la siguiente SIN generar fila de historial.
    """

    def test_un_golpe_dos_piezas_solo_cabe_una(self):
        """El ejemplo que planteaste: 1 golpe, 2 piezas, a la orden le falta 1."""
        a = repartir(incremento=1, multiplicador=2,
                     capacidades=[cap(1, "A", 1), cap(2, "B", 100)])
        assert a == [
            Asignacion(1, "A", incremento=1, piezas=1, cierra=True),
            Asignacion(2, "B", incremento=0, piezas=1, cierra=False),   # incremento=0: sin fila
        ]

    def test_dos_golpes_el_segundo_va_entero_a_la_siguiente(self):
        """Tu segundo ejemplo: 2 golpes, 4 piezas, a la orden le faltan 2."""
        a = repartir(incremento=2, multiplicador=2,
                     capacidades=[cap(1, "A", 2), cap(2, "B", 100)])
        assert a == [
            Asignacion(1, "A", incremento=1, piezas=2, cierra=True),
            Asignacion(2, "B", incremento=1, piezas=2, cierra=False),
        ]

    def test_orden_imposible_de_llenar_exacto(self):
        """
        DGH970273A, orden 4051219: planeada 1 pieza con multiplicador 2.
        Ningún número entero de golpes la llena exacto; el golpe la cierra
        y la pieza sobrante se va a la siguiente.
        """
        a = repartir(incremento=1, multiplicador=2,
                     capacidades=[cap(1, "4051219", 1), cap(2, "4051220", 50)])
        assert a[0] == Asignacion(1, "4051219", incremento=1, piezas=1, cierra=True)
        assert a[1] == Asignacion(2, "4051220", incremento=0, piezas=1, cierra=False)


class TestVariasOrdenes:
    """Un incremento grande puede cruzar más de dos órdenes."""

    def test_cadena_de_tres_ordenes(self):
        a = repartir(incremento=10, multiplicador=1,
                     capacidades=[cap(1, "A", 2), cap(2, "B", 3), cap(3, "C", 50)])
        assert a == [
            Asignacion(1, "A", incremento=2, piezas=2, cierra=True),
            Asignacion(2, "B", incremento=3, piezas=3, cierra=True),
            Asignacion(3, "C", incremento=5, piezas=5, cierra=False),
        ]

    def test_salta_las_ordenes_ya_llenas(self):
        a = repartir(incremento=5, multiplicador=1,
                     capacidades=[cap(1, "A", 0), cap(2, "B", 100)])
        assert a == [Asignacion(2, "B", incremento=5, piezas=5, cierra=False)]


class TestNoPlaneado:
    """Sin órdenes con hueco, la producción va al registro status 24."""

    def test_sin_ninguna_orden(self):
        a = repartir(incremento=7, multiplicador=1, capacidades=[])
        assert a == [Asignacion(None, None, incremento=7, piezas=7, cierra=False)]
        assert a[0].es_no_planeado

    def test_se_acaban_las_ordenes_a_media_reparticion(self):
        """Confirmaste que el sobrante crea el registro 24 en ese momento."""
        a = repartir(incremento=10, multiplicador=1, capacidades=[cap(1, "A", 3)])
        assert a == [
            Asignacion(1, "A", incremento=3, piezas=3, cierra=True),
            Asignacion(None, None, incremento=7, piezas=7, cierra=False),
        ]

    def test_piezas_sueltas_sin_orden_siguiente(self):
        """El golpe se registró en A; su pieza sobrante va al 24 sin fila propia."""
        a = repartir(incremento=1, multiplicador=2, capacidades=[cap(1, "A", 1)])
        assert a == [
            Asignacion(1, "A", incremento=1, piezas=1, cierra=True),
            Asignacion(None, None, incremento=0, piezas=1, cierra=False),
        ]

    def test_estampado_sin_ordenes(self):
        a = repartir(incremento=5, multiplicador=4, capacidades=[])
        assert a == [Asignacion(None, None, incremento=5, piezas=20, cierra=False)]


class TestNadaSePierde:
    """Invariante: la suma repartida siempre iguala lo que entró."""

    @pytest.mark.parametrize("incremento,mult,faltas", [
        (20, 4, [60, 1000]),
        (1, 2, [1, 100]),
        (10, 1, [2, 3, 50]),
        (7, 1, []),
        (100, 20, [30, 45, 7]),
        (3, 2, [1]),
        (1, 20, [1]),
    ])
    def test_incremento_y_piezas_cuadran(self, incremento, mult, faltas):
        caps = [cap(i + 1, f"O{i}", f) for i, f in enumerate(faltas)]
        a = repartir(incremento, mult, caps)
        g, p = totales(a)
        assert g == incremento, "se perdió o se inventó avance del contador"
        assert p == incremento * mult, "se perdieron o inventaron piezas"

    @pytest.mark.parametrize("incremento,mult,faltas", [
        (20, 4, [60, 1000]),
        (1, 2, [1, 100]),
        (10, 1, [2, 3, 50]),
        (100, 20, [30, 45, 7]),
    ])
    def test_ninguna_orden_se_sobrepasa(self, incremento, mult, faltas):
        caps = [cap(i + 1, f"O{i}", f) for i, f in enumerate(faltas)]
        for asig in repartir(incremento, mult, caps):
            if asig.es_no_planeado:
                continue
            assert asig.piezas <= faltas[asig.id_registro - 1], \
                "una orden recibió más piezas de las que le faltaban"


class TestDosLadosLaMismaOrden:
    """
    LH y RH produciendo la misma parte llenan la MISMA orden. El reparto se
    calcula con el hueco que la BD reporta en ese momento, así que cada lado
    ve la capacidad ya descontada por el otro.
    """

    def test_el_segundo_lado_ve_menos_hueco(self):
        lh = repartir(incremento=30, multiplicador=1, capacidades=[cap(1, "A", 100)])
        assert lh == [Asignacion(1, "A", incremento=30, piezas=30, cierra=False)]

        # El UPDATE atómico dejó la orden con 70 de hueco cuando entra RH
        rh = repartir(incremento=80, multiplicador=1,
                      capacidades=[cap(1, "A", 70), cap(2, "B", 500)])
        assert rh == [
            Asignacion(1, "A", incremento=70, piezas=70, cierra=True),
            Asignacion(2, "B", incremento=10, piezas=10, cierra=False),
        ]
