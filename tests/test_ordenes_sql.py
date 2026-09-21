"""
Contrato de la capa de órdenes contra la base.

Lo que se fija aquí es la REGLA escrita en SQL: a qué orden se le suma, cuándo
se cierra y cuándo NO se cierra. Son las decisiones que, si se rompen, pierden
producción de planta sin que nadie se entere hasta el corte del mes.
"""

import logging

import pytest

from persistence import ordenes as ord_sql
from tests.dobles import CursorFalso

LOG = logging.getLogger("test")
AHORA = "2026-08-19 07:30:00"
HOY = "2026-08-19"
DESDE = "2026-08-12"


class TestCapacidades:
    """La consulta de prioridad: dónde se registra lo que se acaba de producir."""

    def test_devuelve_las_ordenes_en_el_orden_de_la_consulta(self):
        cur = CursorFalso([[(101, "4051130", 60), (102, "4051131", 900)]])
        caps = ord_sql.capacidades(cur, "MK05", "BDTS53411-BK", DESDE)

        assert [c.id_registro for c in caps] == [101, 102]
        assert [c.orden for c in caps] == ["4051130", "4051131"]
        assert [c.falta for c in caps] == [60, 900]

    def test_sin_ordenes_devuelve_lista_vacia(self):
        """No es un error: significa que la producción es No planeado."""
        assert ord_sql.capacidades(CursorFalso([[]]), "MK05", "X", DESDE) == []

    def test_pasa_estacion_parte_y_fecha_de_corte(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "BDTS53411-BK", DESDE)
        assert cur.params == ("MK05", "BDTS53411-BK", DESDE)

    def test_la_orden_en_progreso_va_primero(self):
        """Retomar lo que se estaba llenando manda sobre la antigüedad."""
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", DESDE)
        assert "ORDER BY CASE WHEN pr.status_id = 7 THEN 0 ELSE 1 END" in cur.sql

    def test_luego_de_la_mas_antigua_a_la_mas_nueva(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", DESDE)
        assert "pr.planned_date, pr.shift_id, pr.created_at" in cur.sql

    def test_descarta_las_ordenes_ya_llenas(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", DESDE)
        assert "pr.produced_quantity < pr.planned_quantity" in cur.sql

    def test_las_pendientes_exigen_numero_de_orden(self):
        """Sin shop_order_number no es una orden: es otra cosa."""
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", DESDE)
        assert "pr.shop_order_number IS NOT NULL" in cur.sql

    def test_la_fecha_de_corte_no_aplica_a_la_orden_en_progreso(self):
        """
        El corte por fecha vive DENTRO del OR de status 3/8. Si aplicara a la
        orden en progreso, una corrida larga quedaría huérfana a medio llenar.
        """
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", DESDE)
        recorte = cur.sql[cur.sql.index("pr.status_id = 7"):cur.sql.index("ORDER BY")]
        assert "pr.planned_date >= ?" in recorte
        assert "IN (3, 8)" in recorte

    def test_ignora_lo_ya_enviado_a_infor(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", DESDE)
        assert "ISNULL(pr.synced_to_infor, 0) <> 1" in cur.sql

    def test_compara_el_numero_de_parte_sin_espacios(self):
        """'BDTS28BFC -P' y 'BDTS28BFC-P' son la misma parte en la base."""
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", DESDE)
        assert "REPLACE(pn.number, ' ', '') = ?" in cur.sql

    def test_acota_cuantas_ordenes_trae(self):
        cur = CursorFalso([[]])
        ord_sql.capacidades(cur, "MK05", "X", DESDE)
        assert f"SELECT TOP({ord_sql.MAX_ORDENES})" in cur.sql


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

    def test_solo_toca_el_acumulador_sin_orden(self):
        cur = CursorFalso([[(7,)]])
        ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY)
        assert "shop_order_number IS NULL" in cur.sql
        assert "status_id IN (7, 24)" in cur.sql

    def test_lo_deja_en_progreso_mientras_la_corrida_vive(self):
        """Solo pasa a No planeado cuando la corrida termina."""
        cur = CursorFalso([[(7,)]])
        ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY)
        assert "status_id = 7" in cur.sql

    def test_mueve_la_fecha_al_dia_que_se_produjo(self):
        """El acumulador es uno solo: su fecha marca la última producción."""
        cur = CursorFalso([[(7,)]])
        ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY)
        assert "planned_date = ?" in cur.sql
        assert cur.params == (7, HOY, AHORA, AHORA, 500)

    def test_el_inicio_se_mueve_solo_al_reabrir(self):
        """
        Si venía cerrado, esta suma abre corrida nueva y el inicio es ahora.
        Si sigue vivo, conserva el inicio de la corrida en curso.
        """
        cur = CursorFalso([[(7,)]])
        ord_sql.sumar_a_no_planeado(cur, 500, 7, AHORA, HOY)
        assert ("production_start = CASE WHEN status_id = 24 "
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
    """Fin de corrida sin llenar la orden."""

    def test_solo_desde_en_progreso(self):
        """
        Una orden Pendiente que nunca se empezó sigue siendo plan. Cerrarla
        destruiría la programación: ya nos pasó una vez.
        """
        cur = CursorFalso([[]])
        ord_sql.detener(cur, 101, AHORA)
        assert "AND status_id = 7" in cur.sql

    def test_el_estado_final_lo_decide_el_registro(self):
        """Con orden → Detenido. Sin orden → No planeado. Lo resuelve el SQL."""
        cur = CursorFalso([[]])
        ord_sql.detener(cur, 101, AHORA)
        assert ("status_id = CASE WHEN shop_order_number IS NULL "
                "THEN 24 ELSE 8 END") in cur.sql

    def test_la_estacion_completa_tambien_solo_en_progreso(self):
        cur = CursorFalso([[]])
        ord_sql.detener_estacion(cur, "MK05", AHORA)
        assert "pr.status_id = 7" in cur.sql
        assert cur.params == (AHORA, "MK05")

    def test_detener_estacion_no_filtra_por_turno_ni_fecha(self):
        """Ya no se registra por turno: una corrida cruza turnos sin cortarse."""
        cur = CursorFalso([[]])
        ord_sql.detener_estacion(cur, "MK05", AHORA)
        assert "shift_id" not in cur.sql
        assert "planned_date" not in cur.sql


class TestAcumuladorNoPlaneado:
    def test_reutiliza_el_que_ya_existe(self):
        cur = CursorFalso([[(777,)]])
        assert ord_sql.acumulador_no_planeado(
            cur, "MK05", "X", DESDE, HOY, 1, AHORA, LOG) == (777, None)
        assert len(cur) == 1, "no debe insertar si ya existe"

    def test_lo_crea_cuando_no_hay(self):
        cur = CursorFalso([[], [(888,)]])
        assert ord_sql.acumulador_no_planeado(
            cur, "MK05", "X", DESDE, HOY, 1, AHORA, LOG) == (888, None)
        assert "INSERT INTO production_records" in cur.sql

    def test_nace_en_progreso_no_en_no_planeado(self):
        """
        Nace vivo, como cualquier corrida. Sin esto quedaría en No planeado si
        el INSERT prospera y la suma que viene detrás falla.
        """
        cur = CursorFalso([[], [(888,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", DESDE, HOY, 1, AHORA, LOG)
        assert f", {ord_sql.EN_PROGRESO}, ?" in cur.sql
        assert f", {ord_sql.NO_PLANEADO}, ?" not in cur.sql

    def test_nace_en_cero_y_se_llena_con_los_deltas(self):
        """
        Si naciera con la producción dentro, un reintento la duplicaría. Nace
        vacío y todo entra por sumar_a_no_planeado.
        """
        cur = CursorFalso([[], [(888,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", DESDE, HOY, 1, AHORA, LOG)
        assert "SELECT pn.id, 0, ?" in cur.sql

    def test_busca_uno_solo_por_parte_y_estacion(self):
        """
        Uno por parte: la producción sin plan de un número de parte se junta
        toda en el mismo renglón, sin importar el día en que cayó.
        """
        cur = CursorFalso([[(777,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", DESDE, HOY, 1, AHORA, LOG)
        assert "pr.shop_order_number IS NULL" in cur.sql
        assert "pr.shift_id" not in cur.sql, "el turno no acota la búsqueda"

    def test_no_mira_antes_de_la_fecha_de_corte(self):
        """
        El mismo corte que acota las órdenes (ORDENES_DESDE). La base arrastra
        cientos de registros sin orden de años anteriores: adoptar uno de esos
        mezclaría producción vieja con la de hoy en el mismo renglón.
        """
        cur = CursorFalso([[(777,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", DESDE, HOY, 1, AHORA, LOG)
        assert "pr.planned_date >= ?" in cur.sql
        assert cur.params == ("MK05", "X", DESDE)

    def test_si_hay_varios_dentro_del_corte_toma_el_mas_reciente(self):
        cur = CursorFalso([[(777,)]])
        ord_sql.acumulador_no_planeado(cur, "MK05", "X", DESDE, HOY, 1, AHORA, LOG)
        assert "ORDER BY pr.id DESC" in cur.sql

    def test_parte_inexistente_devuelve_error_para_el_tablero(self):
        cur = CursorFalso([[], []])
        id_reg, error = ord_sql.acumulador_no_planeado(
            cur, "MK05", "FANTASMA", DESDE, HOY, 1, AHORA, LOG)
        assert id_reg is None
        assert error == "PART_NUMBER_NO_EXISTE_BD"

    def test_error_de_base_no_tumba_el_ciclo(self):
        class CursorRoto(CursorFalso):
            def execute(self, sql, params=()):
                if "INSERT" in sql:
                    raise RuntimeError("deadlock")
                return super().execute(sql, params)

        id_reg, error = ord_sql.acumulador_no_planeado(
            CursorRoto([[]]), "MK05", "X", DESDE, HOY, 1, AHORA, LOG)
        assert (id_reg, error) == (None, "DB_ERROR")


class TestEstadosCoinciden:
    """Los números de estado son los de la base, no invenciones."""

    @pytest.mark.parametrize("nombre,valor", [
        ("PENDIENTE", 3), ("COMPLETADO", 4), ("EN_PROGRESO", 7),
        ("DETENIDO", 8), ("NO_PLANEADO", 24),
    ])
    def test_constantes(self, nombre, valor):
        assert getattr(ord_sql, nombre) == valor
