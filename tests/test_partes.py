"""Expansión de números de parte que manda el PLC (áreas distintas de Estampado)."""

import pytest

from domain.partes import procesar_numero_parte


class TestExpansion:
    def test_caso_del_usuario(self):
        """'DGH9 53 83 XB_ZB' son en realidad dos números de parte."""
        assert procesar_numero_parte("DGH9 53 83 XB_ZB") == ["DGH95383XB", "DGH95383ZB"]

    def test_alternativa_en_segmento_intermedio(self):
        assert procesar_numero_parte("ABC 12_34 99") == ["ABC1299", "ABC3499"]

    def test_dos_alternativas_dan_producto_cartesiano(self):
        assert procesar_numero_parte("A_B C_D") == ["AC", "AD", "BC", "BD"]

    def test_numero_simple_sin_separadores(self):
        assert procesar_numero_parte("SIMPLE123") == ["SIMPLE123"]

    def test_solo_espacios_se_concatenan(self):
        assert procesar_numero_parte("ABC 123") == ["ABC123"]

    def test_espacios_dobles(self):
        assert procesar_numero_parte("DGH9  53 XB_ZB") == ["DGH953XB", "DGH953ZB"]

    def test_alternativa_vacia_se_ignora(self):
        assert procesar_numero_parte("XB_ XB") == ["XBXB"]

    def test_deduplica(self):
        """'AB_AB' no debe crear dos registros para el mismo número."""
        assert procesar_numero_parte("AB_AB 9") == ["AB9"]

    @pytest.mark.parametrize("entrada", ["", "   ", None])
    def test_entradas_vacias(self, entrada):
        assert procesar_numero_parte(entrada) == []

    def test_resultado_sin_espacios(self):
        """Las consultas comparan con REPLACE(pn.number, ' ', ''): no deben quedar espacios."""
        for nombre in procesar_numero_parte("DGH9 53 83 XB_ZB"):
            assert " " not in nombre

    def test_el_orden_es_estable(self):
        """Importa porque determina qué registro se crea primero."""
        assert procesar_numero_parte("A_B C") == procesar_numero_parte("A_B C")


class TestLaDiagonalYaNoSepara:
    """
    Hay números de parte legítimos con diagonal en el catálogo —'VC6753/54T6P'
    y 'VA4053/54T6P' en MS10—. Mientras '/' fue el separador, esos números se
    partían en pedazos que no existen y su producción no se podía registrar.
    """

    @pytest.mark.parametrize("numero", [
        "VC6753/54T6P",
        "VA4053/54T6P",
        "DGH953/54812A-MAT",
        "BDTS53/54181/183-MAT",
    ])
    def test_se_respeta_tal_cual(self, numero):
        assert procesar_numero_parte(numero) == [numero]

    def test_con_espacios_la_diagonal_sigue_intacta(self):
        """El espacio une segmentos; la diagonal viaja dentro del resultado."""
        assert procesar_numero_parte("VC6753/54 T6P") == ["VC6753/54T6P"]

    def test_la_diagonal_y_el_guion_bajo_conviven(self):
        """Un número con diagonal que además trae alternativas se expande bien."""
        assert procesar_numero_parte("VC6753/54 T6P_T7P") == [
            "VC6753/54T6P", "VC6753/54T7P",
        ]
