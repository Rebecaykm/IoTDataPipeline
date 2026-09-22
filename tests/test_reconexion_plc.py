"""
Qué pasa cuando el PLC se cae y vuelve a media corrida.

El caso real: el 21 de septiembre de 2026, el PLC 10.1.2.1 de MK05 se reconectó
cuatro veces en ochenta minutos. Ochenta milisegundos después de una de esas
reconexiones el recolector anunció "corrida nueva, el contador trae 8" y
registró esas 8 piezas... que ya estaban registradas desde hacía media hora.

El circuito era este: la parte desaparece de la lectura → se cierra su registro
y se BORRA su línea base → la parte vuelve → sin línea base se pregunta a la
base si hay algo En progreso → pero acabamos de dejarlo en Detenido, así que la
respuesta es no → "corrida nueva" → se cuenta el contador entero otra vez.

Lo que se fija aquí: la línea base sobrevive a la ausencia. Solo se olvida
cuando la parte lleva tanto sin aparecer que su contador ya no dice nada.
"""

import logging
from datetime import datetime, timedelta

import pytest

import Prensas
from persistence import ordenes as ord_sql
from tests.base_falsa import BaseFalsa, ConexionFalsa, Registro

LOG = logging.getLogger("test")

IP = "10.1.2.1"
ESTACION = "MK05"
PARTE = "DGH97065ZA"
OTRA = "DGH97066ZA"
AREA = "Chasis"
MOMENTO = datetime(2026, 9, 21, 12, 11, 0)
CORTE = datetime(2026, 7, 3).date()


@pytest.fixture(autouse=True)
def corte_fijo(monkeypatch):
    monkeypatch.setattr(Prensas, "ORDENES_DESDE", CORTE)


@pytest.fixture
def base():
    return BaseFalsa([], partes=[(ESTACION, PARTE), (ESTACION, OTRA)])


@pytest.fixture
def procesador(base, tmp_path, monkeypatch):
    monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
    monkeypatch.setattr(Prensas, "create_connection", lambda: ConexionFalsa(base))
    monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
    monkeypatch.setattr(Prensas, "registrar_error_validacion", lambda *a, **k: None)
    Prensas.REGISTRO.olvidar_estacion(ESTACION)
    return Prensas.IPDataProcessor(IP)


def lectura(contador, parte=PARTE):
    return {"parte": parte, "original": parte, "contador": contador,
            "tiempo": 1.5, "lado": "LH", "validado": True, "troquel_id": None}


def procesar(procesador, datos, ts=MOMENTO, plc_ok=True):
    return procesador._process_estacion({
        "estacion": ESTACION, "datos": datos,
        "area": AREA, "plc_ok": plc_ok, "ts": ts,
    })


def orden(id, num, planned, produced=0, status=ord_sql.PENDIENTE):
    return Registro(id, ESTACION, PARTE, orden=num, planned=planned,
                    produced=produced, status=status,
                    planned_date="2026-09-21", created_at="2026-09-21 00:00:00")


def clave(parte=PARTE):
    return f"{ESTACION}_{parte}_LH"


class TestLaParteQueDesapareceYVuelve:
    """El caso de MK05, tal cual."""

    def test_no_vuelve_a_contar_el_contador_entero(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])          # 8 piezas, registradas
        procesar(procesador, [])                    # el PLC se cae: nada que leer
        procesar(procesador, [lectura(8)])          # vuelve, mismo contador

        assert base[101].produced == 8, "8 piezas, no 16"

    def test_al_volver_solo_cuenta_lo_nuevo(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [])
        procesar(procesador, [lectura(11)])         # siguió produciendo sin nosotros

        assert base[101].produced == 11, "las 3 del hueco entran, las 8 no se repiten"

    def test_la_linea_base_sobrevive_a_la_ausencia(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [])

        reg = procesador.active_records.get(clave())
        assert reg is not None, "el registro no se borra"
        assert reg['contador_registro'] == 8

    def test_la_orden_si_queda_detenida(self, procesador, base):
        """Conservar la línea base no significa dejar la orden abierta."""
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [])

        assert base[101].status == ord_sql.DETENIDO
        assert procesador.active_records[clave()]['id_en_curso'] is None

    def test_al_volver_retoma_la_orden_detenida(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [])
        procesar(procesador, [lectura(11)])

        assert base[101].produced == 11
        assert base[101].status == ord_sql.EN_PROGRESO


class TestCuandoDesapareceSoloUnaParte:
    """La estación sigue reportando, pero una de sus partes ya no aparece."""

    def test_la_que_se_va_conserva_su_linea_base(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [lectura(3, parte=OTRA)])   # cambió de parte
        procesar(procesador, [lectura(9)])               # volvió la primera

        reg = procesador.active_records[clave()]
        assert reg['contador_registro'] == 9
        assert base[101].produced == 9, "no se recontaron las 8"


class TestElResetSigueFuncionando:
    """
    Conservar la línea base no puede tapar un arranque de verdad: si el
    contador volvió a empezar, lo que marca es producción nueva y va completa.
    """

    def test_si_el_contador_reinicio_se_cuenta_completo(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(50)])
        procesar(procesador, [])
        procesar(procesador, [lectura(4)])      # corrida nueva desde cero

        assert base[101].produced == 54, "50 de antes + 4 de la corrida nueva"


class TestCuandoYaNoTieneSentidoRecordar:
    """
    Una parte que lleva medio día sin aparecer ya no dice nada útil: su
    contador puede haberse reiniciado veinte veces sin que nadie lo viera.
    """

    def test_se_olvida_despues_de_la_ventana(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(8)])

        # Trece horas sin aparecer. La cifra es absoluta a propósito: medirla
        # contra la propia constante haría que la prueba siguiera pasando
        # aunque alguien estirara la ventana a un año.
        procesador.active_records[clave()]['visto_en'] -= 13 * 3600

        procesar(procesador, [])

        assert clave() not in procesador.active_records

    def test_la_ventana_dura_un_turno_no_una_eternidad(self):
        """
        Un contador de hace días no dice nada. Si la ventana creciera sin
        límite, una parte que vuelve la semana entrante heredaría una línea
        base inventada y se perdería toda su primera corrida.
        """
        assert 3600 <= Prensas.VENTANA_OLVIDO_LINEA_BASE <= 24 * 3600

    def test_dentro_de_la_ventana_se_conserva(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(8)])

        procesador.active_records[clave()]['visto_en'] -= 3600   # una hora

        procesar(procesador, [])

        assert clave() in procesador.active_records

    def test_verla_de_nuevo_reinicia_la_cuenta_de_ausencia(self, procesador, base):
        """
        Una parte que lleva todo el turno produciendo no está ausente. Sin
        refrescar la marca en cada lectura, bastaría un hueco del PLC al final
        del turno para borrarle la línea base.
        """
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(8)])

        procesador.active_records[clave()]['visto_en'] -= 13 * 3600
        procesar(procesador, [lectura(9)])    # sigue ahí: la marca se refresca
        procesar(procesador, [])              # ahora sí desaparece

        assert clave() in procesador.active_records

    def test_una_parte_recien_creada_no_se_olvida_de_inmediato(
            self, procesador, base):
        """
        Aparece y desaparece en dos ciclos seguidos. Sin marca de tiempo propia
        se daría por ausente desde 1970 y perdería la línea base recién puesta.
        """
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [])

        assert clave() in procesador.active_records


class TestLecturaParcialSigueIntacta:
    """
    Con el PLC a medias no se concluye nada: ni se cierran órdenes ni se
    olvidan líneas base. Esto ya era así y tiene que seguir siéndolo.
    """

    def test_no_cierra_nada_con_lectura_parcial(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [], plc_ok=False)

        assert base[101].status == ord_sql.EN_PROGRESO
        assert procesador.active_records[clave()]['contador_registro'] == 8
