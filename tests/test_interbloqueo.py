"""
Qué pasa cuando SQL Server elige al recolector como víctima de un interbloqueo.

El caso real: el 21 de septiembre de 2026, MK05 recibió seis de estos en diez
minutos. La transacción se revierte ENTERA, pero la línea base del contador ya
había avanzado en memoria, así que esa producción no se volvía a intentar
nunca. No dejaba descuadre entre tablas —production_records e histories se
revierten juntos— y por eso solo se notaba cuadrando contra el PLC.

Lo que se fija aquí: nada del estado en memoria avanza antes del commit, y el
ciclo se reintenta porque el propio motor lo pide ("Rerun the transaction").
"""

import logging
from datetime import datetime

import pyodbc
import pytest

import Prensas
from persistence import ordenes as ord_sql
from tests.base_falsa import BaseFalsa, ConexionFalsa, Registro

LOG = logging.getLogger("test")

IP = "10.1.2.1"
ESTACION = "MK05"
PARTE = "DGH97065ZA"
AREA = "Chasis"
MOMENTO = datetime(2026, 9, 21, 12, 0, 0)
CORTE = datetime(2026, 7, 3).date()

#: El error tal cual lo escupió el driver en producción.
ERROR_REAL = (
    "('40001', '[40001] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
    "Transaction (Process ID 239) was deadlocked on lock resources with another "
    "process and has been chosen as the deadlock victim. Rerun the transaction. "
    "(1205) (SQLExecDirectW)')"
)


def interbloqueo():
    return pyodbc.Error('40001', ERROR_REAL)


class ConexionQueSeAtora(ConexionFalsa):
    """
    Revienta con interbloqueo en los primeros `fallos` intentos de commit.

    Revertir de verdad es lo que hace útil la prueba: si el doble no restaurara
    el estado, un reintento parecería duplicar producción que en la base real
    nunca llegó a existir.
    """

    def __init__(self, base, fallos):
        super().__init__(base)
        self.fallos = fallos
        self.intentos = 0

    def commit(self):
        self.intentos += 1
        if self.intentos <= self.fallos:
            # A propósito NO se revierte aquí: eso le toca al recolector. Si el
            # doble limpiara solo, la prueba pasaría igual aunque Prensas se
            # olvidara del rollback, y ese olvido deja la conexión del pool con
            # una transacción abortada.
            raise interbloqueo()
        super().commit()


@pytest.fixture(autouse=True)
def corte_fijo(monkeypatch):
    monkeypatch.setattr(Prensas, "ORDENES_DESDE", CORTE)


def armar(monkeypatch, tmp_path, conexion):
    monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
    monkeypatch.setattr(Prensas, "create_connection", lambda: conexion)
    monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
    monkeypatch.setattr(Prensas, "registrar_error_validacion", lambda *a, **k: None)
    Prensas.REGISTRO.olvidar_estacion(ESTACION)
    return Prensas.IPDataProcessor(IP)


def lectura(contador):
    return {"parte": PARTE, "original": PARTE, "contador": contador,
            "tiempo": 1.5, "lado": "LH", "validado": True, "troquel_id": None}


def procesar(procesador, contador, ts=MOMENTO):
    return procesador._process_estacion({
        "estacion": ESTACION, "datos": [lectura(contador)],
        "area": AREA, "plc_ok": True, "ts": ts,
    })


def orden(id, num, planned, produced=0):
    return Registro(id, ESTACION, PARTE, orden=num, planned=planned,
                    produced=produced, status=ord_sql.PENDIENTE,
                    planned_date="2026-09-21", created_at="2026-09-21 00:00:00")


class TestReconoceElError:
    def test_reconoce_el_interbloqueo_real_del_driver(self):
        assert Prensas.es_interbloqueo(interbloqueo())

    def test_reconoce_el_texto_aunque_no_sea_pyodbc(self):
        """Los envoltorios de otras capas pierden el sqlstate y dejan el texto."""
        assert Prensas.es_interbloqueo(RuntimeError(ERROR_REAL))

    @pytest.mark.parametrize("otro", [
        pyodbc.Error('08S01', 'Communication link failure'),
        RuntimeError("timeout"),
        ValueError("1205 piezas producidas"),   # el número solo no basta
    ])
    def test_no_confunde_otros_errores(self, otro):
        assert not Prensas.es_interbloqueo(otro)


class TestUnInterbloqueoNoCuestaProduccion:
    """Lo que de verdad importa: que las piezas acaben registradas."""

    def test_reintenta_y_la_produccion_queda(self, monkeypatch, tmp_path):
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])
        conexion = ConexionQueSeAtora(base, fallos=1)
        procesador = armar(monkeypatch, tmp_path, conexion)

        procesar(procesador, 8)

        assert conexion.intentos == 2, "falló una vez, reintentó y la segunda pasó"
        assert base[101].produced == 8

    def test_el_reintento_no_duplica(self, monkeypatch, tmp_path):
        """
        El primer intento alcanzó a escribir antes de que lo mataran. Si el
        reintento sumara encima de eso, la orden quedaría con el doble.
        """
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])
        conexion = ConexionQueSeAtora(base, fallos=2)
        procesador = armar(monkeypatch, tmp_path, conexion)

        procesar(procesador, 8)

        assert conexion.intentos == 3
        assert base[101].produced == 8, "8 piezas, no 16 ni 24"
        assert len(base.histories) == 1, "una sola fila en histories, no tres"

    def test_la_segunda_lectura_sigue_contando_bien(self, monkeypatch, tmp_path):
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])
        conexion = ConexionQueSeAtora(base, fallos=1)
        procesador = armar(monkeypatch, tmp_path, conexion)

        procesar(procesador, 8)
        procesar(procesador, 11)

        assert base[101].produced == 11


class TestSiNoSePuedeLaLineaBaseNoSeMueve:
    """
    El caso de MK05: el interbloqueo gana todos los intentos. Entonces la
    producción NO se da por registrada, y el siguiente ciclo la recupera.
    """

    def test_no_queda_nada_escrito(self, monkeypatch, tmp_path):
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])
        conexion = ConexionQueSeAtora(base, fallos=99)
        procesador = armar(monkeypatch, tmp_path, conexion)

        assert procesar(procesador, 8) is False
        assert base[101].produced == 0
        assert base.historias == []

    def test_agota_los_intentos_y_no_mas(self, monkeypatch, tmp_path):
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])
        conexion = ConexionQueSeAtora(base, fallos=99)
        procesador = armar(monkeypatch, tmp_path, conexion)

        procesar(procesador, 8)

        assert conexion.intentos == Prensas.BD_INTENTOS_INTERBLOQUEO

    def test_el_siguiente_ciclo_recupera_el_delta_completo(self, monkeypatch, tmp_path):
        """
        Esto es lo que se perdía: la línea base avanzaba a 8 aunque la base
        hubiera revertido, y esas 8 piezas no se volvían a intentar jamás.
        """
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])
        conexion = ConexionQueSeAtora(base, fallos=99)
        procesador = armar(monkeypatch, tmp_path, conexion)

        procesar(procesador, 8)          # se pierde entero
        conexion.fallos = 0              # la base se recupera
        procesar(procesador, 11)

        assert base[101].produced == 11, "las 8 del hueco no se perdieron"

    def test_tampoco_se_pierden_si_el_contador_no_vuelve_a_moverse(
            self, monkeypatch, tmp_path):
        """
        La prensa se paró justo después del interbloqueo. Si el caché de
        'contador congelado' hubiera avanzado, el ciclo siguiente saltaría esta
        parte por 'no ha cambiado nada' y las piezas se irían al olvido.
        """
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])
        conexion = ConexionQueSeAtora(base, fallos=99)
        procesador = armar(monkeypatch, tmp_path, conexion)

        procesar(procesador, 8)
        conexion.fallos = 0
        procesar(procesador, 8)          # el mismo contador, sin movimiento

        assert base[101].produced == 8


class TestOtrosErroresNoSeReintentan:
    def test_un_error_que_no_es_interbloqueo_se_intenta_una_sola_vez(
            self, monkeypatch, tmp_path):
        """
        Reintentar un fallo permanente solo multiplica el daño y llena el log.
        """
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])

        class ConexionRota(ConexionFalsa):
            def __init__(self, base):
                super().__init__(base)
                self.intentos = 0

            def commit(self):
                self.intentos += 1
                self.rollback()
                raise pyodbc.Error('08S01', 'Communication link failure')

        conexion = ConexionRota(base)
        procesador = armar(monkeypatch, tmp_path, conexion)

        assert procesar(procesador, 8) is False
        assert conexion.intentos == 1
        assert base[101].produced == 0


class TestElEstadoNoAvanzaSinCommit:
    """
    La regla de fondo, en su forma más directa: mientras el commit no pase,
    la memoria del recolector tiene que quedar como estaba.
    """

    def test_la_linea_base_en_memoria_se_queda_donde_estaba(
            self, monkeypatch, tmp_path):
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])
        conexion = ConexionQueSeAtora(base, fallos=99)
        procesador = armar(monkeypatch, tmp_path, conexion)

        procesar(procesador, 8)

        clave = f"{ESTACION}_{PARTE}_LH"
        reg = procesador.active_records.get(clave)
        assert reg is not None
        assert reg['contador_registro'] == 0, "no se dio por registrado el 8"

    def test_se_revierte_la_transaccion_al_fallar(self, monkeypatch, tmp_path):
        """
        Sin rollback explícito, la conexión vuelve al pool con una transacción
        abortada y el siguiente que la tome hereda el problema.
        """
        base = BaseFalsa([orden(101, "4051130", planned=900)],
                         partes=[(ESTACION, PARTE)])
        conexion = ConexionQueSeAtora(base, fallos=99)
        procesador = armar(monkeypatch, tmp_path, conexion)

        procesar(procesador, 8)

        assert conexion.rollbacks >= Prensas.BD_INTENTOS_INTERBLOQUEO
