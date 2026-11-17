"""
Traducir el MDI de Estampado: con caducidad, y fuera del hilo de los PLC.

Dos problemas que se agravaban entre sí:

  - La traducción se guardaba para siempre, incluso los rechazos: un MDI dado
    de alta en el catálogo seguía rechazado hasta reiniciar el servicio.
  - La consulta corría en el hilo que lee TODOS los PLC. Con la base lenta o
    caída, cada consulta congelaba la lectura de la planta entera hasta 10 s.

Darle caducidad a la traducción provoca más consultas, así que las dos cosas
se corrigen juntas.
"""

import asyncio
import logging
import threading

import pyodbc
import pytest

import Prensas
from observabilidad.lecturas import RegistroLecturas
from tests.test_lecturas_en_vivo import CFG, PLCFalso

LOG = logging.getLogger("test")
ESTACION = "2500T  TR"
MDI = "BDTS28BFC"


class Reloj:
    def __init__(self):
        self.ahora = 1000.0

    def monotonic(self):
        return self.ahora


class ConexionMinima:
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def catalogo(monkeypatch):
    """
    Devuelve (reloj, respuestas, consultas). `respuestas` es lo que contestará
    el catálogo, en orden; la última se repite.
    """
    reloj = Reloj()
    respuestas = [(["BDTS28BFC-P"], None)]
    consultas = []

    def resolver(cursor, mdi, estacion, log):
        consultas.append(mdi)
        return respuestas[min(len(consultas), len(respuestas)) - 1]

    monkeypatch.setattr(Prensas, "system_time", reloj)
    monkeypatch.setattr(Prensas, "_mdi_resueltos", {})
    monkeypatch.setattr(Prensas, "create_connection", ConexionMinima)
    monkeypatch.setattr(Prensas.catalogo, "resolver_mdi", resolver)
    return reloj, respuestas, consultas


def traducir():
    return Prensas.validar_numeros_parte_estampado(MDI, ESTACION, LOG)


class TestCaducidad:
    def test_un_acierto_se_reutiliza_diez_minutos(self, catalogo):
        reloj, _, consultas = catalogo
        traducir()
        reloj.ahora += Prensas.VIGENCIA_MDI_RESUELTO - 1
        traducir()
        assert len(consultas) == 1

        reloj.ahora += 2
        traducir()
        assert len(consultas) == 2

    def test_un_mdi_dado_de_alta_entra_solo_sin_reiniciar(self, catalogo):
        """Antes el rechazo se quedaba hasta reiniciar el servicio."""
        reloj, respuestas, _ = catalogo
        respuestas[:] = [([], "MDI_NO_EXISTE"), (["BDTS28BFC-P"], None)]

        assert traducir() == ([], "MDI_NO_EXISTE")
        reloj.ahora += Prensas.VIGENCIA_MDI_FALLIDO - 1
        assert traducir() == ([], "MDI_NO_EXISTE"), "todavía no caduca"

        reloj.ahora += 2
        assert traducir() == (["BDTS28BFC-P"], None)

    def test_con_la_base_caida_no_se_reintenta_cada_segundo(self, catalogo, monkeypatch):
        reloj, _, _ = catalogo
        intentos = []
        monkeypatch.setattr(Prensas, "create_connection", lambda: intentos.append(1))

        assert traducir() == ([], "SIN_CONEXION_BD")
        traducir()
        assert len(intentos) == 1

        reloj.ahora += Prensas.VIGENCIA_MDI_FALLIDO + 1
        traducir()
        assert len(intentos) == 2

    def test_un_error_del_driver_descarta_la_conexion(self, catalogo, monkeypatch):
        descartes = []
        monkeypatch.setattr(Prensas, "descartar_conexion", lambda: descartes.append(1))

        def roto(*_a):
            raise pyodbc.Error("08S01", "Communication link failure")
        monkeypatch.setattr(Prensas.catalogo, "resolver_mdi", roto)

        assert traducir() == ([], "DB_ERROR")
        assert descartes == [1]


class TestFueraDelHiloDeLosPLC:
    def leer(self, area, monkeypatch):
        """Un ciclo de lectura; devuelve en qué hilo se tradujo."""
        monkeypatch.setattr(Prensas, "LECTURAS", RegistroLecturas())
        monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
        plc = PLCFalso()
        plc.poner("LH", MDI, 5)
        plc.poner("RH", MDI, 5)

        colector = Prensas.IPDataCollector("10.1.1.1", [ESTACION], area)
        colector._merge_blocks = lambda blocks, max_gap=15: [
            {"head": a, "len": n, "sub_blocks": [{"orig": (a, n), "offset": 0}]}
            for a, n in blocks
        ]
        hilos = []

        def resolver(raw, ctx):
            hilos.append(threading.current_thread().name)
            return ["BDTS28BFC-P"], None
        monkeypatch.setattr(colector._pipeline, "resolver_partes", resolver)

        info = {"estaciones": [ESTACION], "station_configs": {ESTACION: CFG},
                "all_addresses": {(v["address"], v["long"]) for v in CFG.values()}}
        asyncio.run(colector.collect_and_enqueue(plc, info))
        return hilos

    def test_estampado_traduce_en_el_hilo_de_catalogo(self, monkeypatch):
        hilos = self.leer("Estampado", monkeypatch)
        assert hilos and all(h.startswith("catalogo") for h in hilos), hilos

    def test_las_demas_areas_no_pagan_el_salto_de_hilo(self, monkeypatch):
        """Expandir por '/' es puro: no hay base que esperar."""
        hilos = self.leer("Chasis", monkeypatch)
        assert hilos == [threading.current_thread().name] * 2
