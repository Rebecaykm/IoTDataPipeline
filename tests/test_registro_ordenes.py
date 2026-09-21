"""
El camino completo: del incremento del contador a lo que queda en la base.

Estas pruebas corren contra una base en memoria (tests/base_falsa.py) y afirman
sobre el ESTADO FINAL de los registros, no sobre el SQL. Si una de ellas se
pone en rojo, es que en la planta se está registrando mal la producción.
"""

import logging

import pytest

from persistence import ordenes as ord_sql
from persistence.ordenes import registrar_incremento
from tests.base_falsa import BaseFalsa, Registro

LOG = logging.getLogger("test")

HOY = "2026-08-19"
AHORA = "2026-08-19 07:30:00"
DESDE = "2026-08-12"          # ORDENES_DIAS_ATRAS = 7
ESTACION = "MK05"
PARTE = "BDTS53411-BK"


def registrar(base, incremento, mult, estacion=ESTACION, parte=PARTE, desde=DESDE):
    return registrar_incremento(
        base, estacion, parte, incremento, mult,
        desde_fecha=desde, fecha_plan=HOY, turno=1, fecha_fmt=AHORA,
        anotar_history=base.anotar_history, log=LOG,
    )


def orden(id, num, planned, produced=0, status=ord_sql.PENDIENTE,
          fecha=HOY, shift=1, creado="2026-08-19 00:00:00"):
    return Registro(id, ESTACION, PARTE, orden=num, planned=planned,
                    produced=produced, status=status, planned_date=fecha,
                    shift=shift, created_at=creado)


class TestCorridaNormal:
    def test_suma_a_la_orden_y_deja_una_sola_fila_de_historial(self):
        base = BaseFalsa([orden(101, "4051130", planned=900)])
        r = registrar(base, incremento=10, mult=2)

        assert base[101].produced == 20
        assert base[101].status == ord_sql.EN_PROGRESO
        assert base.historias == [(101, "4051130", 10)]
        assert (r.piezas, r.incremento, r.cerradas) == (20, 10, [])
        assert r.en_curso == 101

    def test_el_historial_lleva_el_numero_de_orden(self):
        """Es lo que permite reconciliar histories contra el plan de Infor."""
        base = BaseFalsa([orden(101, "4051130", planned=900)])
        registrar(base, incremento=3, mult=1)
        assert base.historias[0][1] == "4051130"

    def test_incremento_cero_no_toca_la_base(self):
        base = BaseFalsa([orden(101, "4051130", planned=900)])
        r = registrar(base, incremento=0, mult=2)
        assert base.consultas == []
        assert (r.piezas, r.incremento) == (0, 0)

    def test_dos_ciclos_seguidos_acumulan(self):
        """La base es la dueña del acumulado: se SUMA, nunca se asigna."""
        base = BaseFalsa([orden(101, "4051130", planned=900)])
        registrar(base, incremento=10, mult=2)
        registrar(base, incremento=5, mult=2)
        assert base[101].produced == 30

    def test_marca_el_inicio_de_produccion_una_sola_vez(self):
        base = BaseFalsa([orden(101, "4051130", planned=900)])
        registrar(base, incremento=1, mult=1)
        primero = base[101].production_start
        registrar_incremento(base, ESTACION, PARTE, 1, 1, DESDE, HOY, 1,
                             "2026-08-19 09:00:00", base.anotar_history, LOG)
        assert base[101].production_start == primero


class TestLlenarYPasarALaSiguiente:
    """El caso que motivó todo el rediseño."""

    def test_cierra_la_llena_y_sigue_con_la_siguiente(self):
        """
        Orden 4051130: 840 de 900, faltan 60. Llegan 20 golpes con mult 4 = 80
        piezas. 15 golpes la llenan y la cierran; los 5 restantes abren 4051131.
        """
        base = BaseFalsa([
            orden(101, "4051130", planned=900, produced=840),
            orden(102, "4051131", planned=1000, fecha="2026-08-19",
                  creado="2026-08-19 01:00:00"),
        ])
        r = registrar(base, incremento=20, mult=4)

        assert base[101].produced == 900
        assert base[101].status == ord_sql.COMPLETADO
        assert base[102].produced == 20
        assert base[102].status == ord_sql.EN_PROGRESO
        assert base.historias == [(101, "4051130", 15), (102, "4051131", 5)]
        assert r.cerradas == ["4051130"]
        assert r.en_curso == 102

    def test_una_orden_completada_ya_no_recibe_nada(self):
        base = BaseFalsa([
            orden(101, "4051130", planned=900, produced=900,
                  status=ord_sql.COMPLETADO),
            orden(102, "4051131", planned=1000),
        ])
        registrar(base, incremento=5, mult=1)
        assert base[101].produced == 900
        assert base[102].produced == 5

    def test_cruza_tres_ordenes_de_un_solo_incremento(self):
        base = BaseFalsa([
            orden(101, "A", planned=2, creado="2026-08-19 01:00:00"),
            orden(102, "B", planned=3, creado="2026-08-19 02:00:00"),
            orden(103, "C", planned=50, creado="2026-08-19 03:00:00"),
        ])
        r = registrar(base, incremento=10, mult=1)

        assert (base[101].status, base[102].status) == (ord_sql.COMPLETADO,
                                                        ord_sql.COMPLETADO)
        assert base[103].produced == 5
        assert r.cerradas == ["A", "B"]


class TestGolpePartido:
    """Un golpe se registra entero donde cayó; sus piezas pueden irse a otra."""

    def test_la_pieza_sobrante_no_genera_fila_de_historial(self):
        base = BaseFalsa([
            orden(101, "A", planned=1, creado="2026-08-19 01:00:00"),
            orden(102, "B", planned=100, creado="2026-08-19 02:00:00"),
        ])
        r = registrar(base, incremento=1, mult=2)

        assert base[101].produced == 1 and base[101].status == ord_sql.COMPLETADO
        assert base[102].produced == 1
        assert base.historias == [(101, "A", 1)], "el golpe se cuenta UNA vez"
        assert (r.piezas, r.incremento) == (2, 1)

    def test_el_segundo_golpe_va_entero_a_la_siguiente(self):
        base = BaseFalsa([
            orden(101, "A", planned=2, creado="2026-08-19 01:00:00"),
            orden(102, "B", planned=100, creado="2026-08-19 02:00:00"),
        ])
        registrar(base, incremento=2, mult=2)
        assert base.historias == [(101, "A", 1), (102, "B", 1)]

    def test_orden_que_ningun_numero_entero_de_golpes_llena(self):
        """Planeada 1 pieza con multiplicador 2: se cierra igual, no se atora."""
        base = BaseFalsa([
            orden(101, "4051219", planned=1, creado="2026-08-19 01:00:00"),
            orden(102, "4051220", planned=50, creado="2026-08-19 02:00:00"),
        ])
        registrar(base, incremento=1, mult=2)
        assert base[101].status == ord_sql.COMPLETADO
        assert base[102].produced == 1


class TestNoPlaneado:
    def test_sin_ordenes_crea_el_acumulador(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        r = registrar(base, incremento=5, mult=4)

        acumuladores = base.acumuladores
        assert len(acumuladores) == 1
        assert acumuladores[0].produced == 20
        assert acumuladores[0].orden is None
        assert base.historias == [(acumuladores[0].id, None, 5)]
        assert r.no_planeado == 20

    def test_reutiliza_siempre_el_mismo_acumulador(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=5, mult=1)
        registrar(base, incremento=3, mult=1)

        assert len(base.acumuladores) == 1
        assert base.acumuladores[0].produced == 8

    def test_el_sobrante_de_la_ultima_orden_cae_al_acumulador(self):
        base = BaseFalsa([orden(101, "A", planned=3)])
        r = registrar(base, incremento=10, mult=1)

        assert base[101].status == ord_sql.COMPLETADO
        assert base.acumuladores[0].produced == 7
        assert r.no_planeado == 7
        assert base.historias == [(101, "A", 3), (base.acumuladores[0].id, None, 7)]

    def test_el_acumulador_no_se_confunde_con_una_orden(self):
        """No tiene plan que llenar, así que nunca se ofrece como capacidad."""
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=5, mult=1)
        acumulador = base.acumuladores[0]
        assert ord_sql.capacidades(base, ESTACION, PARTE, DESDE) == []
        assert acumulador.orden is None

    def test_parte_que_no_existe_reporta_error_sin_inventar_registros(self):
        base = BaseFalsa([], partes=[])
        r = registrar(base, incremento=5, mult=1, parte="FANTASMA")

        assert r.error == "PART_NUMBER_NO_EXISTE_BD"
        assert r.piezas == 0
        assert base.registros == {}


class TestPrioridad:
    def test_retoma_la_orden_en_progreso_antes_que_una_mas_antigua(self):
        """
        Abandonar a media corrida la orden que se estaba llenando dejaría
        producción huérfana. La antigüedad manda solo entre las pendientes.
        """
        base = BaseFalsa([
            orden(101, "VIEJA", planned=500, fecha="2026-08-13"),
            orden(102, "ENCURSO", planned=500, produced=100,
                  status=ord_sql.EN_PROGRESO, fecha="2026-08-18"),
        ])
        registrar(base, incremento=10, mult=1)
        assert base[102].produced == 110
        assert base[101].produced == 0

    def test_entre_pendientes_gana_la_mas_antigua(self):
        base = BaseFalsa([
            orden(101, "NUEVA", planned=500, fecha="2026-08-19"),
            orden(102, "ANTIGUA", planned=500, fecha="2026-08-14"),
        ])
        registrar(base, incremento=10, mult=1)
        assert base[102].produced == 10
        assert base[101].produced == 0

    def test_a_igual_fecha_desempata_el_turno(self):
        base = BaseFalsa([
            orden(101, "T2", planned=500, fecha="2026-08-14", shift=2),
            orden(102, "T1", planned=500, fecha="2026-08-14", shift=1),
        ])
        registrar(base, incremento=10, mult=1)
        assert base[102].produced == 10

    def test_retoma_una_orden_detenida(self):
        """Una corrida que se reanuda vuelve a llenar la orden que dejó a medias."""
        base = BaseFalsa([
            orden(101, "A", planned=500, produced=200, status=ord_sql.DETENIDO,
                  fecha="2026-08-14"),
        ])
        registrar(base, incremento=10, mult=1)
        assert base[101].produced == 210
        assert base[101].status == ord_sql.EN_PROGRESO

    def test_no_rescata_ordenes_mas_viejas_que_el_corte(self):
        base = BaseFalsa([orden(101, "MUY_VIEJA", planned=500, fecha="2026-07-01")])
        registrar(base, incremento=5, mult=1)
        assert base[101].produced == 0
        assert base.acumuladores[0].produced == 5

    def test_el_corte_no_aplica_a_la_orden_en_progreso(self):
        base = BaseFalsa([
            orden(101, "VIEJA_EN_CURSO", planned=500, produced=10,
                  status=ord_sql.EN_PROGRESO, fecha="2026-07-01"),
        ])
        registrar(base, incremento=5, mult=1)
        assert base[101].produced == 15
        assert base.acumuladores == []

    def test_ignora_lo_ya_enviado_a_infor(self):
        base = BaseFalsa([orden(101, "ENVIADA", planned=500)],
                         partes=[(ESTACION, PARTE)])
        base[101].synced = 1
        registrar(base, incremento=5, mult=1)
        assert base[101].produced == 0
        assert base.acumuladores[0].produced == 5


class TestDosLados:
    """LH y RH con la misma parte llenan la MISMA orden, uno tras otro."""

    def test_el_segundo_lado_ve_el_hueco_que_dejo_el_primero(self):
        base = BaseFalsa([
            orden(101, "A", planned=100, creado="2026-08-19 01:00:00"),
            orden(102, "B", planned=500, creado="2026-08-19 02:00:00"),
        ])
        registrar(base, incremento=30, mult=1)      # LH
        registrar(base, incremento=80, mult=1)      # RH

        assert base[101].produced == 100
        assert base[101].status == ord_sql.COMPLETADO
        assert base[102].produced == 10
        assert base.total_conteo() == 110

    def test_ninguna_orden_se_pasa_de_lo_planeado(self):
        base = BaseFalsa([orden(101, "A", planned=100)],
                         partes=[(ESTACION, PARTE)])
        registrar(base, incremento=90, mult=1)
        registrar(base, incremento=90, mult=1)

        assert base[101].produced == 100
        assert base.acumuladores[0].produced == 80


class TestFinDeCorrida:
    def test_detener_deja_la_orden_lista_para_retomarse(self):
        base = BaseFalsa([orden(101, "A", planned=500)])
        registrar(base, incremento=10, mult=1)
        ord_sql.detener(base, 101, AHORA)

        assert base[101].status == ord_sql.DETENIDO
        assert [c.id_registro for c in
                ord_sql.capacidades(base, ESTACION, PARTE, DESDE)] == [101]

    def test_detener_no_toca_una_orden_pendiente(self):
        """El plan de producción no se cierra desde el recolector: ya nos pasó."""
        base = BaseFalsa([orden(101, "A", planned=500)])
        ord_sql.detener(base, 101, AHORA)
        assert base[101].status == ord_sql.PENDIENTE

    def test_detener_no_reabre_una_completada(self):
        base = BaseFalsa([orden(101, "A", planned=10, produced=10,
                                status=ord_sql.COMPLETADO)])
        ord_sql.detener(base, 101, AHORA)
        assert base[101].status == ord_sql.COMPLETADO

    def test_la_estacion_que_deja_de_reportar_detiene_lo_suyo(self):
        base = BaseFalsa([
            orden(101, "A", planned=500, produced=10, status=ord_sql.EN_PROGRESO),
            orden(102, "B", planned=500),
        ])
        ord_sql.detener_estacion(base, ESTACION, AHORA)
        assert base[101].status == ord_sql.DETENIDO
        assert base[102].status == ord_sql.PENDIENTE, "el plan no se toca"


class TestNadaSePierde:
    """La invariante que importa: lo que salió del PLC está en la base."""

    ESCENARIOS = [
        ("una orden holgada", [(101, 900, 0)], 10, 2),
        ("orden a punto de llenarse", [(101, 900, 840), (102, 1000, 0)], 20, 4),
        ("golpe partido", [(101, 1, 0), (102, 100, 0)], 1, 2),
        ("tres ordenes", [(101, 2, 0), (102, 3, 0), (103, 50, 0)], 10, 1),
        ("se acaban las ordenes", [(101, 3, 0)], 10, 1),
        ("sin ordenes", [], 7, 1),
        ("estampado sin ordenes", [], 5, 4),
        ("incremento enorme", [(101, 30, 0), (102, 45, 0)], 100, 20),
    ]

    @pytest.mark.parametrize("nombre,filas,incremento,mult", ESCENARIOS,
                             ids=[e[0] for e in ESCENARIOS])
    def test_todo_lo_producido_queda_registrado(self, nombre, filas, incremento, mult):
        base = BaseFalsa(
            [orden(i, f"O{i}", planned=p, produced=q,
                   creado=f"2026-08-19 0{n}:00:00")
             for n, (i, p, q) in enumerate(filas)],
            partes=[(ESTACION, PARTE)],
        )
        antes = base.total_producido()
        r = registrar(base, incremento=incremento, mult=mult)

        assert base.total_producido() - antes == incremento * mult
        assert base.total_conteo() == incremento
        assert r.piezas == incremento * mult
        assert r.incremento == incremento

    @pytest.mark.parametrize("nombre,filas,incremento,mult", ESCENARIOS,
                             ids=[e[0] for e in ESCENARIOS])
    def test_ninguna_orden_supera_su_plan(self, nombre, filas, incremento, mult):
        base = BaseFalsa(
            [orden(i, f"O{i}", planned=p, produced=q,
                   creado=f"2026-08-19 0{n}:00:00")
             for n, (i, p, q) in enumerate(filas)],
            partes=[(ESTACION, PARTE)],
        )
        registrar(base, incremento=incremento, mult=mult)
        for r in base.registros.values():
            if r.orden is not None:
                assert r.produced <= r.planned, f"{r} se pasó del plan"

    @pytest.mark.parametrize("nombre,filas,incremento,mult", ESCENARIOS,
                             ids=[e[0] for e in ESCENARIOS])
    def test_toda_orden_llena_queda_completada(self, nombre, filas, incremento, mult):
        base = BaseFalsa(
            [orden(i, f"O{i}", planned=p, produced=q,
                   creado=f"2026-08-19 0{n}:00:00")
             for n, (i, p, q) in enumerate(filas)],
            partes=[(ESTACION, PARTE)],
        )
        registrar(base, incremento=incremento, mult=mult)
        for r in base.registros.values():
            if r.orden is not None and r.produced >= r.planned:
                assert r.status == ord_sql.COMPLETADO, f"{r} quedó llena sin cerrar"


class TestLineaBaseDeCorrida:
    """
    Qué se hace con el valor que el contador ya traía cuando aparece una parte.

    Es la diferencia entre recuperar una corrida y duplicarla.
    """

    def test_sin_corrida_previa_el_contador_es_produccion(self):
        base = BaseFalsa([orden(101, "A", planned=500)])
        assert ord_sql.hay_corrida_en_progreso(base, ESTACION, PARTE) is False

    def test_con_orden_en_progreso_el_contador_ya_estaba_contado(self):
        base = BaseFalsa([orden(101, "A", planned=500, produced=200,
                                status=ord_sql.EN_PROGRESO)])
        assert ord_sql.hay_corrida_en_progreso(base, ESTACION, PARTE) is True

    def test_una_orden_pendiente_no_cuenta_como_corrida(self):
        base = BaseFalsa([orden(101, "A", planned=500, status=ord_sql.PENDIENTE)])
        assert ord_sql.hay_corrida_en_progreso(base, ESTACION, PARTE) is False

    def test_una_orden_detenida_tampoco(self):
        """La corrida terminó: lo que el contador traiga es de la nueva."""
        base = BaseFalsa([orden(101, "A", planned=500, produced=200,
                                status=ord_sql.DETENIDO)])
        assert ord_sql.hay_corrida_en_progreso(base, ESTACION, PARTE) is False

    def test_no_confunde_partes_ni_estaciones(self):
        base = BaseFalsa([orden(101, "A", planned=500, produced=1,
                                status=ord_sql.EN_PROGRESO)])
        assert ord_sql.hay_corrida_en_progreso(base, ESTACION, "OTRA") is False
        assert ord_sql.hay_corrida_en_progreso(base, "OTRA_EST", PARTE) is False


class TestCicloDelAcumulador:
    """
    La producción sin orden vive En progreso mientras la corrida está viva, y
    solo queda como No planeado cuando termina. Así, mirando la base en
    cualquier momento, lo que está corriendo se ve corriendo.
    """

    def test_nace_en_progreso_no_en_no_planeado(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=5, mult=1)

        acumulador = base.acumuladores[0]
        assert acumulador.status == ord_sql.EN_PROGRESO
        assert base.no_planeados == [], "todavía no ha terminado la corrida"

    def test_al_terminar_la_corrida_pasa_a_no_planeado(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        r = registrar(base, incremento=5, mult=1)
        ord_sql.detener(base, r.en_curso, AHORA)

        assert base.acumuladores[0].status == ord_sql.NO_PLANEADO
        assert base.acumuladores[0].produced == 5

    def test_el_acumulador_queda_como_lo_abierto(self):
        """Para que el fin de corrida sepa a quién cerrar."""
        base = BaseFalsa([orden(101, "A", planned=3)], partes=[(ESTACION, PARTE)])
        r = registrar(base, incremento=10, mult=1)

        assert base[101].status == ord_sql.COMPLETADO
        assert r.en_curso == base.acumuladores[0].id, \
            "la orden se llenó, lo que queda abierto es el acumulador"

    def test_una_orden_a_medias_si_queda_detenida(self):
        """El mismo detener() manda 8 o 24 según el registro."""
        base = BaseFalsa([orden(101, "A", planned=500)])
        r = registrar(base, incremento=10, mult=1)
        ord_sql.detener(base, r.en_curso, AHORA)

        assert base[101].status == ord_sql.DETENIDO

    def test_la_estacion_que_deja_de_reportar_cierra_ambos(self):
        base = BaseFalsa([orden(101, "A", planned=3)], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=10, mult=1)
        ord_sql.detener_estacion(base, ESTACION, AHORA)

        assert base[101].status == ord_sql.COMPLETADO, "la llena no se toca"
        assert base.acumuladores[0].status == ord_sql.NO_PLANEADO

    def test_se_reabre_si_vuelve_a_producir_el_mismo_dia(self):
        """
        Segunda corrida sin orden en el mismo día: reutiliza el acumulador y lo
        devuelve a En progreso en vez de crear otro registro.
        """
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        r = registrar(base, incremento=5, mult=1)
        ord_sql.detener(base, r.en_curso, AHORA)
        registrar(base, incremento=3, mult=1)

        assert len(base.acumuladores) == 1
        assert base.acumuladores[0].produced == 8
        assert base.acumuladores[0].status == ord_sql.EN_PROGRESO

    def test_el_recorrido_completo_queda_en_el_rastro(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        r = registrar(base, incremento=5, mult=1)
        acumulador = base.acumuladores[0].id
        ord_sql.detener(base, r.en_curso, AHORA)

        assert [t for t in base.transiciones if t[0] == acumulador] == [
            (acumulador, ord_sql.EN_PROGRESO, ord_sql.NO_PLANEADO),
        ]


class TestUnSoloAcumuladorPorParte:
    """
    La producción sin orden de un número de parte se junta TODA en el mismo
    registro, sin importar el día. Para saber cuánto lleva una parte sin plan
    se lee un renglón, no se suman decenas.
    """

    OTRO_DIA = "2026-08-25"

    def registrar_en(self, base, incremento, dia):
        return registrar_incremento(
            base, ESTACION, PARTE, incremento, 1,
            desde_fecha=DESDE, fecha_plan=dia, turno=1,
            fecha_fmt=f"{dia} 07:30:00", anotar_history=base.anotar_history, log=LOG,
        )

    def test_dias_distintos_caen_en_el_mismo_registro(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        r = self.registrar_en(base, 5, HOY)
        ord_sql.detener(base, r.en_curso, AHORA)
        self.registrar_en(base, 3, self.OTRO_DIA)

        assert len(base.acumuladores) == 1, "un solo registro para la parte"
        assert base.acumuladores[0].produced == 8

    def test_la_fecha_queda_en_el_ultimo_dia_con_produccion(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        r = self.registrar_en(base, 5, HOY)
        ord_sql.detener(base, r.en_curso, AHORA)
        self.registrar_en(base, 3, self.OTRO_DIA)

        assert base.acumuladores[0].planned_date == self.OTRO_DIA

    def test_al_reabrir_el_inicio_marca_la_corrida_nueva(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        r = self.registrar_en(base, 5, HOY)
        primer_inicio = base.acumuladores[0].production_start
        ord_sql.detener(base, r.en_curso, AHORA)
        self.registrar_en(base, 3, self.OTRO_DIA)

        assert base.acumuladores[0].production_start != primer_inicio
        assert base.acumuladores[0].production_start.startswith(self.OTRO_DIA)

    def test_dentro_de_la_misma_corrida_el_inicio_no_se_mueve(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        self.registrar_en(base, 5, HOY)
        primer_inicio = base.acumuladores[0].production_start
        self.registrar_en(base, 3, HOY)          # sigue viva, no se detuvo

        assert base.acumuladores[0].production_start == primer_inicio
        assert base.acumuladores[0].produced == 8

    def test_cada_parte_tiene_el_suyo(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE), (ESTACION, "OTRA")])
        registrar(base, incremento=5, mult=1)
        registrar(base, incremento=7, mult=1, parte="OTRA")

        assert len(base.acumuladores) == 2
        assert sorted(a.produced for a in base.acumuladores) == [5, 7]

    def test_adopta_el_acumulador_mas_reciente_si_hay_varios(self):
        """
        Si por lo que sea hay más de uno después del corte, se toma el más
        nuevo (ORDER BY id DESC) y ese se sigue llenando; el otro no se toca.
        """
        viejo = Registro(50, ESTACION, PARTE, orden=None, planned=0, produced=100,
                         status=ord_sql.NO_PLANEADO, planned_date="2026-08-13")
        nuevo = Registro(80, ESTACION, PARTE, orden=None, planned=0, produced=40,
                         status=ord_sql.NO_PLANEADO, planned_date="2026-08-14")
        base = BaseFalsa([viejo, nuevo], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=6, mult=1)

        assert base[80].produced == 46
        assert base[50].produced == 100, "el otro queda congelado"


class TestElCorteTambienAcotaElAcumulador:
    """
    ORDENES_DESDE marca desde cuándo cuenta la historia, y vale igual para el
    acumulador. La base arrastra cientos de registros sin orden de años
    anteriores, de cuando se creaba uno por día: si el recolector adoptara uno
    de esos, mezclaría producción vieja con la de hoy en el mismo renglón.
    """

    def anterior_al_corte(self, produced=100, dia="2025-09-19"):
        return Registro(50, ESTACION, PARTE, orden=None, planned=0,
                        produced=produced, status=ord_sql.NO_PLANEADO,
                        planned_date=dia)

    def test_no_adopta_uno_anterior_al_corte(self):
        base = BaseFalsa([self.anterior_al_corte()], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=6, mult=1)

        assert base[50].produced == 100, "el viejo no se toca"
        nuevos = [a for a in base.acumuladores if a.id != 50]
        assert len(nuevos) == 1, "se abre uno del lado de acá del corte"
        assert nuevos[0].produced == 6

    def test_uno_del_dia_del_corte_si_se_adopta(self):
        """El corte incluye su propio día: es >=, no >."""
        base = BaseFalsa([self.anterior_al_corte(dia=DESDE)],
                         partes=[(ESTACION, PARTE)])
        registrar(base, incremento=6, mult=1)

        assert base[50].produced == 106
        assert len(base.acumuladores) == 1

    def test_el_nuevo_sigue_acumulando_en_los_dias_siguientes(self):
        """
        Se abre uno solo, no uno por día: el corte decide desde cuándo, no
        cada cuándo.
        """
        base = BaseFalsa([self.anterior_al_corte()], partes=[(ESTACION, PARTE)])
        r = registrar(base, incremento=6, mult=1)
        ord_sql.detener(base, r.en_curso, AHORA)
        registrar_incremento(
            base, ESTACION, PARTE, 4, 1, desde_fecha=DESDE,
            fecha_plan="2026-08-25", turno=1, fecha_fmt="2026-08-25 07:30:00",
            anotar_history=base.anotar_history, log=LOG)

        nuevos = [a for a in base.acumuladores if a.id != 50]
        assert len(nuevos) == 1
        assert nuevos[0].produced == 10


class TestCuandoUnaOrdenReclamaElAcumulado:
    """
    El acumulador no es solo un depósito: es una bolsa que otro proceso vacía
    cuando llega una orden nueva para esa parte. Mientras tanto el recolector
    sigue produciendo, así que los dos se cruzan.
    """

    def test_si_se_lo_llevan_a_media_escritura_se_abre_otro(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=10, mult=1)
        primero = base.acumuladores[0]

        # Llega una orden y reclama TODO lo acumulado: el registro deja de ser
        # un acumulador y pasa a ser el de esa orden.
        base.reclamar_para_orden(primero.id, "4105999", 10)
        assert primero.orden == "4105999" and primero.status == ord_sql.COMPLETADO

        registrar(base, incremento=15, mult=1)      # el recolector sigue

        assert len(base.acumuladores) == 1, "se abrió uno nuevo"
        assert base.acumuladores[0].produced == 15, "el delta entero, en el nuevo"
        assert base[primero.id].produced == 10, "lo reclamado no se toca"

    def test_no_se_pierde_ni_una_pieza_en_el_cruce(self):
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=10, mult=1)
        base.reclamar_para_orden(base.acumuladores[0].id, "4105999", 10)
        registrar(base, incremento=15, mult=1)

        assert base.total_producido() == 25, "10 reclamadas + 15 del delta nuevo"
        assert base.total_conteo() == 25

    def test_si_solo_se_llevan_parte_el_acumulador_sigue_siendo_el_mismo(self):
        """Sobró producción: el registro conserva el resto y sigue recibiendo."""
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=10, mult=1)
        acumulador = base.acumuladores[0]

        base.reclamar_para_orden(acumulador.id, "4105999", 4)   # se lleva 4 de 10
        assert acumulador.produced == 6
        assert acumulador.orden is None, "sigue siendo acumulador"

        registrar(base, incremento=13, mult=1)

        assert len(base.acumuladores) == 1, "no se abre otro: este sigue sirviendo"
        assert acumulador.produced == 19, "6 que quedaron + 13 del delta"

    def test_sin_catalogo_reporta_error_y_no_mueve_la_linea_base(self):
        """
        Se lo llevaron y tampoco se puede abrir otro. Mejor reportarlo que
        fingir que se registró: el contador no avanza y el siguiente ciclo
        reintenta con el delta completo.
        """
        base = BaseFalsa([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=10, mult=1)
        base.reclamar_para_orden(base.acumuladores[0].id, "4105999", 10)
        base.partes.clear()                       # la parte deja de existir

        r = registrar(base, incremento=15, mult=1)
        assert r.error is not None
        assert r.piezas == 0


class BaseQueReclamaAMediaEscritura(BaseFalsa):
    """
    Simula el cruce exacto: el proceso que asigna órdenes reclama el acumulador
    JUSTO entre que el recolector lo encuentra y que le escribe. Es la ventana
    que la búsqueda por sí sola no puede cubrir.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reclamados = []

    def _buscar_no_planeado(self, estacion, numero, desde):
        filas = super()._buscar_no_planeado(estacion, numero, desde)
        if filas and filas[0][0] not in self.reclamados:
            id_registro = filas[0][0]
            self.reclamados.append(id_registro)
            self.reclamar_para_orden(id_registro, f"ORD{id_registro}",
                                     self.registros[id_registro].produced)
        return filas


class TestElAcumuladorReclamadoAMediaEscritura:
    """
    El caso que el WHERE del UPDATE protege: se lo llevaron después de que lo
    encontramos. Sin reintento, esas piezas no quedan en ningún lado y el
    recolector las reporta como registradas.
    """

    def test_reintenta_con_otro_acumulador_y_no_pierde_nada(self):
        base = BaseQueReclamaAMediaEscritura([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=10, mult=1)     # crea uno, se lo reclaman, reintenta
        r = registrar(base, incremento=7, mult=1)

        assert r.error is None
        assert r.piezas == 7
        vivos = [a for a in base.acumuladores if a.orden is None]
        assert sum(a.produced for a in vivos) == 7, "las 7 piezas están en la base"

    def test_lo_deja_en_el_log_no_en_silencio(self):
        """
        Que el reintento funcione no basta: si nadie se entera de que dos
        procesos se estorban, el día que falle no habrá por dónde empezar.
        """
        class LogEspia:
            def __init__(self):
                self.avisos = []

            def warning(self, mensaje):
                self.avisos.append(mensaje)

            def info(self, mensaje):
                pass

            error = warning

        espia = LogEspia()
        base = BaseQueReclamaAMediaEscritura([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=10, mult=1)      # crea el acumulador
        registrar_incremento(base, ESTACION, PARTE, 7, 1, DESDE, HOY, 1, AHORA,
                             base.anotar_history, espia)   # aquí se lo reclaman

        assert any("reclamado" in a for a in espia.avisos), espia.avisos

    def test_si_tampoco_se_puede_abrir_otro_lo_reporta(self):
        """
        Se lo llevaron y la parte no está en el catálogo: no hay dónde. Se
        reporta el error y la línea base NO se mueve, para reintentar completo.
        """
        base = BaseQueReclamaAMediaEscritura([], partes=[(ESTACION, PARTE)])
        registrar(base, incremento=10, mult=1)
        base.partes.clear()
        base.reclamados.clear()

        r = registrar(base, incremento=7, mult=1)
        assert r.error is not None
        assert r.piezas == 0, "no se reporta como registrado lo que no entró"
