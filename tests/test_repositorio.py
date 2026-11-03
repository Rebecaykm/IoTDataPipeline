"""
obtener_part_number_id() es el único punto de resolución de número+estación a
part_numbers.id en todo el repo (ver persistence/ordenes.py::acumulador_no_planeado
e historias en Prensas.py). Sin el filtro de obsoleto, un catálogo con dos filas
para el mismo número+estación (una vigente, otra dada de baja) podía resolver
una distinta para production_records y para histories -- eso pasó en producción.
"""
from persistence.repositorio import obtener_part_number_id
from tests.dobles import CursorFalso


class TestObtenerPartNumberId:
    def test_devuelve_el_id_cuando_hay_una_sola_fila(self):
        cur = CursorFalso([[(40689,)]])
        assert obtener_part_number_id(cur, "5202504020A-BP", "EFB SG-20B") == 40689

    def test_filtra_is_obsolete(self):
        """La causa real del bug: sin este filtro podía devolver la obsoleta."""
        cur = CursorFalso([[(40689,)]])
        obtener_part_number_id(cur, "5202504020A-BP", "EFB SG-20B")
        assert "pn.is_obsolete = 0" in cur.sql

    def test_ordena_por_id_descendente(self):
        """
        Defensivo: si alguna vez hay más de una fila ACTIVA a la vez (no
        debería), toma la más reciente en vez de una al azar.
        """
        cur = CursorFalso([[(40689,)]])
        obtener_part_number_id(cur, "5202504020A-BP", "EFB SG-20B")
        assert "ORDER BY pn.id DESC" in cur.sql

    def test_ninguna_fila_devuelve_none(self):
        cur = CursorFalso([[]])
        assert obtener_part_number_id(cur, "FANTASMA", "MK05") is None

    def test_solo_la_obsoleta_devuelve_none(self):
        """
        El filtro en el WHERE ya excluye la obsoleta: si todo lo que hay es
        obsoleto, la consulta no devuelve filas.
        """
        cur = CursorFalso([[]])
        assert obtener_part_number_id(cur, "VC675332Y", "MN SUB-20") is None

    def test_params_exactos(self):
        cur = CursorFalso([[(1,)]])
        obtener_part_number_id(cur, "ABC123", "MK05")
        assert cur.params == ("ABC123", "MK05")
