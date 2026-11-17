"""
El pool de conexiones a SQL Server: una por hilo, sin consulta de prueba.

Antes, cada vez que alguien pedía la conexión se lanzaba un SELECT 1 para
comprobar que siguiera viva: una consulta extra por estación por segundo. Ahora
se entrega tal cual, y quien recibe un error del driver la descarta para que la
siguiente petición abra otra.
"""

import threading

import pytest

import Prensas
from Prensas import ConnectionPool


class ConexionDePrueba:
    def __init__(self):
        self.cerrada = False
        self.cursores = 0

    def cursor(self):
        self.cursores += 1
        raise AssertionError("el pool no debe consultar nada para entregarla")

    def close(self):
        self.cerrada = True


@pytest.fixture
def creadas(monkeypatch):
    """Lo que el pool abrió, en orden. Cada prueba arranca con el pool vacío."""
    abiertas = []

    def conectar():
        abiertas.append(ConexionDePrueba())
        return abiertas[-1]

    monkeypatch.setattr(ConnectionPool, "_conectar", staticmethod(conectar))
    monkeypatch.setattr(ConnectionPool, "_conexiones", {})
    return abiertas


class TestUnaPorHilo:
    def test_se_reutiliza_sin_consulta_de_prueba(self, creadas):
        primera = ConnectionPool.obtener()
        segunda = ConnectionPool.obtener()

        assert primera is segunda
        assert len(creadas) == 1
        assert primera.cursores == 0

    def test_otro_hilo_recibe_la_suya(self, creadas):
        aqui = ConnectionPool.obtener()
        alla = []
        hilo = threading.Thread(target=lambda: alla.append(ConnectionPool.obtener()))
        hilo.start()
        hilo.join()

        assert alla[0] is not aqui


class TestTrasUnError:
    def test_descartar_la_cierra_y_la_siguiente_es_nueva(self, creadas):
        vieja = ConnectionPool.obtener()

        Prensas.descartar_conexion()
        nueva = ConnectionPool.obtener()

        assert vieja.cerrada
        assert nueva is not vieja

    def test_descartar_sin_conexion_no_truena(self, creadas):
        Prensas.descartar_conexion()

    def test_sin_base_no_guarda_nada_y_reintenta_a_la_siguiente(self, monkeypatch):
        intentos = []
        monkeypatch.setattr(ConnectionPool, "_conectar",
                            staticmethod(lambda: intentos.append(1)))
        monkeypatch.setattr(ConnectionPool, "_conexiones", {})

        assert ConnectionPool.obtener() is None
        assert ConnectionPool.obtener() is None
        assert len(intentos) == 2
