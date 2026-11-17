"""
Un error de la base a media transacción nunca pierde ni duplica piezas.

Tres caminos lo hacían en silencio, porque el error se atrapaba y el ciclo
seguía como si nada:

  1. Un interbloqueo al escribir histories o al crear el acumulador. SQL Server
     ya había revertido TODO el ciclo, pero el código continuaba, hacía commit
     de lo que viniera después y avanzaba la línea base: esas piezas no
     quedaban en ningún lado.
  2. Un fallo al leer pieces_per_shot se tomaba como "1 pieza por golpe": 10
     golpes de un troquel de 4 se registraban como 10 piezas, no 40.
  3. Un fallo al preguntar si había corrida en progreso se tomaba como "sí":
     todo lo que traía el contador de una corrida nueva se daba por ya contado.

Y uno más, que duplicaba: si el incremento llenaba una orden y el sobrante no
tenía dónde quedar, lo de la orden se confirmaba con la línea base sin mover,
y el siguiente ciclo lo volvía a registrar entero.

La regla en todos: si no se puede registrar el ciclo COMPLETO, no se registra
nada y la línea base no se mueve. El contador del PLC es absoluto, así que el
siguiente ciclo recupera el delta entero.
"""

import logging
from datetime import date, datetime

import pyodbc
import pytest

import Prensas
from persistence import catalogo
from persistence import ordenes as ord_sql
from tests.base_falsa import BaseFalsa, ConexionFalsa, Registro
from tests.dobles import CursorFalso
from tests.test_interbloqueo import interbloqueo

LOG = logging.getLogger("test")

IP = "10.1.2.1"
ESTACION = "MK05"
PARTE = "DGH97065ZA"
MOMENTO = datetime(2026, 9, 21, 12, 0, 0)

PRENSA = "2500T  TR"
TROQUEL = "BDTS28BFC-P"


def normalizado(sql):
    return " ".join(sql.split())


class BaseQueFalla(BaseFalsa):
    """
    Lanza `error` las primeras `veces` que se ejecuta una consulta que contiene
    `fragmento`.

    Si el error es un interbloqueo, ANTES revierte la transacción, como hace
    SQL Server con su víctima: se pierde todo lo escrito en ella, no solo esa
    sentencia. Sin eso, la prueba no vería la pérdida que se quiere evitar.
    """

    def __init__(self, *a, fragmento, error, veces=1, **kw):
        super().__init__(*a, **kw)
        self.fragmento = fragmento
        self.error = error
        self.veces = veces
        self.conexion = ConexionFalsa(self)

    def _despachar(self, sql, p):
        if self.fragmento in sql and self.veces > 0:
            self.veces -= 1
            if Prensas.es_interbloqueo(self.error):
                self.conexion.rollback()
            raise self.error
        return super()._despachar(sql, p)


@pytest.fixture(autouse=True)
def hoy_fijo(monkeypatch):
    """La fecha planificada sale de date.today(); se fija a la de las órdenes."""
    class _FechaFija(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 21)
    monkeypatch.setattr(Prensas, "date", _FechaFija)


@pytest.fixture
def armar(tmp_path, monkeypatch):
    def _armar(base, conexion=None):
        conexion = conexion or getattr(base, "conexion", None) or ConexionFalsa(base)
        monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
        monkeypatch.setattr(Prensas, "create_connection", lambda: conexion)
        monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
        monkeypatch.setattr(Prensas, "registrar_error_validacion", lambda *a, **k: None)
        Prensas.REGISTRO.olvidar_estacion(ESTACION)
        Prensas.REGISTRO.olvidar_estacion(PRENSA)
        return Prensas.IPDataProcessor(IP)
    return _armar


def orden(id, num, planned, estacion=ESTACION, parte=PARTE):
    return Registro(id, estacion, parte, orden=num, planned=planned,
                    status=ord_sql.PENDIENTE, planned_date="2026-09-21",
                    created_at=f"2026-09-21 00:00:{id % 60:02d}")


def procesar(procesador, contador):
    return procesador._process_estacion({
        "estacion": ESTACION, "area": "Chasis", "plc_ok": True, "ts": MOMENTO,
        "datos": [{"parte": PARTE, "original": PARTE, "contador": contador,
                   "tiempo": 1.5, "lado": "LH", "validado": True, "troquel_id": None}],
    })


def golpear(procesador, contador):
    return procesador._process_estacion({
        "estacion": PRENSA, "area": "Estampado", "plc_ok": True, "ts": MOMENTO,
        "datos": [{"parte": TROQUEL, "original": TROQUEL, "contador": contador,
                   "tiempo": 2.4, "lado": "LH", "validado": True, "troquel_id": 77}],
    })


# ───────────────────── 1. el interbloqueo no se traga ─────────────────────

class TestElInterbloqueoSiempreSeReintenta:
    def test_al_escribir_histories(self, armar):
        """
        Antes: el INSERT a histories atrapaba el error, el ciclo seguía y
        hacía commit con la orden ya revertida. La orden quedaba en 0 y la
        línea base en 10: diez piezas perdidas.
        """
        base = BaseQueFalla([orden(101, "4051130", planned=900)],
                            partes=[(ESTACION, PARTE)],
                            sync_from={ESTACION: date(2000, 1, 1)},
                            fragmento="INSERT INTO histories", error=interbloqueo())
        procesador = armar(base)

        procesar(procesador, 10)

        assert base[101].produced == 10
        assert len(base.histories) == 1

    def test_al_crear_el_acumulador(self, armar):
        """Antes se devolvía como DB_ERROR y el ciclo confirmaba lo demás."""
        base = BaseQueFalla([], partes=[(ESTACION, PARTE)],
                            sync_from={ESTACION: date(2000, 1, 1)},
                            fragmento="INSERT INTO production_records", error=interbloqueo())
        procesador = armar(base)

        procesar(procesador, 10)

        assert sum(a.produced for a in base.acumuladores) == 10

    def test_si_gana_todos_los_intentos_el_siguiente_ciclo_recupera_todo(self, armar):
        base = BaseQueFalla([orden(101, "4051130", planned=900)],
                            partes=[(ESTACION, PARTE)],
                            sync_from={ESTACION: date(2000, 1, 1)},
                            fragmento="INSERT INTO histories", error=interbloqueo(),
                            veces=Prensas.BD_INTENTOS_INTERBLOQUEO)
        procesador = armar(base)

        procesar(procesador, 10)
        assert base[101].produced == 0, "no quedó nada a medias"

        procesar(procesador, 15)
        assert base[101].produced == 15, "las 10 del ciclo fallido no se perdieron"


# ───────────────────────── 2. piezas por golpe ─────────────────────────

class TestPiezasPorGolpe:
    def base_estampado(self, **kw):
        return BaseQueFalla([orden(201, "A", planned=5000, estacion=PRENSA, parte=TROQUEL)],
                            partes=[(PRENSA, TROQUEL)],
                            multiplicadores={(PRENSA, TROQUEL): 4},
                            sync_from={PRENSA: date(2000, 1, 1)}, **kw)

    def test_si_la_base_no_responde_no_se_registra_con_uno(self, armar):
        """
        Antes: 10 golpes x 1 = 10 piezas y la línea base en 10. Al siguiente
        ciclo, 2 golpes x 4 = 8. Total 18 piezas por 48 reales.
        """
        base = self.base_estampado(fragmento="pieces_per_shot",
                                   error=pyodbc.Error("08S01", "Communication link failure"))
        procesador = armar(base)

        golpear(procesador, 10)
        assert base[201].produced == 0, "no se adivina el multiplicador"

        golpear(procesador, 12)
        assert base[201].produced == 48, "12 golpes x 4 piezas"

    def test_se_lee_de_la_misma_fila_a_la_que_se_registra(self):
        """
        Por id, no por número+estación: con el número dado de alta dos veces
        (la vigente y una de baja), buscar por número podía tomar el valor de
        la de baja.
        """
        cur = CursorFalso([[("4",)]])

        assert catalogo.obtener_multiplicador(cur, 40689) == 4
        assert "a.attributable_id = ?" in cur.sql
        assert cur.params == (catalogo.TIPO_ATRIBUTO, 40689)

    def test_sin_fila_vigente_no_se_registra_de_menos(self, armar):
        """Sin la fila no hay pieces_per_shot que leer: se rechaza, no se supone 1."""
        base = BaseFalsa([orden(201, "A", planned=5000, estacion=PRENSA, parte=TROQUEL)],
                         partes=[("OTRA", "X")],
                         sync_from={PRENSA: date(2000, 1, 1)})
        procesador = armar(base)

        golpear(procesador, 10)

        assert base[201].produced == 0


# ─────────────────── 3. la corrida en progreso no se adivina ───────────────────

class TestCorridaEnProgresoSinRespuesta:
    def test_no_se_da_por_contado_lo_que_trae_el_contador(self, armar):
        """
        Una parte nueva llega con el contador en 50 y no había orden en
        progreso: son 50 piezas de esta corrida. Antes, si la pregunta fallaba
        se suponía que sí había, la base quedaba en 50 y esas piezas se perdían.
        """
        base = BaseQueFalla([orden(101, "4051130", planned=900)],
                            partes=[(ESTACION, PARTE)],
                            sync_from={ESTACION: date(2000, 1, 1)},
                            fragmento=normalizado(ord_sql.SQL_CORRIDA_EN_PROGRESO),
                            error=pyodbc.Error("08S01", "Communication link failure"))
        procesador = armar(base)

        procesar(procesador, 50)
        procesar(procesador, 52)

        assert base[101].produced == 52


# ───────────────── el sobrante sin dónde quedar no duplica ─────────────────

class BaseQueCierraUnaOrden(BaseFalsa):
    """Otro proceso cierra la orden `id_cerrada` justo antes de que se le escriba, una vez."""

    def __init__(self, *a, id_cerrada, **kw):
        super().__init__(*a, **kw)
        self.id_cerrada = id_cerrada

    def _sumar_a_orden(self, piezas, fecha, id_registro):
        if id_registro == self.id_cerrada:
            self.id_cerrada = None
            return []
        return super()._sumar_a_orden(piezas, fecha, id_registro)


class TestTodoONada:
    def test_lo_escrito_a_medias_se_revierte_y_no_se_cuenta_dos_veces(self, armar):
        """
        8 piezas: 5 llenan la orden A, 3 iban a B pero otro proceso la cerró, y
        la parte no está en el catálogo para abrir acumulador. Antes las 5 de A
        se confirmaban y el ciclo siguiente volvía a registrar las 8: 13.
        """
        base = BaseQueCierraUnaOrden(
            [orden(101, "A", planned=5), orden(102, "B", planned=100)],
            partes=[("OTRA", "X")], sync_from={ESTACION: date(2000, 1, 1)},
            id_cerrada=102)
        procesador = armar(base)

        procesar(procesador, 8)
        assert base[101].produced == 0, "se revirtió el ciclo completo"

        procesar(procesador, 8)
        assert base[101].produced + base[102].produced == 8, "8 piezas, no 13"
