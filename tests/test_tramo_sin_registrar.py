"""
Lo que avanza el registro de un lado mientras NADIE lo puede registrar no se lo
lleva la parte que llega después.

El caso real, de la 1200T AP el 19 de noviembre de 2026:

    12:10:59  montan el troquel 'VA4050083'; su MDI no existe en el catálogo
    14:01:12  el troquel lleva 1,117 golpes rechazados; el registro va en 2,345
    14:01:13  eligen BDTT70662 sin reiniciar el contador

BDTT70662 arrancó desde 1,228, el último valor registrado, y se llevó los 1,117
golpes de 'VA4050083'. El registro del lado solo se anotaba cuando alguien
registraba, y durante el rechazo nadie registró.

Tres caminos llevaban al mismo hueco:
  - un MDI que no existe en el catálogo (solo Estampado);
  - una parte sin catálogo, en cualquier área;
  - un lado que llega en blanco con el contador avanzando.

En los tres, el avance queda ahora en un tramo sin registrar del lado, y la
siguiente parte distinta arranca después de él. Si lo que llega es la misma
parte que se rechazaba (la dieron de alta) o la que estaba antes del lado en
blanco, el tramo es suyo: se comporta exactamente como antes.
"""

import json
import logging
from datetime import date, datetime, timedelta

import pytest

import Prensas
from observabilidad import metricas as obs_metricas
from persistence import ordenes as ord_sql
from tests.base_falsa import BaseFalsa, ConexionFalsa, Registro
from tests.test_interbloqueo import ConexionQueSeAtora

LOG = logging.getLogger("test")
IP = "10.1.2.99"
INICIO = datetime(2026, 11, 19, 10, 0, 0)


@pytest.fixture(autouse=True)
def hoy_fijo(monkeypatch):
    """La fecha planificada sale de date.today(): se fija al día del caso real."""
    class _FechaFija(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 19)
    monkeypatch.setattr(Prensas, "date", _FechaFija)


class Estacion:
    """El procesador REAL contra la base falsa, con un reloj que avanza por lectura."""

    def __init__(self, tmp_path, monkeypatch, nombre, area, partes,
                 multiplicadores=None):
        self.nombre, self.area = nombre, area
        self.tmp_path, self.monkeypatch = tmp_path, monkeypatch
        self.base = BaseFalsa(
            [], partes=[(nombre, p) for p in partes],
            multiplicadores={(nombre, p): m for p, m in (multiplicadores or {}).items()},
            sync_from={nombre: date(2000, 1, 1)})
        self.conexion = ConexionFalsa(self.base)
        self.reloj = INICIO
        self._siguiente_orden = 101
        monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
        monkeypatch.setattr(Prensas, "create_connection", lambda: self.conexion)
        monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
        monkeypatch.setattr(Prensas, "registrar_error_validacion", lambda *a, **k: None)
        Prensas.REGISTRO.olvidar_estacion(nombre)
        self.procesador = Prensas.IPDataProcessor(IP)

    def orden(self, parte, planeado=99999):
        """Una orden abierta y holgada, para leer lo registrado de cada parte."""
        id_ = self._siguiente_orden
        self._siguiente_orden += 1
        self.base.registros[id_] = Registro(
            id_, self.nombre, parte, orden=str(4000000 + id_), planned=planeado,
            status=ord_sql.PENDIENTE, planned_date="2026-11-19",
            created_at="2026-11-19 00:00:00")
        return id_

    def producido(self, id_orden):
        return self.base[id_orden].produced

    def leer(self, *lados, plc_ok=True, minutos=1):
        """Una lectura del PLC con lo que manda cada lado (ver parte/rechazo/blanco)."""
        self.reloj += timedelta(minutes=minutos)
        return self.procesador._process_estacion({
            "estacion": self.nombre, "area": self.area, "plc_ok": plc_ok,
            "ts": self.reloj,
            "datos": [d for d in lados if "en_blanco" not in d],
            "en_blanco": [{"lado": d["lado"], "contador": d["contador"]}
                          for d in lados if "en_blanco" in d],
        })

    def reiniciar_servicio(self):
        """El servicio se detiene y vuelve a arrancar: todo sale del archivo de estado."""
        self.procesador.save_state()
        self.procesador = Prensas.IPDataProcessor(IP)

    def tramo(self, lado):
        return self.procesador.tramos_lado.get(f"{self.nombre}_{lado}")


def parte(numero, contador, lado="LH", original=None):
    """Un lado con una parte válida, tal como la entrega el lector."""
    return {"parte": numero, "original": original or numero, "contador": contador,
            "tiempo": 2.0, "lado": lado, "validado": True, "troquel_id": None}


def rechazo(original, contador, lado="LH", error="MDI_NO_EXISTE"):
    """Un lado cuyo número no resolvió en el lector (un MDI que no existe)."""
    return {"parte": None, "original": original, "contador": contador,
            "tiempo": 2.0, "lado": lado, "validado": False,
            "error_validacion": error, "troquel_id": None}


def blanco(contador, lado="RH"):
    """Un lado que el PLC mandó sin número de parte."""
    return {"en_blanco": True, "lado": lado, "contador": contador}


# ───────────────────────────── los tres caminos ─────────────────────────────

T1200 = "1200T  AP"


class TestMdiQueNoExiste:
    def test_el_troquel_rechazado_no_se_lo_hereda_el_siguiente(
            self, tmp_path, monkeypatch, caplog):
        """El caso de arriba, con sus números: BDTT70662 se queda con sus 60."""
        est = Estacion(tmp_path, monkeypatch, T1200, "Estampado",
                       ["DGJ428B04A", "BDTT70662"],
                       multiplicadores={"DGJ428B04A": 2, "BDTT70662": 2})
        dgj, bdtt = est.orden("DGJ428B04A"), est.orden("BDTT70662")

        est.leer(parte("DGJ428B04A", 0))
        est.leer(parte("DGJ428B04A", 1228))
        est.leer(rechazo("VA4050083", 1228))    # montan el troquel sin catálogo
        est.leer(rechazo("VA4050083", 1800))
        est.leer(rechazo("VA4050083", 2345))    # 1,117 golpes rechazados
        with caplog.at_level(logging.INFO, logger="test"):
            est.leer(parte("BDTT70662", 2345))  # la eligen sin reiniciar
        est.leer(parte("BDTT70662", 0))         # el operador reinicia
        est.leer(parte("BDTT70662", 60))        # sus 60 golpes reales

        assert est.producido(dgj) == 1228 * 2
        assert est.producido(bdtt) == 60 * 2, "los 1,117 golpes no son de BDTT70662"
        assert "1117 golpes sin registrar mientras se rechazaba 'VA4050083'" in caplog.text
        assert est.tramo("LH") is None, "el tramo se cerró al llegar BDTT70662"

    def test_tras_un_reinicio_no_se_toma_como_reinicio_de_la_siguiente(
            self, tmp_path, monkeypatch):
        """
        1200T AP, 19-nov 18:08 a 19:14. Lo último registrado era 3,840; el
        troquel 'DGH928B12-B22' (sin catálogo) arrancó con el contador en 0 y
        llegó a 1,804. BDTS28BFC-BK llegó leyendo 1,804, y como era menor que
        3,840 se tomó como reinicio: se registraron los 1,804 completos.
        """
        est = Estacion(tmp_path, monkeypatch, T1200, "Estampado",
                       ["BDTS54314A", "BDTS28BFC-BK"])
        bdts543, bdts28 = est.orden("BDTS54314A"), est.orden("BDTS28BFC-BK")

        est.leer(parte("BDTS54314A", 0))
        est.leer(parte("BDTS54314A", 3840))
        est.leer(rechazo("DGH928B12-B22", 0))
        est.leer(rechazo("DGH928B12-B22", 900))
        est.leer(rechazo("DGH928B12-B22", 1804))
        est.leer(parte("BDTS28BFC-BK", 1804))
        est.leer(parte("BDTS28BFC-BK", 0))
        est.leer(parte("BDTS28BFC-BK", 80))

        assert est.producido(bdts543) == 3840
        assert est.producido(bdts28) == 80

    def test_si_dan_de_alta_el_mdi_en_marcha_se_queda_con_todo_su_tramo(
            self, tmp_path, monkeypatch, caplog):
        """
        Lo que NO debe cambiar. El PLC manda 'DA6A53-54951' con guion y el
        catálogo lo tenía con diagonal; a media corrida alguien da de alta la
        versión con guion. Ese troquel es el que estuvo golpeando: sus 450
        golpes rechazados son suyos.
        """
        est = Estacion(tmp_path, monkeypatch, T1200, "Estampado",
                       ["BDTS50261-BK", "DA6A53/54951-BK"])
        bdts, da6 = est.orden("BDTS50261-BK"), est.orden("DA6A53/54951-BK")

        est.leer(parte("BDTS50261-BK", 0))
        est.leer(parte("BDTS50261-BK", 500))
        est.leer(rechazo("DA6A53-54951", 500))
        est.leer(rechazo("DA6A53-54951", 900))
        with caplog.at_level(logging.INFO, logger="test"):
            est.leer(parte("DA6A53/54951-BK", 950, original="DA6A53-54951"))  # ya resuelve
        est.leer(parte("DA6A53/54951-BK", 1000, original="DA6A53-54951"))

        assert est.producido(bdts) == 500
        assert est.producido(da6) == 500, "500 → 1,000 es todo de ese troquel"
        assert "se queda con los 400 golpes del tramo" in caplog.text

    def test_si_el_rechazo_cambia_cada_numero_lleva_su_propio_tramo(
            self, tmp_path, monkeypatch):
        """
        Dos MDI rechazados seguidos y luego dan de alta el segundo: se lleva lo
        suyo (150 → 200), no lo del primero (100 → 150).
        """
        est = Estacion(tmp_path, monkeypatch, T1200, "Estampado", ["A1", "Z1"])
        a1, z1 = est.orden("A1"), est.orden("Z1")

        est.leer(parte("A1", 0))
        est.leer(parte("A1", 100))
        est.leer(rechazo("X-1", 100))
        est.leer(rechazo("X-1", 150))
        est.leer(rechazo("Z-1", 150))
        est.leer(rechazo("Z-1", 180))
        est.leer(parte("Z1", 200, original="Z-1"))

        assert est.producido(a1) == 100
        assert est.producido(z1) == 50


MP30 = "MP30"


class TestParteSinCatalogo:
    def test_la_parte_que_regresa_no_se_lleva_lo_de_la_rechazada(
            self, tmp_path, monkeypatch):
        """
        Fuera de Estampado. MP30: cambian a BDTS7018P/180, que no existe en su
        catálogo; produce 40 y regresan a DGH970760B sin reiniciar el contador.
        """
        est = Estacion(tmp_path, monkeypatch, MP30, "Carrocería Fase 2", ["DGH970760B"])
        dgh = est.orden("DGH970760B")

        est.leer(parte("DGH970760B", 0))
        est.leer(parte("DGH970760B", 100))
        est.leer(parte("BDTS7018P/180", 100))
        est.leer(parte("BDTS7018P/180", 120))
        est.leer(parte("BDTS7018P/180", 140))
        est.leer(parte("DGH970760B", 140))
        est.leer(parte("DGH970760B", 150))

        assert est.producido(dgh) == 110, "las 40 de BDTS7018P/180 no son suyas"
        assert est.base.acumuladores == [], "y no se inventa nada para la que no existe"

    def test_si_la_dan_de_alta_en_marcha_se_queda_con_todo_lo_suyo(
            self, tmp_path, monkeypatch):
        """
        El reintento de siempre (test_no_mueve_la_linea_base_para_poder_reintentar),
        ahora con otra parte registrada antes en el mismo lado.
        """
        est = Estacion(tmp_path, monkeypatch, MP30, "Carrocería Fase 2", ["DGH970760B"])
        dgh = est.orden("DGH970760B")

        est.leer(parte("DGH970760B", 0))
        est.leer(parte("DGH970760B", 100))
        est.leer(parte("BDTS7018P/180", 110))
        est.leer(parte("BDTS7018P/180", 130))
        est.base.partes.add((MP30, "BDTS7018P/180"))     # la dan de alta
        est.leer(parte("BDTS7018P/180", 135))

        assert est.producido(dgh) == 100
        nueva = [r for r in est.base.acumuladores if r.numero == "BDTS7018P/180"]
        assert nueva[0].produced == 35, "100 → 135 corrió con ella en el lado"
        assert est.tramo("LH") is None

    def test_si_su_gemela_registra_no_hay_tramo(self, tmp_path, monkeypatch):
        """
        Un troquel que saca dos números por golpe, uno sin fila vigente en el
        catálogo. El lado tuvo dueño: la que sí existe registra el golpe completo
        y no se abre ningún tramo.
        """
        est = Estacion(tmp_path, monkeypatch, T1200, "Estampado", ["P1"])
        p1 = est.orden("P1")

        est.leer(parte("P1", 0, original="P1/P2"), parte("P2", 0, original="P1/P2"))
        est.leer(parte("P1", 10, original="P1/P2"), parte("P2", 10, original="P1/P2"))
        est.leer(parte("P1", 25, original="P1/P2"), parte("P2", 25, original="P1/P2"))

        assert est.producido(p1) == 25
        assert est.tramo("LH") is None


MM11 = "MM11"


class TestLadoEnBlanco:
    def test_otra_parte_no_se_lleva_lo_que_avanzo_en_blanco(self, tmp_path, monkeypatch):
        """El número llega vacío media hora, el contador avanza 60 y aparece otra parte."""
        est = Estacion(tmp_path, monkeypatch, MM11, "Carrocería Fase 1",
                       ["DGH97027YA", "BDTS7027YA"])
        a, b = est.orden("DGH97027YA"), est.orden("BDTS7027YA")

        est.leer(parte("DGH97027YA", 0, lado="RH"))
        est.leer(parte("DGH97027YA", 200, lado="RH"))
        est.leer(blanco(200))
        est.leer(blanco(260), minutos=30)
        est.leer(parte("BDTS7027YA", 260, lado="RH"))
        est.leer(parte("BDTS7027YA", 270, lado="RH"))

        assert est.producido(a) == 200
        assert est.producido(b) == 10

    def test_si_regresa_la_misma_parte_lo_de_ese_rato_es_suyo(self, tmp_path, monkeypatch):
        """Lo que NO debe cambiar: el operador borró el número pero siguió con la misma."""
        est = Estacion(tmp_path, monkeypatch, MM11, "Carrocería Fase 1", ["DGH97027YA"])
        a = est.orden("DGH97027YA")

        est.leer(parte("DGH97027YA", 0, lado="RH"))
        est.leer(parte("DGH97027YA", 200, lado="RH"))
        est.leer(blanco(230))
        est.leer(blanco(260))
        est.leer(parte("DGH97027YA", 270, lado="RH"))

        assert est.producido(a) == 270

    def test_el_otro_lado_sigue_contando_normal(self, tmp_path, monkeypatch):
        """
        RH queda en blanco y después entra otra parte. LH, que nunca dejó de
        producir, no se entera.
        """
        est = Estacion(tmp_path, monkeypatch, MM11, "Carrocería Fase 1",
                       ["DGH97027YA", "BDTS7027YA", "DGH97027ZA"])
        a, b, c = est.orden("DGH97027YA"), est.orden("BDTS7027YA"), est.orden("DGH97027ZA")

        est.leer(parte("DGH97027YA", 0, lado="LH"), parte("BDTS7027YA", 0, lado="RH"))
        est.leer(parte("DGH97027YA", 10, lado="LH"), blanco(5))
        est.leer(parte("DGH97027YA", 20, lado="LH"), blanco(9))
        est.leer(parte("DGH97027YA", 30, lado="LH"), parte("DGH97027ZA", 12, lado="RH"))

        assert est.producido(a) == 30
        assert est.producido(b) == 0
        assert est.producido(c) == 3, "solo lo que avanzó con DGH97027ZA en el lado"

    def test_con_lectura_parcial_no_se_concluye_nada(self, tmp_path, monkeypatch):
        """
        Un bloque que falló deja el lado sin número aunque el PLC sí lo mande:
        no abre tramo, y al volver la lectura buena la parte cobra el hueco.
        """
        est = Estacion(tmp_path, monkeypatch, MM11, "Carrocería Fase 1", ["DGH97027YA"])
        a = est.orden("DGH97027YA")

        est.leer(parte("DGH97027YA", 0, lado="RH"))
        est.leer(parte("DGH97027YA", 200, lado="RH"))
        est.leer(blanco(230), plc_ok=False)
        est.leer(parte("DGH97027YA", 240, lado="RH"))

        assert est.producido(a) == 240
        assert est.tramo("RH") is None


# ───────────────────── lo que NO se debe romper al arreglarlo ─────────────────────

class TestLoQueSigueIgual:
    def test_un_rechazo_pasajero_no_abre_tramo(self, tmp_path, monkeypatch):
        """
        La base no respondió al traducir el MDI. Cuando vuelve, la misma parte
        cobra todo lo que avanzó mientras tanto, como siempre.
        """
        est = Estacion(tmp_path, monkeypatch, T1200, "Estampado", ["DGJ428B04A"])
        dgj = est.orden("DGJ428B04A")

        est.leer(parte("DGJ428B04A", 0))
        est.leer(parte("DGJ428B04A", 100))
        est.leer(rechazo("DGJ428B04A", 150, error="SIN_CONEXION_BD"))
        est.leer(rechazo("DGJ428B04A", 155, error="DB_ERROR"))
        est.leer(parte("DGJ428B04A", 160))

        assert est.tramo("LH") is None
        assert est.producido(dgj) == 160

    def test_un_interbloqueo_no_cierra_el_tramo_a_medias(self, tmp_path, monkeypatch):
        """
        SQL Server revierte el ciclo en que llega la parte nueva: el reintento
        vuelve a decidir igual, y el tramo se cierra una sola vez, con el commit.
        """
        est = Estacion(tmp_path, monkeypatch, T1200, "Estampado", ["A1", "B1"])
        a1, b1 = est.orden("A1"), est.orden("B1")

        est.leer(parte("A1", 0))
        est.leer(parte("A1", 100))
        est.leer(rechazo("X-1", 100))
        est.leer(rechazo("X-1", 150))
        est.conexion = ConexionQueSeAtora(est.base, fallos=1)
        est.leer(parte("B1", 160))

        assert est.producido(a1) == 100
        assert est.producido(b1) == 10, "150 → 160 ya corrió con B1 en el lado"
        assert est.tramo("LH") is None
        assert est.procesador.contadores_lado[f"{T1200}_LH"]["contador"] == 160

    def test_el_tramo_sobrevive_un_reinicio_del_servicio(self, tmp_path, monkeypatch):
        """Reiniciar a media corrida rechazada no le regala el tramo a nadie."""
        est = Estacion(tmp_path, monkeypatch, T1200, "Estampado", ["A1", "B1"])
        a1, b1 = est.orden("A1"), est.orden("B1")

        est.leer(parte("A1", 0))
        est.leer(parte("A1", 100))
        est.leer(rechazo("X-1", 100))
        est.leer(rechazo("X-1", 150))
        est.reiniciar_servicio()
        est.leer(rechazo("X-1", 170))
        est.leer(parte("B1", 170))
        est.leer(parte("B1", 180))

        assert est.producido(a1) == 100
        assert est.producido(b1) == 10

    def test_desplegar_a_media_corrida_rechazada_tampoco(self, tmp_path, monkeypatch):
        """
        El día del despliegue el archivo de estado no trae tramos, porque lo
        escribió la versión anterior. El primer rechazo que se lee abre el tramo
        desde lo último registrado del lado, que sí está guardado.
        """
        est = Estacion(tmp_path, monkeypatch, T1200, "Estampado", ["A1", "B1"])
        a1, b1 = est.orden("A1"), est.orden("B1")

        est.leer(parte("A1", 0))
        est.leer(parte("A1", 100))
        est.procesador.save_state()
        archivo = est.procesador.state_file
        estado = json.loads(archivo.read_text(encoding="utf-8"))
        del estado["tramos"]                        # como lo dejó la versión anterior
        archivo.write_text(json.dumps(estado), encoding="utf-8")
        est.procesador = Prensas.IPDataProcessor(IP)

        est.leer(rechazo("X-1", 160))               # ya llevaba 60 rechazados
        est.leer(parte("B1", 160))
        est.leer(parte("B1", 175))

        assert est.producido(a1) == 100
        assert est.producido(b1) == 15
        assert est.tramo("LH") is None

    def test_el_lado_sin_numero_de_por_vida_no_registra_nada(self, tmp_path, monkeypatch):
        """
        MAI MB-60/RH y MS07/RH tienen contador pero ningún tag de número de
        parte: llegan siempre en blanco. Su tramo solo lleva la cuenta.
        """
        est = Estacion(tmp_path, monkeypatch, MM11, "Chasis", [])

        est.leer(blanco(0))
        est.leer(blanco(40))
        est.leer(blanco(90))

        assert est.base.registros == {}
        assert est.tramo("RH")["golpes"] == 90
        assert est.tramo("RH")["unidad"] == "piezas"

    def test_la_metrica_cuenta_lo_que_nadie_heredo(self, tmp_path, monkeypatch):
        est = Estacion(tmp_path, monkeypatch, "1500T  TR", "Estampado", ["A1", "B1"])
        est.orden("A1"), est.orden("B1")
        antes = obs_metricas.conteo_sin_registrar.labels("1500T  TR", "LH")._value.get()

        est.leer(parte("A1", 0))
        est.leer(parte("A1", 30))
        est.leer(rechazo("X-1", 30))
        est.leer(rechazo("X-1", 292))
        est.leer(parte("B1", 292))

        despues = obs_metricas.conteo_sin_registrar.labels("1500T  TR", "LH")._value.get()
        assert despues - antes == 262
