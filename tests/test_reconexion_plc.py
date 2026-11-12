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

Lo que se fija aquí: la línea base sobrevive a la ausencia.

Antes, lo que sobrevivía era la ENTRADA de la parte, y solo se olvidaba cuando
llevaba tanto sin aparecer que su contador ya no decía nada. Desde que el
registro del lado se anota aunque la estación esté detenida (ver
IPDataProcessor._anotar_contador_lado), es ESE dato el que sobrevive por su
cuenta y no depende de ninguna ventana: en cuanto el lado queda protegido
(ver _lado_protegido), la entrada de la parte se puede olvidar de inmediato
sin perder nada. La ventana sigue existiendo, pero solo como red de
seguridad para el lado que todavía no tiene observación propia.
"""

import json
import logging
import time
from datetime import date, datetime, timedelta

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


@pytest.fixture(autouse=True)
def hoy_fijo(monkeypatch):
    """
    get_current_shift() calcula la fecha planificada con date.today(), sin
    mirar la fecha del paquete simulado. Fijarla aquí la hace coincidir con
    las órdenes de este archivo (2026-09-21), sin importar qué día sea
    realmente cuando corren las pruebas.
    """
    class _FechaFija(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 21)
    monkeypatch.setattr(Prensas, "date", _FechaFija)


@pytest.fixture
def base():
    """Con sync_from ya pasado: estas pruebas no tienen que ver con el régimen
    de cuarentena, solo con la supervivencia de la línea base."""
    return BaseFalsa([], partes=[(ESTACION, PARTE), (ESTACION, OTRA)],
                     sync_from={ESTACION: date(2000, 1, 1)})


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
        """
        Lo que sobrevive ya no es la ENTRADA de la parte: es el registro del
        lado. La primera lectura ya deja contadores_lado["MK05_LH"] en 8 (se
        anota en cada avance, ver _anotar_contador_lado), y con eso protegido
        la entrada se puede olvidar de inmediato sin perder nada —si la parte
        vuelve, _linea_base_de_corrida le da la base correcta desde el lado.
        """
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [])

        assert clave() not in procesador.active_records, "ya no hace falta conservarla"
        assert procesador.contadores_lado[f"{ESTACION}_LH"]['contador'] == 8

    def test_la_orden_si_queda_detenida(self, procesador, base):
        """
        Olvidar la entrada de la parte no significa dejar la orden abierta:
        se detiene igual, aunque ya no quede ningún registro vivo que marcar
        como 'sin orden en curso' (ver test_la_linea_base_sobrevive_a_la_ausencia).
        """
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [])

        assert base[101].status == ord_sql.DETENIDO
        assert clave() not in procesador.active_records

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
        """
        Hay UN contador por lado, no uno por parte: el tag se identifica por
        tipo y lado ('Contador LH' en plc/decodificador.py::parse_tag) y nunca
        por número, y areas/estampado.py lo dice para los troqueles de varias
        piezas ("todos con el mismo contador").

        Así que el recorrido del registro de este lado es 0→8 con PARTE, reinicio,
        0→3 con OTRA, y 3→9 con PARTE otra vez: 17 piezas en total. Lo que se
        reparte tiene que sumar eso mismo —8+6 para PARTE y 3 para OTRA— sin
        duplicar ni perder nada.

        Esta prueba esperaba 9 para PARTE, que sale de comparar el 9 contra su
        línea base de 8 como si el registro fuera suyo. Tapaba las dos caras del
        mismo error: cobraba de más cuando el sobrante era grande (3,393 piezas
        fantasma en siete semanas de log de MM11) y de menos aquí, porque dejaba
        5 piezas sin atribuir a nadie.
        """
        base.registros[101] = orden(101, "4051130", planned=900)

        procesar(procesador, [lectura(8)])
        procesar(procesador, [lectura(3, parte=OTRA)])   # cambió de parte
        procesar(procesador, [lectura(9)])               # volvió la primera

        reg = procesador.active_records[clave()]
        assert reg['contador_registro'] == 9
        assert base[101].produced == 14, "sus 8 de antes + el tramo 3→9"


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
    La ventana de doce horas solo sigue importando mientras el LADO no tiene
    observación propia (ver IPDataProcessor._lado_protegido): protegido, la
    entrada de la parte se olvida de inmediato (ver
    TestLaParteQueDesapareceYVuelve.test_la_linea_base_sobrevive_a_la_ausencia)
    y nada de lo de aquí abajo aplica.

    El caso que sigue siendo real: justo después de migrar un archivo de
    estado de antes de `lados` (ver persistence/estado.py), o cualquier
    arranque donde ese lado todavía no se ha leído ni una vez.
    """

    def test_la_ventana_dura_un_turno_no_una_eternidad(self):
        """
        Un contador de hace días no dice nada. Si la ventana creciera sin
        límite, una parte que vuelve la semana entrante heredaría una línea
        base inventada y se perdería toda su primera corrida.
        """
        assert 3600 <= Prensas.VENTANA_OLVIDO_LINEA_BASE <= 24 * 3600

    def _procesador_sin_observacion_del_lado(self, tmp_path, monkeypatch, base,
                                             horas_de_ausencia):
        """
        Arma un IPDataProcessor con la entrada de la parte ya en el archivo de
        estado, pero SIN sección `lados` poblada: el escenario real de un
        archivo escrito antes de este arreglo, o de un lado que nunca se ha
        leído. Sin esa observación, _lado_protegido no tiene red que ofrecer y
        el código cae en la ventana de siempre.
        """
        monkeypatch.setattr(Prensas, "create_connection", lambda: ConexionFalsa(base))
        monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
        monkeypatch.setattr(Prensas, "registrar_error_validacion", lambda *a, **k: None)
        Prensas.REGISTRO.olvidar_estacion(ESTACION)
        monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)

        state_file = tmp_path / f"state_{IP.replace('.', '_')}.json"
        state_file.write_text(json.dumps({
            "version": 2,
            "partes": {
                clave(): {
                    "contador_registro": 8,
                    "multiplicador": 1,
                    "numero_original": PARTE,
                    "lado": "LH",
                    "id_en_curso": None,
                    "visto_en": time.time() - horas_de_ausencia * 3600,
                }
            },
            "lados": {},
        }), encoding="utf-8")

        proc = Prensas.IPDataProcessor(IP)
        assert proc.contadores_lado == {}, "precondición: el lado sin observación"
        return proc

    def test_sin_observacion_del_lado_se_conserva_dentro_de_la_ventana(
            self, base, tmp_path, monkeypatch):
        proc = self._procesador_sin_observacion_del_lado(
            tmp_path, monkeypatch, base, horas_de_ausencia=1)

        procesar(proc, [])

        assert clave() in proc.active_records

    def test_sin_observacion_del_lado_se_olvida_fuera_de_la_ventana(
            self, base, tmp_path, monkeypatch):
        # Trece horas sin aparecer. La cifra es absoluta a propósito: medirla
        # contra la propia constante haría que la prueba siguiera pasando
        # aunque alguien estirara la ventana a un año.
        proc = self._procesador_sin_observacion_del_lado(
            tmp_path, monkeypatch, base, horas_de_ausencia=13)

        procesar(proc, [])

        assert clave() not in proc.active_records


class TestPurgaAlArrancar:
    """
    Una estación cuyo PLC deja de responder del todo nunca vuelve a pasar por
    _process_estacion —no llega ningún paquete suyo—, así que ninguna de las
    dos ventanas de arriba la alcanza. Sus partes se quedarían en el archivo
    de estado para siempre. Esta purga corre una sola vez, al cargar el
    estado, y es la única red para ese caso (ver VENTANA_PURGA_ARRANQUE).
    """

    def _escribir_estado(self, tmp_path, partes):
        state_file = tmp_path / f"state_{IP.replace('.', '_')}.json"
        state_file.write_text(json.dumps({
            "version": 2,
            "partes": partes,
            "lados": {},
        }), encoding="utf-8")
        return state_file

    def _entrada(self, horas_de_ausencia, parte=PARTE):
        return {
            "contador_registro": 8,
            "multiplicador": 1,
            "numero_original": parte,
            "lado": "LH",
            "id_en_curso": None,
            "visto_en": time.time() - horas_de_ausencia * 3600,
        }

    def test_la_huerfana_de_mas_de_un_dia_se_olvida_al_arrancar(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
        self._escribir_estado(tmp_path, {clave(): self._entrada(25)})

        proc = Prensas.IPDataProcessor(IP)

        assert clave() not in proc.active_records

    def test_la_que_no_lleva_un_dia_sobrevive_al_arranque(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
        self._escribir_estado(tmp_path, {clave(): self._entrada(20)})

        proc = Prensas.IPDataProcessor(IP)

        assert clave() in proc.active_records

    def test_la_purga_no_toca_a_las_demas_partes_de_la_misma_estacion(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
        clave_otra = clave(OTRA)
        self._escribir_estado(tmp_path, {
            clave(): self._entrada(25),
            clave_otra: self._entrada(1, parte=OTRA),
        })

        proc = Prensas.IPDataProcessor(IP)

        assert clave() not in proc.active_records
        assert clave_otra in proc.active_records

    def test_lo_purgado_queda_fuera_del_archivo_tras_guardarse(
            self, tmp_path, monkeypatch):
        """
        La purga no solo limpia la memoria: si no se guardara, el próximo
        reinicio volvería a cargar la misma huérfana y repetiría el aviso
        para siempre.
        """
        monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
        state_file = self._escribir_estado(tmp_path, {clave(): self._entrada(25)})

        Prensas.IPDataProcessor(IP)

        contenido = json.loads(state_file.read_text(encoding="utf-8"))
        assert clave() not in contenido["partes"]


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
