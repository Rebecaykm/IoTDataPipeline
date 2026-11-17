"""
Una estación parada se cierra UNA vez, no cada segundo.

Antes, cada lectura completa sin número de parte lanzaba "cierra todo lo que
esta estación tenga en progreso" más un commit, aunque ya no hubiera nada que
cerrar. Con ~100 estaciones paradas un domingo eran ~8.6 millones de escrituras
al día sobre production_records, y cada una competía por bloqueos con las
estaciones que sí producían.

Ahora se cierra al pararse; mientras siga parada no se toca la base, ni
siquiera para pedir conexión. Si el cierre falla se reintenta, y al reiniciar
el servicio se hace una vez más.
"""

import logging
from datetime import date, datetime

import pytest

import Prensas
from persistence import ordenes as ord_sql
from tests.base_falsa import BaseFalsa, ConexionFalsa, Registro

LOG = logging.getLogger("test")

IP = "10.1.2.5"
ESTACION = "MM05"
PARTE = "BDTS5318XB"
MOMENTO = datetime(2026, 11, 15, 3, 52, 0)


@pytest.fixture(autouse=True)
def hoy_fijo(monkeypatch):
    class _FechaFija(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 15)
    monkeypatch.setattr(Prensas, "date", _FechaFija)


@pytest.fixture
def base():
    return BaseFalsa([Registro(101, ESTACION, PARTE, orden="4051130", planned=900,
                               produced=240, status=ord_sql.EN_PROGRESO,
                               planned_date="2026-11-15")],
                     partes=[(ESTACION, PARTE)],
                     sync_from={ESTACION: date(2000, 1, 1)})


@pytest.fixture
def conexiones(base, monkeypatch):
    """Cada petición de conexión queda anotada: es lo que se quiere contar."""
    pedidas = []

    def conectar():
        pedidas.append(ConexionFalsa(base))
        return pedidas[-1]

    monkeypatch.setattr(Prensas, "create_connection", conectar)
    return pedidas


@pytest.fixture
def procesador(conexiones, tmp_path, monkeypatch):
    monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
    monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
    monkeypatch.setattr(Prensas, "registrar_error_validacion", lambda *a, **k: None)
    Prensas.REGISTRO.olvidar_estacion(ESTACION)
    return Prensas.IPDataProcessor(IP)


def parada(procesador, plc_ok=True):
    return procesador._process_estacion({
        "estacion": ESTACION, "area": "Carrocería Fase 1", "plc_ok": plc_ok,
        "ts": MOMENTO, "datos": [],
    })


def produciendo(procesador, contador):
    return procesador._process_estacion({
        "estacion": ESTACION, "area": "Carrocería Fase 1", "plc_ok": True,
        "ts": MOMENTO,
        "datos": [{"parte": PARTE, "original": PARTE, "contador": contador,
                   "tiempo": 17.8, "lado": "LH", "validado": True, "troquel_id": None}],
    })


def cierres(base):
    """Cuántas veces se lanzó el cierre de toda la estación."""
    return sum(1 for c in base.consultas
               if "WHERE wc.name = ? AND pr.status_id" in " ".join(c.split()))


class TestSeCierraUnaSolaVez:
    def test_tres_lecturas_paradas_un_solo_cierre(self, procesador, base, conexiones):
        for _ in range(3):
            parada(procesador)

        assert cierres(base) == 1
        assert base[101].status == ord_sql.DETENIDO
        assert len(conexiones) == 1, "parada y ya cerrada: ni siquiera pide conexión"

    def test_si_vuelve_a_producir_y_se_para_se_cierra_otra_vez(self, procesador, base):
        parada(procesador)
        produciendo(procesador, 241)
        parada(procesador)
        parada(procesador)

        assert cierres(base) == 2
        assert base[101].status == ord_sql.DETENIDO

    def test_si_el_cierre_no_se_confirma_se_reintenta(self, procesador, base, monkeypatch):
        """Solo se da por cerrada si el commit pasó."""
        class ConexionQueFallaUnaVez(ConexionFalsa):
            fallas = 1

            def commit(self):
                if ConexionQueFallaUnaVez.fallas:
                    ConexionQueFallaUnaVez.fallas -= 1
                    self.rollback()
                    raise RuntimeError("se cayó la red")
                super().commit()

        conexion = ConexionQueFallaUnaVez(base)
        monkeypatch.setattr(Prensas, "create_connection", lambda: conexion)

        parada(procesador)
        assert base[101].status == ord_sql.EN_PROGRESO, "el primer cierre se revirtió"

        parada(procesador)
        parada(procesador)

        assert cierres(base) == 2, "falló uno, se reintentó una vez, y ya"
        assert base[101].status == ord_sql.DETENIDO


class TestLoQueNoCambia:
    def test_una_lectura_incompleta_no_cierra_ni_pide_conexion(
            self, procesador, base, conexiones):
        """Sin lectura completa no se sabe si la estación se paró."""
        parada(procesador, plc_ok=False)

        assert cierres(base) == 0
        assert conexiones == []
        assert base[101].status == ord_sql.EN_PROGRESO

    def test_al_reiniciar_el_servicio_se_cierra_una_vez_mas(
            self, procesador, base, tmp_path):
        """
        Lo que recuerda que ya se cerró vive en memoria: tras un reinicio se
        cierra otra vez, por si algo quedó abierto entretanto.
        """
        parada(procesador)
        reiniciado = Prensas.IPDataProcessor(IP)
        parada(reiniciado)

        assert cierres(base) == 2
