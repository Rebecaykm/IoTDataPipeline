"""
Casos de conteo. Cada uno corresponde a una situación real de piso que
costó trabajo diagnosticar; quedan congelados aquí para que no vuelvan.
"""

import pytest

from domain.contadores import calcular_incremento


class TestIncremento:
    def test_avance_normal(self):
        assert calcular_incremento(20, 23) == (3, False)

    def test_sin_avance(self):
        """Contador detenido: la estación no produjo nada."""
        assert calcular_incremento(150, 150) == (0, False)

    def test_reset_a_cero(self):
        """Termina la corrida y el operador reinicia el contador."""
        assert calcular_incremento(64, 0) == (0, True)

    def test_reset_con_produccion(self):
        """
        Caso real de MT05: la BD tenía 64 y el PLC volvió a 3.
        El delta debe ser 3 (los golpes de la corrida nueva), NUNCA -61.
        """
        incremento, hubo_reset = calcular_incremento(64, 3)
        assert incremento == 3
        assert hubo_reset is True
        assert incremento >= 0

    def test_reset_desde_cero(self):
        assert calcular_incremento(0, 5) == (5, False)

    def test_salto_grande(self):
        """Si el servicio estuvo caído, el PLC siguió contando: se recupera todo."""
        assert calcular_incremento(30, 45) == (15, False)

    @pytest.mark.parametrize("prev,cnt", [(0, 0), (1, 0), (999, 1), (5, 5)])
    def test_nunca_negativo(self, prev, cnt):
        incremento, _ = calcular_incremento(prev, cnt)
        assert incremento >= 0


class TestCorridasSucesivas:
    def test_corrida_nueva_tras_reset_suma_al_acumulado(self):
        """El bug original: la segunda corrida NO debe borrar la primera."""
        acumulado = 64  # lo que ya está en la base

        inc, hubo_reset = calcular_incremento(64, 0)   # reset
        acumulado += inc
        assert hubo_reset and acumulado == 64          # nada se pierde

        for prev, cnt in [(0, 1), (1, 2), (2, 20)]:
            inc, _ = calcular_incremento(prev, cnt)
            acumulado += inc

        assert acumulado == 84  # 64 de la primera corrida + 20 de la segunda
