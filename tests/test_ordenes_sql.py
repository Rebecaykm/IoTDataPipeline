"""
Contrato de la capa de órdenes contra la base.

Lo que se fija aquí es la REGLA escrita en SQL: a qué orden se le suma, cuándo
se cierra y cuándo NO se cierra. Son las decisiones que, si se rompen, pierden
producción de planta sin que nadie se entere hasta el corte del mes.
"""

import logging
from datetime import date, datetime

import pytest

from persistence import ordenes as ord_sql
from tests.dobles import CursorFalso

LOG = logging.getLogger("test")
AHORA = "2026-08-19 07:30:00"
HOY = date(2026, 8, 19)
MES_INICIO, MES_FIN = ord_sql.limites_mes(HOY)


class TestCapacidades:
    """La consulta de prioridad: dónde se registra lo que se acaba de producir."""

    def test_devuelve_las_ordenes_en_el_orden_de_la_consulta(self):
        cur = CursorFalso([[(101, "4051130", 60), (102, "4051131", 900)]])
        caps = ord_sql.capacidades(cur, "MK05", "BDTS53411-BK", HOY)

        assert [c.id_registro for c in caps] == [101, 102]
        assert [c.orden for c in caps] == ["4051130", "4051131"]
        assert [c.falta for c in caps] == [60, 900]

    def test_sin_ordenes_devuelve_lista_vacia(self):
        """No es un error: significa que la producción es sobrante (acumulador)."""
        assert ord_sql.capacidades(CursorFalso([[]]), "MK05", "X", HOY) == []

    def test_pasa_estacion_parte_y_limites_del_mes(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "BDTS53411-BK", HOY)
        assert cur.params == ("MK05", "BDTS53411-BK", MES_INICIO, MES_FIN)

    def test_la_orden_en_progreso_va_primero(self):
        """Retomar lo que se estaba llenando manda sobre la antigüedad."""
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", HOY)
        assert "ORDER BY CASE WHEN pr.status_id = 7 THEN 0 ELSE 1 END" in cur.sql

    def test_luego_de_la_mas_antigua_a_la_mas_nueva(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", HOY)
        assert "pr.planned_date, pr.shift_id, pr.created_at" in cur.sql

    def test_descarta_las_ordenes_ya_llenas(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", HOY)
        assert "pr.produced_quantity < pr.planned_quantity" in cur.sql

    def test_las_pendientes_exigen_numero_de_orden(self):
        """Sin shop_order_number no es una orden: es otra cosa."""
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", HOY)
        assert "pr.shop_order_number IS NOT NULL" in cur.sql

    def test_el_mismo_filtro_de_mes_aplica_a_la_orden_en_progreso(self):
        """
        El filtro de mes es parejo para TODA la consulta, incluida la orden
        en progreso: si queda en un mes distinto, el recolector simplemente
        deja de verla -no la cierra ni la clasifica, eso es responsabilidad
        del proceso externo que cierra el mes.
        """
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", HOY)
        assert "pr.planned_date >= ? AND pr.planned_date < ?" in cur.sql
        # Y esa condición no está anidada dentro de ninguna rama del OR:
        # aplica antes de distinguir status 7 de status 3/8.
        recorte = cur.sql[:cur.sql.index("AND ( pr.status_id")]
        assert "pr.planned_date >= ? AND pr.planned_date < ?" in recorte

    def test_no_depende_del_sync_from_de_la_estacion(self):
        """
        El filtrado de órdenes es una sola regla sin excepción por régimen:
        sync_from no participa aquí, solo decide el flavor del sobrante.
        """
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", HOY)
        assert "infor_sync_work_centers" not in cur.sql

    def test_ignora_lo_ya_enviado_a_infor(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", HOY)
        assert "ISNULL(pr.synced_to_infor, 0) <> 1" in cur.sql

    def test_compara_el_numero_de_parte_sin_espacios(self):
        """'BDTS28BFC -P' y 'BDTS28BFC-P' son la misma parte en la base."""
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", HOY)
        assert "REPLACE(pn.number, ' ', '') = ?" in cur.sql

    def test_acota_cuantas_ordenes_trae(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", HOY)
        assert f"SELECT TOP({ord_sql.MAX_ORDENES})" in cur.sql


class TestLimitesMes:
    """[inicio, fin) del mes que contiene fecha_plan. Nunca se cruza de mes."""

    def test_mes_normal(self):
        assert ord_sql.limites_mes(date(2026, 8, 19)) == (date(2026, 8, 1), date(2026, 9, 1))

    def test_primer_dia_del_mes(self):
        assert ord_sql.limites_mes(date(2026, 8, 1)) == (date(2026, 8, 1), date(2026, 9, 1))

    def test_ultimo_dia_del_mes(self):
        assert ord_sql.limites_mes(date(2026, 8, 31)) == (date(2026, 8, 1), date(2026, 9, 1))

    def test_rollover_de_diciembre_a_enero(self):
        assert ord_sql.limites_mes(date(2026, 12, 15)) == (date(2026, 12, 1), date(2027, 1, 1))

    def test_febrero_de_anio_bisiesto(self):
        """2028 es bisiesto: el fin de febrero cae en el 1 de marzo sin caso especial."""
        assert ord_sql.limites_mes(date(2028, 2, 29)) == (date(2028, 2, 1), date(2028, 3, 1))


class TestObtenerSyncFrom:
    """El corte por estación, leído fresco cada vez (nunca se cachea)."""

    def test_sin_fila_devuelve_none(self):
        cur = CursorFalso([[]])
        assert ord_sql.obtener_sync_from(cur, "MK05") is None

    def test_con_fila_devuelve_la_fecha(self):
        cur = CursorFalso([[(date(2026, 9, 21),)]])
        assert ord_sql.obtener_sync_from(cur, "MK05") == date(2026, 9, 21)

    def test_convierte_datetime_a_date(self):
        cur = CursorFalso([[(datetime(2026, 9, 21, 0, 0, 0),)]])
        assert ord_sql.obtener_sync_from(cur, "MK05") == date(2026, 9, 21)

    def test_consulta_por_nombre_de_estacion(self):
        cur = CursorFalso([[]])
        ord_sql.obtener_sync_from(cur, "MK05")
        assert cur.params == ("MK05",)
        assert "infor_sync_work_centers" in cur.sql


class TestSumarAOrden:
    """El tope lo pone SQL Server dentro del UPDATE, no Python."""

    def test_suma_completa_cuando_cabe(self):
        cur = CursorFalso([[(100, 130, 500)]])       # antes, después, planeada
        aplicadas, lleno = ord_sql.sumar_a_orden(cur, 101, 30, AHORA)
        assert (aplicadas, lleno) == (30, False)

    def test_avisa_cuando_la_orden_queda_llena(self):
        cur = CursorFalso([[(840, 900, 900)]])
        aplicadas, lleno = ord_sql.sumar_a_orden(cur, 101, 60, AHORA)
        assert (aplicadas, lleno) == (60, True)

    def test_el_tope_recorta_y_lo_reporta(self):
        """
        El otro lado ya llenó la orden entre la consulta y el UPDATE: solo
        entraron 40 de las 60 que traíamos. Las 20 restantes NO se pierden,
        se las devolvemos al llamador.
        """
        cur = CursorFalso([[(860, 900, 900)]])
        aplicadas, lleno = ord_sql.sumar_a_orden(cur, 101, 60, AHORA)
        assert (aplicadas, lleno) == (40, True)

    def test_orden_que_ya_no_admite_devuelve_cero(self):
        """La cerraron entre la consulta y el UPDATE: el WHERE no la alcanza."""
        cur = CursorFalso([[]])
        assert ord_sql.sumar_a_orden(cur, 101, 60, AHORA) == (0, False)

    def test_el_recorte_esta_en_el_sql(self):
        cur = CursorFalso([[(0, 10, 100)]])
        ord_sql.sumar_a_orden(cur, 101, 10, AHORA)
        assert "CASE WHEN produced_quantity + ? > planned_quantity THEN planned_quantity" in cur.sql

    def test_suma_nunca_asigna_un_absoluto(self):
        """Un reset de contador jamás debe poder borrar lo ya registrado."""
        cur = CursorFalso([[(0, 10, 100)]])
        ord_sql.sumar_a_orden(cur, 101, 10, AHORA)
        assert "produced_quantity + ?" in cur.sql

    def test_deja_la_orden_en_progreso(self):
        cur = CursorFalso([[(0, 10, 100)]])
        ord_sql.sumar_a_orden(cur, 101, 10, AHORA)
        assert "status_id = 7" in cur.sql

    def test_no_pisa_el_inicio_de_produccion_ya_registrado(self):
        cur = CursorFalso([[(0, 10, 100)]])
        ord_sql.sumar_a_orden(cur, 101, 10, AHORA)
        assert "production_start = ISNULL(production_start, ?)" in cur.sql

    def test_no_revive_una_orden_completada(self):
        cur = CursorFalso([[(0, 10, 100)]])
        ord_sql.sumar_a_orden(cur, 101, 10, AHORA)
        assert "status_id IN (3, 7, 8)" in cur.sql


class TestSumarANoPlaneado:
    """El acumulador no tiene plan, así que no tiene tope."""

    def test_suma_sin_recortar(self):
        cur = CursorFalso([[(70,)]])
        assert ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY) == 70
        assert "planned_quantity" not in cur.sql, "no hay plan contra el cual recortar"

    def test_solo_toca_el_acumulador_sin_orden_de_su_regimen(self):
        cur = CursorFalso([[(7,)]])
        ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY, ord_sql.NO_PLANEADO)
        assert "shop_order_number IS NULL" in cur.sql
        assert "status_id IN (7, ?)" in cur.sql
        assert cur.params[-1] == ord_sql.NO_PLANEADO

    def test_funciona_igual_para_el_regimen_no_verificado(self):
        cur = CursorFalso([[(7,)]])
        ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY, ord_sql.NO_VERIFICADO)
        assert cur.params[-1] == ord_sql.NO_VERIFICADO

    def test_lo_deja_en_progreso_mientras_la_corrida_vive(self):
        """Solo cierra -a 24 o a 14- cuando la corrida termina (ver detener())."""
        cur = CursorFalso([[(7,)]])
        ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY)
        assert "status_id = 7" in cur.sql

    def test_mueve_la_fecha_al_dia_que_se_produjo(self):
        """El acumulador marca en planned_date la última producción."""
        cur = CursorFalso([[(7,)]])
        ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY)
        assert "planned_date = ?" in cur.sql
        assert cur.params == (7, HOY, AHORA, AHORA, 500, ord_sql.NO_PLANEADO)

    def test_el_inicio_se_mueve_solo_al_reabrir(self):
        """
        Si no estaba ya corriendo (venía cerrado, en 24 O en 14), esta suma
        abre corrida nueva y el inicio es ahora. Si sigue vivo, conserva el
        inicio de la corrida en curso. Cubre los dos regímenes sin condicional
        en Python.
        """
        cur = CursorFalso([[(7,)]])
        ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY)
        assert ("production_start = CASE WHEN status_id <> 7 "
                "OR production_start IS NULL") in cur.sql


class TestCompletar:
    """Cerrar una orden es irreversible: más vale no cerrarla de más."""

    def test_exige_estar_llena(self):
        cur = CursorFalso([[]])
        ord_sql.completar(cur, 101, AHORA)
        assert "produced_quantity >= planned_quantity" in cur.sql

    def test_pasa_a_completado(self):
        cur = CursorFalso([[]])
        ord_sql.completar(cur, 101, AHORA)
        assert "status_id = 4" in cur.sql
        assert cur.params == (AHORA, 101)


class TestDetener:
    """
    Fin de corrida sin llenar la orden. El status final se decide con un CASE
    de tres ramas: con orden -> Detenido; sin orden, según el sync_from de la
    estación en ESE momento -> No planeado o No verificado.
    """

    def test_solo_desde_en_progreso(self):
        cur = CursorFalso([[]])
        ord_sql.detener(cur, 101, AHORA)
        assert "AND pr.status_id = 7" in cur.sql

    def test_con_orden_cierra_a_detenido(self):
        cur = CursorFalso([[]])
        ord_sql.detener(cur, 101, AHORA)
        assert "WHEN pr.shop_order_number IS NOT NULL THEN 8" in cur.sql

    def test_sin_orden_decide_segun_el_sync_from(self):
        """
        El LEFT JOIN a infor_sync_work_centers es lo que decide sin
        parámetros extra: sin fila (o con una fecha futura), la comparación
        `>=` nunca es verdadera y el CASE cae al 14.
        """
        cur = CursorFalso([[]])
        ord_sql.detener(cur, 101, AHORA)
        assert "LEFT JOIN infor_sync_work_centers isw" in cur.sql
        assert "WHEN pr.planned_date >= isw.sync_from THEN 24" in cur.sql
        assert "ELSE 14" in cur.sql

    def test_pasa_fecha_e_id(self):
        cur = CursorFalso([[]])
        ord_sql.detener(cur, 101, AHORA)
        assert cur.params == (AHORA, 101)

    def test_la_estacion_completa_tambien_solo_en_progreso(self):
        cur = CursorFalso([[]])
        ord_sql.detener_estacion(cur, "MK05", AHORA)
        assert "pr.status_id = 7" in cur.sql
        assert cur.params == (AHORA, "MK05")

    def test_detener_estacion_tambien_decide_segun_sync_from(self):
        cur = CursorFalso([[]])
        ord_sql.detener_estacion(cur, "MK05", AHORA)
        assert "WHEN pr.planned_date >= isw.sync_from THEN 24" in cur.sql
        assert "ELSE 14" in cur.sql

    def test_detener_estacion_no_filtra_por_turno_ni_fecha(self):
        """
        No acota POR CUÁLES filas toca: shift_id no aparece, y planned_date
        solo se usa para decidir a qué status cierra (dentro del CASE), no
        para filtrar el WHERE.
        """
        cur = CursorFalso([[]])
        ord_sql.detener_estacion(cur, "MK05", AHORA)
        assert "shift_id" not in cur.sql
        recorte_where = cur.sql[cur.sql.index("WHERE"):]
        assert "planned_date" not in recorte_where


class TestAcumuladorNoPlaneado:
    def test_reutiliza_el_que_ya_existe(self):
        cur = CursorFalso([[(777,)]])
        assert ord_sql.acumulador_no_planeado(
            cur, "MK05", "X", HOY, 1, AHORA, log=LOG) == (777, None)
        assert len(cur) == 1, "no debe insertar si ya existe"

    def test_lo_crea_cuando_no_hay(self):
        # 1) busca del mes actual -> nada. 2) busca vencido -> nada.
        # 3) resuelve el part_number_id -> 123. 4) crea.
        cur = CursorFalso([[], [], [(123,)], [(888,)]])
        assert ord_sql.acumulador_no_planeado(
            cur, "MK05", "X", HOY, 1, AHORA, log=LOG) == (888, None)
        assert "INSERT INTO production_records" in cur.sql
        assert cur.params == (123, 1, AHORA, AHORA, HOY)

    def test_resuelve_el_part_number_id_con_la_misma_funcion_que_histories(self):
        """
        Mismo punto único de resolución que usa Prensas.py para histories
        (persistence.repositorio.obtener_part_number_id): así no puede volver
        a pasar que production_records e histories terminen con un
        part_number_id distinto para la misma producción.
        """
        cur = CursorFalso([[], [], [(123,)], [(888,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", HOY, 1, AHORA, log=LOG)
        assert "pn.is_obsolete = 0" in cur.sql_de(2)
        assert cur.params_de(2) == ("X", "MK05")

    def test_nace_en_progreso_no_en_no_planeado(self):
        """
        Nace vivo, como cualquier corrida, sin importar el flavor. Sin esto
        quedaría ya cerrado si el INSERT prospera y la suma que viene detrás
        falla.
        """
        cur = CursorFalso([[], [], [(123,)], [(888,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", HOY, 1, AHORA, log=LOG)
        assert f", {ord_sql.EN_PROGRESO}, ?" in cur.sql
        assert f", {ord_sql.NO_PLANEADO}, ?" not in cur.sql

    def test_nace_en_cero_y_se_llena_con_los_deltas(self):
        """
        Si naciera con la producción dentro, un reintento la duplicaría. Nace
        vacío y todo entra por sumar_a_no_planeado.
        """
        cur = CursorFalso([[], [], [(123,)], [(888,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", HOY, 1, AHORA, log=LOG)
        assert "VALUES (?, 0, ?" in cur.sql

    def test_busca_uno_solo_por_parte_y_estacion(self):
        """
        Uno por parte+estación+mes: la producción sin plan de un número de
        parte se junta toda en el mismo renglón dentro de ese mes.
        """
        cur = CursorFalso([[(777,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", HOY, 1, AHORA, log=LOG)
        assert "pr.shop_order_number IS NULL" in cur.sql
        assert "pr.shift_id" not in cur.sql, "el turno no acota la búsqueda"

    def test_no_adopta_uno_de_un_mes_anterior(self):
        """
        El mismo límite de mes que acota las órdenes (ver capacidades()). La
        base arrastra registros sin orden de meses pasados; adoptar uno de
        esos mezclaría producción vieja con la de este mes en el mismo
        renglón.
        """
        cur = CursorFalso([[(777,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", HOY, 1, AHORA, log=LOG)
        assert "pr.planned_date >= ? AND pr.planned_date < ?" in cur.sql
        assert cur.params == ("MK05", "X", ord_sql.NO_PLANEADO, MES_INICIO, MES_FIN)

    def test_el_flavor_decide_que_status_cerrado_cuenta_como_mio(self):
        cur = CursorFalso([[(777,)]])
        ord_sql.acumulador_no_planeado(
            cur, "MK05", "X", HOY, 1, AHORA, flavor=ord_sql.NO_VERIFICADO, log=LOG)
        assert cur.params == ("MK05", "X", ord_sql.NO_VERIFICADO, MES_INICIO, MES_FIN)

    def test_si_hay_varios_toma_el_mas_reciente(self):
        cur = CursorFalso([[(777,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", HOY, 1, AHORA, log=LOG)
        assert "ORDER BY pr.id DESC" in cur.sql

    def test_parte_inexistente_devuelve_error_para_el_tablero(self):
        cur = CursorFalso([[], [], []])
        id_reg, error = ord_sql.acumulador_no_planeado(
            cur, "MK05", "FANTASMA", HOY, 1, AHORA, log=LOG)
        assert id_reg is None
        assert error == "PART_NUMBER_NO_EXISTE_BD"

    def test_error_de_base_no_tumba_el_ciclo(self):
        class CursorRoto(CursorFalso):
            def execute(self, sql, params=()):
                if "INSERT" in sql:
                    raise RuntimeError("deadlock")
                return super().execute(sql, params)

        # 1) mes actual -> nada. 2) vencido -> nada. 3) resuelve el
        # part_number_id -> 123 (para que SÍ se llegue a intentar el INSERT,
        # que es donde se simula el error de base).
        id_reg, error = ord_sql.acumulador_no_planeado(
            CursorRoto([[], [], [(123,)]]), "MK05", "X", HOY, 1, AHORA, log=LOG)
        assert (id_reg, error) == (None, "DB_ERROR")


class TestAcumuladorVencido:
    """
    Si la corrida nunca "soltó" la parte en el PLC al cruzar de mes, el
    acumulador del mes anterior se queda vivo en status 7. acumulador_no_planeado()
    lo encuentra y lo cierra con detener() antes de crear el del mes actual.
    """

    def test_lo_cierra_antes_de_crear_el_del_mes_nuevo(self):
        # 1) busca del mes actual -> nada. 2) busca vencido -> lo encuentra
        # (id 555). 3) detener() lo cierra (UPDATE). 4) resuelve el
        # part_number_id -> 123. 5) crea el del mes nuevo.
        cur = CursorFalso([[], [(555,)], [], [(123,)], [(888,)]])
        id_reg, error = ord_sql.acumulador_no_planeado(
            cur, "MK05", "X", HOY, 1, AHORA, log=LOG)
        assert (id_reg, error) == (888, None)
        assert "NOT (" in cur.sql_de(1)
        assert cur.params_de(2) == (AHORA, 555)   # el detener() sobre el vencido

    def test_sin_vencido_no_dispara_ningun_cierre_de_mas(self):
        cur = CursorFalso([[], [], [(123,)], [(888,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", HOY, 1, AHORA, log=LOG)
        assert len(cur) == 4, "sin vencido no hay UPDATE de cierre de más"


class TestEstadosCoinciden:
    """Los números de estado son los de la base, no invenciones."""

    @pytest.mark.parametrize("nombre,valor", [
        ("PENDIENTE", 3), ("COMPLETADO", 4), ("EN_PROGRESO", 7),
        ("DETENIDO", 8), ("NO_PLANEADO", 24), ("NO_VERIFICADO", 14),
    ])
    def test_constantes(self, nombre, valor):
        assert getattr(ord_sql, nombre) == valor
