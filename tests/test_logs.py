"""
Cuánto ruido deja el recolector en los logs.

Un renglón que se repite en cada ciclo no informa: tapa lo que sí hay que ver y
llena el disco. Con 109 estaciones creciendo al triple, un disco lleno detiene
el recolector.
"""

import pytest

import Prensas

ESTACION = "2500T  TR"
CFG = {"Contador LH": {"address": "D3100", "long": 1},
       "Número de Parte LH": {"address": "D3104", "long": 10}}


class LogEspia:
    """Cuenta lo que el recolector escribe, sin tocar disco."""

    def __init__(self):
        self.avisos = []

    def info(self, mensaje):
        self.avisos.append(str(mensaje))

    warning = error = debug = info

    @property
    def divisiones(self):
        return [m for m in self.avisos if "🔀" in m]


@pytest.fixture
def ciclo(monkeypatch):
    """
    Devuelve (leer, espia). `leer(mdi, contador)` simula un ciclo de lectura:
    el PLC manda ese texto y ese contador.

    El MDI se resuelve partiendo por '+', para aislar el comportamiento del log
    de cómo se resuelven las partes.
    """
    espia = LogEspia()
    monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: espia)
    monkeypatch.setattr(Prensas, "decodificar_bloque",
                        lambda vals: (_ultimo["mdi"], [_ultimo["mdi"]], {}))

    col = Prensas.IPDataCollector("10.1.1.1", [ESTACION], "Estampado")
    monkeypatch.setattr(col._pipeline, "resolver_partes",
                        lambda raw, ctx: ([p for p in raw.split("+") if p], None))

    _ultimo = {"mdi": ""}

    def leer(mdi, contador):
        _ultimo["mdi"] = mdi
        bloques = {("D3100", 1): [contador], ("D3104", 10): [0] * 10}
        return col._process_station_data(ESTACION, CFG, bloques, None)

    return leer, espia


class TestRuidoEnElLog:
    def test_la_division_se_registra_una_vez_no_en_cada_ciclo(self, ciclo):
        """
        Con un MDI de dos partes el renglón salía 86,400 veces al día por lado.
        Era el 74% del log de la 2500T: 25,526 de 34,441 renglones.
        """
        leer, espia = ciclo
        for contador in range(1, 21):
            leer("BDTS53241+BDTS54241A", contador)

        assert len(espia.divisiones) == 1, espia.divisiones

    def test_si_cambia_el_mdi_se_vuelve_a_registrar(self, ciclo):
        leer, espia = ciclo
        leer("BDTS53241+BDTS54241A", 1)
        leer("BDTS53241+BDTS54241A", 2)
        leer("DGH970051+DGH970052", 3)

        assert len(espia.divisiones) == 2

    def test_si_vuelve_el_mismo_mdi_despues_de_otro_se_registra(self, ciclo):
        """Es una corrida distinta, aunque el MDI se repita."""
        leer, espia = ciclo
        leer("A+B", 1)
        leer("C+D", 2)
        leer("A+B", 3)

        assert len(espia.divisiones) == 3

    def test_tras_una_parte_sola_el_mismo_mdi_vuelve_a_registrarse(self, ciclo):
        """
        El troquel cambió a uno de una sola parte y luego volvió el de dos. Es
        una corrida nueva: tiene que quedar constancia, aunque el MDI se repita.
        """
        leer, espia = ciclo
        leer("A+B", 1)
        leer("SOLA", 2)
        leer("A+B", 3)

        assert len(espia.divisiones) == 2

    def test_una_sola_parte_nunca_registra_division(self, ciclo):
        leer, espia = ciclo
        for contador in range(1, 6):
            leer("BDTS53241", contador)

        assert espia.divisiones == []

    def test_el_contador_ya_no_va_en_el_mensaje(self, ciclo):
        """Iba en el texto, y por eso cada ciclo producía un renglón distinto."""
        leer, espia = ciclo
        leer("A+B", 7)

        assert "contador" not in espia.divisiones[0]

    def test_el_mensaje_conserva_lo_que_sirve(self, ciclo):
        """Quitar ruido no es quitar información: debe decir qué resolvió a qué."""
        leer, espia = ciclo
        leer("BDTS53241+BDTS54241A", 1)
        mensaje = espia.divisiones[0]

        assert ESTACION in mensaje
        assert "BDTS53241+BDTS54241A" in mensaje
        assert "BDTS54241A" in mensaje


class TestRotacion:
    def test_ochenta_megas_por_estacion(self):
        total = Prensas.LOG_MAX_BYTES * (1 + Prensas.LOG_RESPALDOS)
        assert total == 80 * 1024 * 1024

    def test_archivos_chicos_para_conservar_mas_tramos(self):
        """
        Mismo tope, más tramos: se conserva más historia y cada archivo se puede
        abrir. Antes eran 100 MB x 2 = 300 MB por estación (32 GB en las 109).
        """
        assert Prensas.LOG_MAX_BYTES <= 10 * 1024 * 1024
        assert Prensas.LOG_RESPALDOS >= 5
