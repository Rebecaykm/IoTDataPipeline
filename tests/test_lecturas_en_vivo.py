"""
El tablero "Lecturas en vivo" tiene que decir lo que el PLC manda AHORA.

El caso real, de MM05 el 15 de noviembre de 2026: la estación llevaba desde la
madrugada del sábado sin número de parte (su archivo de estado ya no tenía
ninguna parte, solo el contador de cada lado en 240), pero el tablero seguía
mostrando BDTS5318XB / BDTS5418XB con contador 240, como si el PLC los siguiera
mandando. Lo único que lo delataba era la columna "Hace": 1.39 días.

La causa: la lectura solo se anotaba cuando el lado traía número. En cuanto
dejaba de traerlo, la fila se quedaba congelada en lo último que tuvo.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import pytest

import Prensas
from observabilidad import lecturas as lec
from observabilidad.lecturas import RegistroLecturas

IP = "10.1.2.5"
ESTACION = "MM05"
CFG = {
    "Contador LH": {"address": "D100", "long": 1},
    "Número de Parte LH": {"address": "D110", "long": 6},
    "Contador RH": {"address": "D200", "long": 1},
    "Número de Parte RH": {"address": "D210", "long": 6},
}


def palabras(texto, largo=6):
    """El número de parte como lo guarda el PLC: dos ASCII por word, bajo primero."""
    crudo = texto.encode("ascii").ljust(largo * 2, b"\x00")
    return [crudo[i] | (crudo[i + 1] << 8) for i in range(0, largo * 2, 2)]


class PLCFalso:
    """Memoria del PLC por dirección. `caido` simula que un bloque no se pudo leer."""

    def __init__(self):
        self.memoria = {}
        self.caido = set()

    def poner(self, lado, parte, contador):
        base = "D1" if lado == "LH" else "D2"
        self.memoria[f"{base}00"] = [contador]
        self.memoria[f"{base}10"] = palabras(parte)

    def batchread_wordunits(self, headdevice, readsize):
        if headdevice in self.caido:
            raise ValueError("bloque ilegible")
        return list(self.memoria[headdevice])[:readsize]


@pytest.fixture
def registro(monkeypatch):
    nuevo = RegistroLecturas()
    monkeypatch.setattr(Prensas, "LECTURAS", nuevo)
    monkeypatch.setattr(Prensas, "get_station_logger",
                        lambda _e: logging.getLogger("test"))
    return nuevo


@pytest.fixture
def leer(registro):
    """Un ciclo completo de collect_and_enqueue contra el PLC falso."""
    plc = PLCFalso()
    colector = Prensas.IPDataCollector(IP, [ESTACION], "Chasis")
    info = {
        "estaciones": [ESTACION],
        "station_configs": {ESTACION: CFG},
        "all_addresses": {(v["address"], v["long"]) for v in CFG.values()},
    }
    # Cada tag en su propia lectura, para poder tirar un bloque sin tirar los demás.
    colector._merge_blocks = lambda blocks, max_gap=15: [
        {"head": a, "len": n, "sub_blocks": [{"orig": (a, n), "offset": 0}]}
        for a, n in blocks
    ]

    def ciclo():
        asyncio.run(colector.collect_and_enqueue(plc, info))
        return {f["lado"]: f for f in registro.snapshot(estacion=ESTACION)}

    return plc, ciclo


class TestElLadoQueDejaDeMandarNumero:
    def test_no_se_queda_mostrando_el_ultimo_numero_que_tuvo(self, leer):
        """El caso de MM05: el PLC responde, el número viene en blanco."""
        plc, ciclo = leer
        plc.poner("LH", "BDTS5318XB", 240)
        plc.poner("RH", "BDTS5418XB", 240)
        filas = ciclo()
        assert filas["LH"]["numero_plc"] == "BDTS5318XB"
        assert filas["LH"]["estado"] == lec.EN_VIVO

        plc.poner("LH", "", 240)
        plc.poner("RH", "", 240)
        filas = ciclo()

        for lado in ("LH", "RH"):
            assert filas[lado]["numero_plc"] is None, "ya no lo manda"
            assert filas[lado]["numero_validado"] is None
            assert filas[lado]["contador"] == 240, "el contador sí se sigue leyendo"
            assert filas[lado]["estado"] == lec.SIN_NUMERO
            assert filas[lado]["hace_segundos"] < 5, "la lectura es de ahora"

    def test_un_lado_en_blanco_no_tapa_al_que_sigue_produciendo(self, leer):
        plc, ciclo = leer
        plc.poner("LH", "", 0)
        plc.poner("RH", "BDTS5418XB", 17)

        filas = ciclo()

        assert filas["LH"]["estado"] == lec.SIN_NUMERO
        assert filas["RH"]["estado"] == lec.EN_VIVO
        assert filas["RH"]["numero_plc"] == "BDTS5418XB"

    def test_cuando_vuelve_el_numero_la_fila_vuelve_a_estar_en_vivo(self, leer):
        plc, ciclo = leer
        plc.poner("LH", "", 240)
        plc.poner("RH", "", 240)
        ciclo()

        plc.poner("LH", "BDTS5318XB", 3)
        filas = ciclo()

        assert filas["LH"]["estado"] == lec.EN_VIVO
        assert filas["LH"]["contador"] == 3

    def test_el_blanco_no_viaja_al_procesador(self, leer, registro):
        """Es solo para el tablero: para el conteo, un lado en blanco no es nada."""
        plc, _ciclo = leer
        plc.poner("LH", "", 240)
        plc.poner("RH", "", 240)
        colector = Prensas.IPDataCollector(IP, [ESTACION], "Chasis")
        bloques = {(v["address"], v["long"]): plc.memoria[v["address"]]
                   for v in CFG.values()}

        assert colector._process_station_data(ESTACION, CFG, bloques, None) == []


class TestElContadorEsElQueMandaElPLC:
    def test_si_el_plc_manda_cero_se_ve_cero(self, leer):
        """
        Lo que se muestra es lo leído en ESE ciclo, no lo que el recolector
        recuerda: con o sin número, y aunque el valor sea 0.
        """
        plc, ciclo = leer
        plc.poner("LH", "BDTS5318XB", 240)
        plc.poner("RH", "BDTS5418XB", 240)
        ciclo()

        plc.poner("LH", "", 0)              # en blanco y contador reiniciado
        plc.poner("RH", "BDTS5418XB", 0)    # con número y contador reiniciado
        filas = ciclo()

        assert filas["LH"]["contador"] == 0
        assert filas["RH"]["contador"] == 0


class TestCuandoNoSePuedeLeer:
    def test_una_lectura_parcial_no_borra_ni_inventa_nada(self, leer):
        """
        Si falla el bloque de un lado no se sabe qué manda: su fila se queda
        como estaba y es la antigüedad la que avisa, no un "sin número" falso.
        """
        plc, ciclo = leer
        plc.poner("LH", "BDTS5318XB", 240)
        plc.poner("RH", "BDTS5418XB", 240)
        ciclo()

        plc.caido = {"D100", "D110"}
        filas = ciclo()

        assert filas["LH"]["numero_plc"] == "BDTS5318XB"
        assert filas["LH"]["estado"] == lec.EN_VIVO, "todavía dentro del umbral"
        assert filas["RH"]["estado"] == lec.EN_VIVO

    def test_pasado_el_umbral_la_fila_dice_que_es_un_dato_viejo(self):
        r = RegistroLecturas()
        r.anotar(ESTACION, "LH", ip=IP, numero_plc="BDTS5318XB", contador=240)
        r._lecturas[(ESTACION, "LH")]["ts"] = (
            datetime.now() - timedelta(seconds=lec.UMBRAL_SIN_RESPUESTA + 5))

        (fila,) = r.snapshot()

        assert fila["estado"] == lec.SIN_RESPUESTA
        assert fila["numero_plc"] is None, "no manda nada: no se muestra nada"
        assert fila["contador"] is None
        assert fila["ultimo_numero_plc"] == "BDTS5318XB", "lo último, aparte"
        assert fila["ultimo_contador"] == 240

    def test_un_numero_que_no_se_resolvio_sale_como_rechazado(self):
        r = RegistroLecturas()
        r.anotar(ESTACION, "LH", numero_plc="XXXX", validado=False,
                 error="PART_NUMBER_NO_EXISTE_BD")

        (fila,) = r.snapshot()

        assert fila["estado"] == lec.RECHAZADA


class TestLoQueYaNoExiste:
    def test_un_lado_que_se_quito_de_la_configuracion_desaparece(self):
        r = RegistroLecturas()
        r.anotar(ESTACION, "LH", ip=IP, contador=1)
        r.anotar(ESTACION, "RH", ip=IP, contador=1)

        r.conservar_lados(ESTACION, {"LH"})

        assert [f["lado"] for f in r.snapshot()] == ["LH"]

    def test_una_estacion_que_sale_de_la_ip_desaparece(self):
        r = RegistroLecturas()
        r.anotar("MM05", "LH", ip=IP, contador=1)
        r.anotar("MM06", "LH", ip=IP, contador=1)
        r.anotar("MK05", "LH", ip="10.1.2.1", contador=1)

        r.olvidar_ip(IP, excepto=["MM05"])

        assert sorted(f["estacion"] for f in r.snapshot()) == ["MK05", "MM05"]
