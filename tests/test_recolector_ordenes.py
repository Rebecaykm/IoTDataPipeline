"""
El recolector completo: de lo que manda el PLC a lo que queda en la base.

A diferencia de test_registro_ordenes.py, aquí se ejercita `_process_estacion`
tal cual, con su caché de estado, su detección de reset y su manejo de lecturas
parciales. Es el cableado —no la aritmética— lo que se está comprobando.
"""

import logging
from datetime import datetime

import pytest

import Prensas
from persistence import ordenes as ord_sql
from tests.base_falsa import BaseFalsa, ConexionFalsa, Registro

LOG = logging.getLogger("test")

IP = "192.168.1.99"
ESTACION = "MK05"
PARTE = "BDTS53411-BK"
AREA = "Chasis"                       # el contador cuenta PIEZAS, no golpes
MANANA = datetime(2026, 8, 19, 9, 0, 0)
NOCHE = datetime(2026, 8, 19, 21, 30, 0)
CORTE = datetime(2026, 7, 3).date()   # las órdenes de aquí son de agosto


@pytest.fixture(autouse=True)
def corte_fijo(monkeypatch):
    """
    Fija la fecha de corte en vez de heredar la del .env de quien corra las
    pruebas. Prensas lee ORDENES_DESDE al importarse: sin esto, adelantar esa
    fecha en el .env deja las órdenes de estas pruebas fuera del corte y media
    docena falla por algo que no tiene nada que ver con lo que prueban.
    """
    monkeypatch.setattr(Prensas, "ORDENES_DESDE", CORTE)


@pytest.fixture
def base():
    return BaseFalsa([], partes=[(ESTACION, PARTE)])


@pytest.fixture
def procesador(base, tmp_path, monkeypatch):
    """Un IPDataProcessor con la base falsa y sin tocar disco del proyecto."""
    monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
    monkeypatch.setattr(Prensas, "create_connection", lambda: ConexionFalsa(base))
    monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
    monkeypatch.setattr(Prensas, "registrar_error_validacion",
                        lambda *a, **k: None)
    # El registro de estados es global: sin limpiarlo, una prueba puede pasar
    # con lo que dejó la anterior.
    Prensas.REGISTRO.olvidar_estacion(ESTACION)
    return Prensas.IPDataProcessor(IP)


def orden(id, num, planned, produced=0, status=ord_sql.PENDIENTE,
          fecha="2026-08-19", creado="2026-08-19 00:00:00"):
    return Registro(id, ESTACION, PARTE, orden=num, planned=planned,
                    produced=produced, status=status, planned_date=fecha,
                    created_at=creado)


def lectura(contador, parte=PARTE, lado="LH", tiempo=1.5):
    return {"parte": parte, "original": parte, "contador": contador,
            "tiempo": tiempo, "lado": lado, "validado": True, "troquel_id": None}


def paquete(datos, ts=MANANA, plc_ok=True):
    return {"estacion": ESTACION, "datos": datos, "area": AREA,
            "plc_ok": plc_ok, "ts": ts}


def procesar(procesador, datos, ts=MANANA, plc_ok=True):
    return procesador._process_estacion(paquete(datos, ts, plc_ok))


# ───────────────────────────── línea base ─────────────────────────────────

class TestArranque:
    def test_corrida_nueva_registra_lo_que_trae_el_contador(self, procesador, base):
        """Nadie estaba contando: las 12 piezas del PLC son producción real."""
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(12)])

        assert base[101].produced == 12
        assert base[101].status == ord_sql.EN_PROGRESO

    def test_corrida_ya_en_curso_no_vuelve_a_contar_el_arranque(self, procesador, base):
        """
        La orden ya venía en progreso: lo que marca el contador ya está
        registrado. Contarlo otra vez duplicaría producción.
        """
        base.registros[101] = orden(101, "4051130", planned=900, produced=500,
                                    status=ord_sql.EN_PROGRESO)
        procesar(procesador, [lectura(500)])

        assert base[101].produced == 500, "no debe sumar el arranque"

    def test_y_a_partir_de_ahi_cuenta_los_deltas(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900, produced=500,
                                    status=ord_sql.EN_PROGRESO)
        procesar(procesador, [lectura(500)])
        procesar(procesador, [lectura(507)])

        assert base[101].produced == 507


class TestCorridaNormal:
    def test_dos_ciclos_acumulan_el_delta(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10)])
        procesar(procesador, [lectura(25)])

        assert base[101].produced == 25
        assert base.conteo_en_histories() == 25

    def test_el_historial_lleva_el_numero_de_orden(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10)])

        assert len(base.histories) == 1
        assert base.histories[0]["shop_order_number"] == "4051130"
        assert base.histories[0]["quantity"] == 10

    def test_contador_congelado_no_toca_la_base(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10)])
        consultas = len(base.consultas)
        procesar(procesador, [lectura(10)])

        assert len(base.consultas) == consultas, "no debe haber ido a la base"
        assert base[101].produced == 10

    def test_llena_una_orden_y_sigue_con_la_siguiente(self, procesador, base):
        base.registros[101] = orden(101, "A", planned=20, creado="2026-08-19 01:00:00")
        base.registros[102] = orden(102, "B", planned=500, creado="2026-08-19 02:00:00")
        procesar(procesador, [lectura(50)])

        assert base[101].produced == 20 and base[101].status == ord_sql.COMPLETADO
        assert base[102].produced == 30
        assert [h["shop_order_number"] for h in base.histories] == ["A", "B"]


class TestElTurnoYaNoParteRegistros:
    """
    Antes, cruzar el cambio de turno cerraba el registro y abría otro. Ahora la
    corrida sigue llenando la MISMA orden.
    """

    def test_la_corrida_cruza_el_cambio_de_turno_sin_cortarse(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10)], ts=MANANA)
        procesar(procesador, [lectura(40)], ts=NOCHE)

        assert base[101].produced == 40
        assert base[101].status == ord_sql.EN_PROGRESO
        assert len(base.registros) == 1, "no debe aparecer un registro por turno"


class TestResetDeContador:
    def test_detiene_la_orden_y_registra_la_corrida_nueva(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(50)])
        procesar(procesador, [lectura(8)])          # el contador volvió a empezar

        assert base[101].produced == 58, "lo ya registrado queda intacto"
        assert base.conteo_en_histories() == 58

    def test_lo_registrado_antes_del_reset_no_se_borra(self, procesador, base):
        """El bug original: un reset 20 → 0 pisaba el acumulado de la base."""
        base.registros[101] = orden(101, "4051130", planned=900, produced=186,
                                    status=ord_sql.EN_PROGRESO)
        procesar(procesador, [lectura(20)])         # línea base: no suma
        procesar(procesador, [lectura(0)])          # reset sin producción nueva

        assert base[101].produced == 186


class TestFinDeCorrida:
    def test_la_parte_que_desaparece_deja_la_orden_detenida(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10)])
        procesar(procesador, [])                    # el PLC ya no reporta partes

        assert base[101].status == ord_sql.DETENIDO
        assert base[101].produced == 10

    def test_cambiar_de_parte_detiene_la_orden_anterior(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        base.partes.add((ESTACION, "OTRA-PARTE"))
        procesar(procesador, [lectura(10)])
        procesar(procesador, [lectura(3, parte="OTRA-PARTE")])

        assert base[101].status == ord_sql.DETENIDO

    def test_una_orden_detenida_se_retoma_donde_quedo(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10)])
        procesar(procesador, [])
        procesar(procesador, [lectura(6)])

        assert base[101].produced == 16
        assert base[101].status == ord_sql.EN_PROGRESO

    def test_el_plan_no_se_toca_al_terminar_la_corrida(self, procesador, base):
        """Los registros Pendientes son el programa de producción: se respetan."""
        base.registros[101] = orden(101, "A", planned=20, creado="2026-08-19 01:00:00")
        base.registros[102] = orden(102, "B", planned=500, creado="2026-08-19 02:00:00")
        procesar(procesador, [lectura(5)])
        procesar(procesador, [])

        assert base[102].status == ord_sql.PENDIENTE


class TestLecturaParcial:
    """
    Una lectura incompleta del PLC NO es prueba de que la estación se detuvo.
    Tratarla como tal borra la línea base y pierde lo producido en el hueco.
    """

    def test_no_detiene_ordenes_ante_una_lectura_fallida(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10)])
        procesar(procesador, [], plc_ok=False)

        assert base[101].status == ord_sql.EN_PROGRESO

    def test_conserva_la_linea_base_para_recuperar_el_hueco(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10)])
        procesar(procesador, [], plc_ok=False)      # el PLC no respondió
        procesar(procesador, [lectura(31)])         # vuelve con 21 piezas acumuladas

        assert base[101].produced == 31, "las 21 piezas del hueco se recuperan"

    def test_no_cierra_partes_ausentes_en_una_lectura_parcial(self, procesador, base):
        """Falló el bloque de un lado; el otro sigue reportando."""
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10, lado="LH"), lectura(5, lado="RH")])
        procesar(procesador, [lectura(12, lado="LH")], plc_ok=False)

        assert f"{ESTACION}_{PARTE}_RH" in procesador.active_records


class TestDosLados:
    def test_los_dos_lados_llenan_la_misma_orden(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10, lado="LH"), lectura(7, lado="RH")])

        assert base[101].produced == 17
        assert len(base.histories) == 2

    def test_cada_lado_lleva_su_propia_linea_base(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(10, lado="LH"), lectura(7, lado="RH")])
        procesar(procesador, [lectura(14, lado="LH"), lectura(7, lado="RH")])

        assert base[101].produced == 21, "solo avanzó LH"

    def test_ninguna_orden_se_pasa_con_los_dos_lados(self, procesador, base):
        base.registros[101] = orden(101, "A", planned=15)
        procesar(procesador, [lectura(10, lado="LH"), lectura(10, lado="RH")])

        assert base[101].produced == 15
        assert base.acumuladores[0].produced == 5


class TestSinOrden:
    def test_produccion_sin_orden_va_al_registro_no_planeado(self, procesador, base):
        procesar(procesador, [lectura(9)])

        assert len(base.acumuladores) == 1
        assert base.acumuladores[0].produced == 9
        assert "shop_order_number" not in base.histories[0], \
            "sin orden, la columna no se escribe en vez de escribirse en NULL"

    def test_el_estado_queda_produciendo_no_como_falla(self, procesador, base):
        procesar(procesador, [lectura(9)])
        estados = Prensas.REGISTRO.snapshot(estacion=ESTACION)
        assert estados[0]["motivo"] == "PRODUCIENDO"


class TestNadaSePierdeEnElCableado:
    def test_la_suma_de_histories_iguala_lo_que_conto_el_plc(self, procesador, base):
        base.registros[101] = orden(101, "A", planned=12, creado="2026-08-19 01:00:00")
        base.registros[102] = orden(102, "B", planned=9, creado="2026-08-19 02:00:00")

        for cnt in (5, 11, 18, 30, 30, 44):
            procesar(procesador, [lectura(cnt)])

        assert base.total_producido() == 44
        assert sum(h["quantity"] for h in base.histories) == 44

    def test_tampoco_se_pierde_con_resets_de_por_medio(self, procesador, base):
        base.registros[101] = orden(101, "A", planned=1000)

        total = 0
        for cnt in (10, 25, 4, 19, 2, 40):      # dos resets
            procesar(procesador, [lectura(cnt)])
        for tramo in (10, 15, 4, 15, 2, 38):
            total += tramo

        assert base[101].produced == total == 84


class TestRastroDeEstados:
    """
    Los cambios de estado importan aunque se reviertan enseguida: son lo que
    permite ver en la base que una corrida se cortó.
    """

    def test_el_reset_marca_detenida_la_orden_antes_de_reanudar(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(50)])
        procesar(procesador, [lectura(8)])

        assert (101, ord_sql.EN_PROGRESO, ord_sql.DETENIDO) in base.transiciones, \
            "el reset es un fin de corrida: la orden debe pasar por Detenida"
        assert base[101].status == ord_sql.EN_PROGRESO, \
            "y se reanuda de inmediato porque la producción continúa"
        assert base[101].produced == 58

    def test_la_orden_pendiente_arranca_en_progreso_con_la_primera_pieza(self, procesador, base):
        base.registros[101] = orden(101, "4051130", planned=900)
        procesar(procesador, [lectura(3)])

        assert base.transiciones == [(101, ord_sql.PENDIENTE, ord_sql.EN_PROGRESO)]

    def test_al_llenarse_pasa_a_completada_y_ahi_se_queda(self, procesador, base):
        base.registros[101] = orden(101, "A", planned=10, creado="2026-08-19 01:00:00")
        base.registros[102] = orden(102, "B", planned=500, creado="2026-08-19 02:00:00")
        procesar(procesador, [lectura(30)])
        procesar(procesador, [lectura(45)])

        de_la_101 = [t for t in base.transiciones if t[0] == 101]
        assert de_la_101 == [(101, ord_sql.PENDIENTE, ord_sql.EN_PROGRESO),
                             (101, ord_sql.EN_PROGRESO, ord_sql.COMPLETADO)]


class TestCuandoNoHayDondeRegistrar:
    """
    Si la parte ni siquiera existe en el catálogo no hay a dónde mandar la
    producción. Lo importante es no fingir que se registró.
    """

    def test_no_inventa_registros(self, procesador, base):
        procesar(procesador, [lectura(10, parte="FANTASMA")])
        assert base.registros == {}

    def test_no_mueve_la_linea_base_para_poder_reintentar(self, procesador, base):
        """
        Mover la línea base daría por buena una producción que nunca se guardó.
        Al darse de alta la parte, el siguiente ciclo recupera el total.
        """
        procesar(procesador, [lectura(10, parte="FANTASMA")])
        base.partes.add((ESTACION, "FANTASMA"))
        procesar(procesador, [lectura(12, parte="FANTASMA")])

        assert base.acumuladores[0].produced == 12, \
            "el contador es absoluto: al reintentar entra el total, no solo el último tramo"

    def test_lo_reporta_en_el_tablero(self, procesador, base):
        procesar(procesador, [lectura(10, parte="FANTASMA")])
        estados = Prensas.REGISTRO.snapshot(estacion=ESTACION)
        assert estados[0]["motivo"] == "PARTE_NO_EXISTE"
        assert estados[0]["requiere_atencion"] is True


class TestPrensaParada:
    def test_al_retomar_una_corrida_sin_avance_reporta_contador_detenido(self, procesador, base):
        """
        Primera lectura tras arrancar el servicio con la corrida ya en curso:
        la línea base es el contador actual, así que todavía no hay avance. La
        estación no puede desaparecer del tablero justo cuando está parada.
        """
        base.registros[101] = orden(101, "A", planned=900, produced=100,
                                    status=ord_sql.EN_PROGRESO)
        procesar(procesador, [lectura(100)])

        estados = Prensas.REGISTRO.snapshot(estacion=ESTACION)
        assert len(estados) == 1
        assert estados[0]["motivo"] == "CONTADOR_DETENIDO"
        assert estados[0]["requiere_atencion"] is False

    def test_el_contador_congelado_tambien_se_reporta(self, procesador, base):
        base.registros[101] = orden(101, "A", planned=900)
        procesar(procesador, [lectura(10)])
        procesar(procesador, [lectura(10)])

        estados = Prensas.REGISTRO.snapshot(estacion=ESTACION)
        assert estados[0]["motivo"] == "CONTADOR_DETENIDO"


# ─────────────────────────────── Estampado ────────────────────────────────
#
# La única área donde el contador cuenta GOLPES y no piezas. Aquí sí hay
# multiplicador, y por tanto las dos tablas llevan unidades distintas:
#     histories          <- golpes
#     production_records <- golpes x pieces_per_shot = piezas

PRENSA = "2500T  TR"
TROQUEL = "BDTS28BFC-P"


@pytest.fixture
def estampado(tmp_path, monkeypatch):
    """Un procesador de Estampado con 4 piezas por golpe."""
    base = BaseFalsa([], partes=[(PRENSA, TROQUEL)],
                     multiplicadores={(PRENSA, TROQUEL): 4})
    monkeypatch.setattr(Prensas, "STATE_DIR", tmp_path)
    monkeypatch.setattr(Prensas, "create_connection", lambda: ConexionFalsa(base))
    monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: LOG)
    monkeypatch.setattr(Prensas, "registrar_error_validacion", lambda *a, **k: None)
    Prensas.REGISTRO.olvidar_estacion(PRENSA)
    return Prensas.IPDataProcessor("10.1.1.1"), base


def golpear(procesador_y_base, contador, troquel_id=77, ts=MANANA, plc_ok=True):
    procesador, _ = procesador_y_base
    return procesador._process_estacion({
        "estacion": PRENSA, "area": "Estampado", "plc_ok": plc_ok, "ts": ts,
        "datos": [{"parte": TROQUEL, "original": TROQUEL, "contador": contador,
                   "tiempo": 2.4, "lado": "LH", "validado": True,
                   "troquel_id": troquel_id}],
    })


def orden_prensa(id, num, planned, produced=0, status=ord_sql.PENDIENTE,
                 creado="2026-08-19 00:00:00"):
    return Registro(id, PRENSA, TROQUEL, orden=num, planned=planned,
                    produced=produced, status=status,
                    planned_date="2026-08-19", created_at=creado)


class TestEstampado:
    def test_las_dos_tablas_llevan_unidades_distintas(self, estampado):
        """5 golpes con 4 piezas por golpe: histories 5, production_records 20."""
        _, base = estampado
        base.registros[101] = orden_prensa(101, "4051130", planned=900)
        golpear(estampado, 5)

        assert base.histories[0]["quantity"] == 5, "histories lleva GOLPES"
        assert base[101].produced == 20, "production_records lleva PIEZAS"

    def test_el_troquel_viaja_en_sequence(self, estampado):
        _, base = estampado
        base.registros[101] = orden_prensa(101, "4051130", planned=900)
        golpear(estampado, 3, troquel_id=77)

        assert base.histories[0]["sequence"] == 77
        assert base.histories[0]["shop_order_number"] == "4051130"

    def test_llena_la_orden_contando_piezas_no_golpes(self, estampado):
        """
        A la orden le faltan 60 piezas. Con 4 piezas por golpe, 15 golpes la
        llenan: los 5 golpes restantes de los 20 abren la siguiente.
        """
        _, base = estampado
        base.registros[101] = orden_prensa(101, "4051130", planned=900, produced=840,
                                           creado="2026-08-19 01:00:00")
        base.registros[102] = orden_prensa(102, "4051131", planned=1000,
                                           creado="2026-08-19 02:00:00")
        golpear(estampado, 20)

        assert base[101].produced == 900 and base[101].status == ord_sql.COMPLETADO
        assert base[102].produced == 20
        assert [h["quantity"] for h in base.histories] == [15, 5], "en GOLPES"

    def test_el_golpe_no_se_parte(self, estampado):
        """
        A la orden le falta 1 pieza y de un golpe salen 4. El golpe entero se
        atribuye a esa orden en histories; las 3 piezas sobrantes pasan a la
        siguiente SIN generar otra fila.
        """
        _, base = estampado
        base.registros[101] = orden_prensa(101, "A", planned=1,
                                           creado="2026-08-19 01:00:00")
        base.registros[102] = orden_prensa(102, "B", planned=500,
                                           creado="2026-08-19 02:00:00")
        golpear(estampado, 1)

        assert base[101].produced == 1 and base[101].status == ord_sql.COMPLETADO
        assert base[102].produced == 3
        assert len(base.histories) == 1, "un golpe, una fila"
        assert base.histories[0]["shop_order_number"] == "A"

    def test_corregir_pieces_per_shot_se_aplica_sin_reiniciar(self, estampado):
        """
        El multiplicador se relee cada ciclo: si estaba mal capturado y se
        corrige en la base, la producción siguiente ya usa el valor bueno.
        """
        _, base = estampado
        base.registros[101] = orden_prensa(101, "A", planned=5000)
        golpear(estampado, 10)
        assert base[101].produced == 40           # 10 golpes x 4

        base.multiplicadores[(PRENSA, TROQUEL)] = 6
        golpear(estampado, 15)
        assert base[101].produced == 40 + 30      # 5 golpes x 6, sin recalcular lo viejo

    def test_sin_atributo_capturado_cuenta_una_pieza_por_golpe(self, estampado):
        """Falta pieces_per_shot: se asume 1 en vez de tirar la producción."""
        _, base = estampado
        base.multiplicadores.clear()
        base.registros[101] = orden_prensa(101, "A", planned=900)
        golpear(estampado, 7)

        assert base[101].produced == 7
        assert base.histories[0]["quantity"] == 7

    def test_nada_se_pierde_con_multiplicador(self, estampado):
        _, base = estampado
        base.registros[101] = orden_prensa(101, "A", planned=30,
                                           creado="2026-08-19 01:00:00")
        base.registros[102] = orden_prensa(102, "B", planned=45,
                                           creado="2026-08-19 02:00:00")
        golpear(estampado, 25)                    # 25 golpes x 4 = 100 piezas

        assert base.total_producido() == 100
        assert base.conteo_en_histories() == 25


class TestCostoEnLaBase:
    """
    Con 109 estaciones creciendo al triple, cada consulta por incremento se
    multiplica por cientos. Lo que se puede resolver una vez, se resuelve una vez.
    """

    def test_el_id_de_la_parte_se_resuelve_una_sola_vez(self, procesador, base):
        base.registros[101] = orden(101, "A", planned=900)
        procesar(procesador, [lectura(10)])
        procesar(procesador, [lectura(20)])
        procesar(procesador, [lectura(30)])

        consultas_pid = [c for c in base.consultas if "SELECT pn.id FROM part_numbers" in c]
        assert len(consultas_pid) == 1, "el id de la parte no cambia durante la corrida"
        assert base.conteo_en_histories() == 30, "y el historial sigue completo"

    def test_las_ordenes_si_se_consultan_cada_vez(self, procesador, base):
        """
        Esta NO se cachea a propósito: el hueco cambia con el otro lado, con
        Infor y con las órdenes que entren nuevas.
        """
        base.registros[101] = orden(101, "A", planned=900)
        procesar(procesador, [lectura(10)])
        procesar(procesador, [lectura(20)])

        consultas_cap = [c for c in base.consultas if "AS falta" in c]
        assert len(consultas_cap) == 2

    def test_el_contador_congelado_no_consulta_nada(self, procesador, base):
        base.registros[101] = orden(101, "A", planned=900)
        procesar(procesador, [lectura(10)])
        antes = len(base.consultas)
        for _ in range(5):
            procesar(procesador, [lectura(10)])

        assert len(base.consultas) == antes, "cinco ciclos sin producir, cero consultas"


# ──────────────────────── fecha límite de órdenes ─────────────────────────

class TestFechaLimiteDeOrdenes:
    """
    Hasta dónde hacia atrás se rescatan órdenes pendientes. Es una FECHA FIJA,
    no una ventana móvil: el corte no debe recorrerse solo con el paso de los días.
    """

    def test_lee_la_fecha_del_entorno(self, monkeypatch):
        monkeypatch.setenv("ORDENES_DESDE", "2026-07-03")
        assert Prensas._fecha_limite_ordenes() == datetime(2026, 7, 3).date()

    def test_es_fija_no_una_ventana_movil(self, monkeypatch):
        """
        El mismo valor de entorno da el mismo corte siempre. Con la ventana de
        días anterior, el corte se recorría solo y un día dejaba de alcanzar
        las órdenes que ayer sí alcanzaba.
        """
        monkeypatch.setenv("ORDENES_DESDE", "2026-07-03")
        assert Prensas._fecha_limite_ordenes() == Prensas._fecha_limite_ordenes()

    @pytest.mark.parametrize("valor", ["", "   ", "03/07/2026", "2026-13-99", "ayer"])
    def test_si_falta_o_viene_mal_usa_un_corte_permisivo(self, monkeypatch, valor):
        """
        Un corte demasiado reciente no dejaría alcanzar NINGUNA orden y toda la
        producción se iría a No planeado sin que nadie se entere. Rescatar
        órdenes viejas se ve y se corrige; perder el enlace con el plan, no.
        """
        monkeypatch.setenv("ORDENES_DESDE", valor)
        assert Prensas._fecha_limite_ordenes() == Prensas.ORDENES_DESDE_DEFECTO
        assert Prensas.ORDENES_DESDE_DEFECTO.year < 2020

    def test_sin_la_variable_tampoco_truena(self, monkeypatch):
        monkeypatch.delenv("ORDENES_DESDE", raising=False)
        assert Prensas._fecha_limite_ordenes() == Prensas.ORDENES_DESDE_DEFECTO

    def test_el_recolector_usa_esa_fecha_al_buscar_ordenes(self, procesador, base,
                                                           monkeypatch):
        """La fecha de corte llega tal cual a la consulta de capacidades."""
        monkeypatch.setattr(Prensas, "ORDENES_DESDE", datetime(2026, 7, 3).date())
        base.registros[101] = orden(101, "A", planned=900, fecha="2026-07-04")
        base.registros[102] = orden(102, "VIEJA", planned=900, fecha="2026-07-02")
        procesar(procesador, [lectura(10)])

        assert base[101].produced == 10, "la del 04-jul entra"
        assert base[102].produced == 0, "la del 02-jul queda fuera del corte"
