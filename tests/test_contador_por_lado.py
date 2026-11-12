"""
El contador es del LADO, no de la parte.

El caso real, de MM11 el 6 de octubre de 2026 a las 08:46:47, en una sola
lectura:

    ⏹️ Corrida terminada en MM11_BDTS7027YA_RH: orden 308346 Detenida.
    ⚠️ Reset de contador en MM11/DGH97027YA/RH: 227 → 25.

Sale una parte, entra la otra, y el registro del lado todavía traía las 25
piezas que acababa de producir la que salió. Esas 25 se registraron DOS veces:
una bien a nombre de quien las hizo, y otra como corrida nueva de la que
entraba. El reinicio del PLC a 0 sí llega, pero llega después del cambio de
número.

Daba lo mismo que la parte entrante tuviera línea base vieja (la bajada se leía
como reinicio) o no la tuviera (se leía como corrida nueva): el número
registrado era el mismo, porque en los dos casos era lo que el registro del lado
traía. Por eso lo que se arregla aquí es uno solo: recordar en cuánto venía el
registro de cada (estación, lado) y contar desde ahí.

Medido contra producción, esto era el 2.9% de la planta en 14 días y hasta el
28% en 1200T AP.
"""

import logging
from datetime import date, datetime

import pytest

import Prensas
from persistence import ordenes as ord_sql
from tests.base_falsa import BaseFalsa, ConexionFalsa, Registro
from tests.test_interbloqueo import ConexionQueSeAtora

LOG = logging.getLogger("test")

IP = "10.1.2.63"
ESTACION = "MM11"
A = "DGH97027YA"          # la que ya venía corriendo
B = "BDTS7027YA"          # la que entra y heredaba el sobrante
AREA = "Chasis"           # el contador cuenta PIEZAS, no golpes
MOMENTO = datetime(2026, 10, 6, 8, 46, 47)


@pytest.fixture(autouse=True)
def hoy_fijo(monkeypatch):
    """
    get_current_shift() saca la fecha planificada de date.today() y solo mira la
    HORA del paquete. Sin fijarla, las órdenes sembradas con fecha 2026-10-06
    dejarían de encontrarse en cuanto cambiara el mes real.
    """
    class _FechaFija(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 6)
    monkeypatch.setattr(Prensas, "date", _FechaFija)


@pytest.fixture
def base():
    return BaseFalsa([], partes=[(ESTACION, A), (ESTACION, B)],
                     sync_from={ESTACION: date(2000, 1, 1)})


def armar(base, tmp_path, monkeypatch, conexion=None):
    monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
    monkeypatch.setattr(Prensas, "create_connection",
                        lambda: conexion or ConexionFalsa(base))
    monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
    monkeypatch.setattr(Prensas, "registrar_error_validacion", lambda *a, **k: None)
    Prensas.REGISTRO.olvidar_estacion(ESTACION)
    return Prensas.IPDataProcessor(IP)


@pytest.fixture
def procesador(base, tmp_path, monkeypatch):
    return armar(base, tmp_path, monkeypatch)


def lectura(contador, parte, lado="RH"):
    return {"parte": parte, "original": parte, "contador": contador,
            "tiempo": 1.5, "lado": lado, "validado": True, "troquel_id": None}


def procesar(procesador, datos, plc_ok=True, ts=MOMENTO):
    return procesador._process_estacion({
        "estacion": ESTACION, "datos": datos, "area": AREA,
        "plc_ok": plc_ok, "ts": ts,
    })


def orden(id, num, parte, planned, produced=0, status=ord_sql.PENDIENTE):
    return Registro(id, ESTACION, parte, orden=num, planned=planned,
                    produced=produced, status=status,
                    planned_date="2026-10-06", created_at="2026-10-06 00:00:00")


# ───────────────────── los dos caminos del fantasma ──────────────────────

class TestSobranteDelCambioDeParte:
    def test_base_rancia_no_se_queda_con_lo_que_produjo_la_otra(self, procesador, base):
        """
        El caso del log: A tiene línea base 27, el registro del lado trae 25
        porque son de B, y la bajada se leía como reinicio de A.
        """
        base.registros[101] = orden(101, "4051130", A, planned=900)
        base.registros[201] = orden(201, "4051131", B, planned=900)

        procesar(procesador, [lectura(10, A)])      # A arranca: 10 piezas
        procesar(procesador, [lectura(27, A)])      # A sigue: +17
        procesar(procesador, [lectura(0, B)])       # cambio de parte, PLC en 0
        procesar(procesador, [lectura(25, B)])      # B produce 25 de verdad
        procesar(procesador, [lectura(25, A)])      # A vuelve leyendo el sobrante

        assert base[101].produced == 27, "las 25 piezas de B no son de A"
        assert base[201].produced == 25

    def test_parte_sin_linea_base_no_hereda_el_contador_de_la_que_salio(
            self, procesador, base):
        """
        El otro camino, del log de MM11 el 9 de noviembre: B entra sin línea
        base y el registro todavía marca las 202 que acababa de hacer A.

            ⏹️ Corrida terminada en MM11_DGH97027YA_RH: orden 337292 Detenida.
            ▶️ MM11/BDTS7027YA: corrida nueva, el contador trae 202 por registrar.
            📦 202 piezas de BDTS7027YA en MM11 sin orden: acumulador 343308.
        """
        base.registros[101] = orden(101, "4051130", A, planned=900)
        base.registros[201] = orden(201, "4051131", B, planned=900)

        procesar(procesador, [lectura(202, A)])     # A: corrida nueva de 202
        procesar(procesador, [lectura(202, B)])     # B entra leyendo lo de A
        procesar(procesador, [lectura(0, B)])       # el PLC reinicia
        procesar(procesador, [lectura(15, B)])      # B produce 15 de verdad

        assert base[101].produced == 202
        assert base[201].produced == 15, "las 202 de A no son de B"

    def test_el_registro_compartido_que_no_reinicia_tampoco_duplica(
            self, procesador, base):
        """
        Hay lados cuyo registro no vuelve a 0 entre partes, solo sigue subiendo.
        Lo que avanzó mientras B era la parte reportada es de B —no hay forma de
        saber otra cosa, y es la única atribución sensata—, pero cuando A vuelve
        a aparecer no se le puede cobrar otra vez ese mismo tramo.
        """
        base.registros[101] = orden(101, "4051130", A, planned=900)
        base.registros[201] = orden(201, "4051131", B, planned=900)

        procesar(procesador, [lectura(100, A)])     # A: 100
        procesar(procesador, [lectura(150, B)])     # entra B, el registro va en 150
        procesar(procesador, [lectura(170, B)])     # B sigue hasta 170
        procesar(procesador, [lectura(170, A)])     # A vuelve leyendo 170

        assert base[101].produced == 100, "el tramo de B no se le cobra a A"
        assert base[201].produced == 70, "100→170 corrió con B reportada"


# ───────────────── lo que NO se debe romper al arreglarlo ────────────────

class TestLoQueSigueIgual:
    def test_dos_partes_del_mismo_golpe_cuentan_las_dos(self, procesador, base):
        """
        Un troquel que saca varios números por golpe reporta varias partes en el
        MISMO lado con el MISMO contador, y a cada una le toca el incremento
        completo. Por eso la observación del lado se lee del estado sin tocar y
        solo se escribe al hacer commit: si se actualizara al vuelo, la primera
        parte se llevaría el incremento y las demás verían cero.
        """
        base.registros[101] = orden(101, "4051130", A, planned=900)
        base.registros[201] = orden(201, "4051131", B, planned=900)

        procesar(procesador, [lectura(5, A), lectura(5, B)])
        procesar(procesador, [lectura(8, A), lectura(8, B)])

        assert base[101].produced == 8
        assert base[201].produced == 8

    def test_un_hueco_del_plc_no_duplica_ni_pierde(self, procesador, base):
        """Lectura parcial: no se concluye nada y al volver se cobra el delta."""
        base.registros[101] = orden(101, "4051130", A, planned=900)

        procesar(procesador, [lectura(138, A)])
        procesar(procesador, [], plc_ok=False)
        procesar(procesador, [lectura(145, A)])

        assert base[101].produced == 145

    def test_el_reinicio_de_la_misma_parte_sigue_contando_la_corrida_nueva(
            self, procesador, base):
        """
        El operador termina una corrida y arranca otra con la MISMA parte: lo que
        marque el contador después del reinicio es producción real.
        """
        base.registros[101] = orden(101, "4051130", A, planned=900)

        procesar(procesador, [lectura(50, A)])
        procesar(procesador, [lectura(3, A)])       # reinicio + 3 piezas

        assert base[101].produced == 53

    def test_sin_observacion_del_lado_respeta_la_orden_en_progreso(
            self, procesador, base):
        """
        Primer ciclo tras arrancar: sin observación del lado se decide como
        siempre, preguntando a la base. Lo que marca el contador ya está
        registrado y no se vuelve a cobrar.

        La prensa queda DETENIDA en ese mismo ciclo (el contador no se movió),
        pero el lado tiene que quedar observado de todos modos: si no, sigue
        sin protección mientras no llegue un golpe (ver TestLadoDetenidoDesdeElArranque).
        """
        base.registros[101] = orden(101, "4051130", A, planned=900, produced=300,
                                    status=ord_sql.EN_PROGRESO)

        procesar(procesador, [lectura(300, A)])

        assert base[101].produced == 300, "ya estaba contado"
        assert procesador.contadores_lado[f"{ESTACION}_RH"]["contador"] == 300

    def test_detenida_desde_el_arranque_no_necesita_avanzar_para_quedar_protegida(
            self, procesador, base):
        """
        El hueco real: antes de este arreglo, un lado sin observación se
        comportaba como el código viejo HASTA su primer avance (ver
        _anotar_contador_lado). Si la prensa estaba detenida desde que arrancó
        el recolector, ese avance podía no llegar nunca, y un cambio de número
        de parte en ese tramo volvía a inflarse —exactamente el fantasma que
        este archivo arregla, reaparecido por otra puerta.

        Aquí A llega ya EN_PROGRESO y detenida: el registro se observa en el
        primer ciclo sin que haga falta ni un golpe. B entra después leyendo el
        mismo valor (el registro no se movió) y no se le cuenta nada.
        """
        base.registros[101] = orden(101, "4051130", A, planned=900, produced=180,
                                    status=ord_sql.EN_PROGRESO)
        base.registros[201] = orden(201, "4051131", B, planned=900)

        procesar(procesador, [lectura(180, A)])      # detenida: se observa sin avanzar
        procesar(procesador, [lectura(180, B)])      # cambia de parte, el registro no se movió

        assert base[101].produced == 180
        assert base[201].produced == 0, "nada fantasma: el registro nunca avanzó"

    def test_un_interbloqueo_no_adelanta_la_observacion_del_lado(
            self, base, tmp_path, monkeypatch):
        """
        SQL Server revierte la transacción ENTERA. Si la observación del lado
        avanzara antes del commit, el reintento compararía contra un registro ya
        adelantado y esa producción se perdería en silencio. MM11 llevaba 615
        interbloqueos en siete semanas de log, así que no es un caso de borde.
        """
        base.registros[101] = orden(101, "4051130", A, planned=900)
        proc = armar(base, tmp_path, monkeypatch,
                     conexion=ConexionQueSeAtora(base, fallos=1))

        procesar(proc, [lectura(10, A)])

        assert base[101].produced == 10, "el reintento no duplica ni pierde"
        assert proc.contadores_lado[f"{ESTACION}_RH"]["contador"] == 10
