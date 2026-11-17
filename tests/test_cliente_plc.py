"""
El cliente del PLC nunca convierte una respuesta incompleta o ajena en lectura.

Con pymcprotocol tal cual, una respuesta partida por la red se decodificaba
con ceros en lo que faltaba, y una respuesta tardía se tomaba como la de la
petición siguiente. Un contador leído en 0 por cualquiera de las dos parece un
reinicio, y la lectura que sigue registra el contador entero como corrida
nueva: producción fantasma.
"""

import asyncio
import logging
import socket
import struct

import pytest
from pymcprotocol.mcprotocolerror import MCProtocolError

import Prensas
from plc.cliente import ClienteMC, RespuestaInvalida, _LINGER_ABORTIVO

FIN = object()   # el PLC cerró la conexión


def trama(palabras=(), codigo_fin=0, detalle=b""):
    """Una respuesta 3E binaria como la manda el PLC."""
    datos = codigo_fin.to_bytes(2, "little") + detalle + b"".join(
        p.to_bytes(2, "little", signed=True) for p in palabras)
    return (b"\xd0\x00" + b"\x00" + b"\xff" + b"\xff\x03" + b"\x00"
            + len(datos).to_bytes(2, "little") + datos)


class SocketFalso:
    """
    `pendiente` es lo que ya está esperando en el socket; `por_peticion` es lo
    que llegará después de cada send(), una lista de trozos por petición. Los
    trozos se entregan uno por recv(), como cuando la red parte una trama.
    """

    def __init__(self, pendiente=(), por_peticion=()):
        self.buzon = list(pendiente)
        self.por_peticion = [list(p) for p in por_peticion]
        self.bloqueante = True
        self.opciones = []
        self.cerrado = False

    def send(self, datos):
        if self.por_peticion:
            self.buzon.extend(self.por_peticion.pop(0))
        return len(datos)

    def recv(self, n):
        if not self.buzon:
            if not self.bloqueante:
                raise BlockingIOError
            raise TimeoutError("timed out")
        if self.buzon[0] is FIN:
            return b""
        trozo = self.buzon[0]
        if len(trozo) > n:
            self.buzon[0] = trozo[n:]
            return trozo[:n]
        self.buzon.pop(0)
        return trozo

    def setblocking(self, bandera):
        self.bloqueante = bandera

    def settimeout(self, segundos):
        self.bloqueante = segundos is not None

    def setsockopt(self, *opcion):
        self.opciones.append(opcion)

    def close(self):
        self.cerrado = True


def cliente(sock):
    c = ClienteMC()
    c.setaccessopt(timer_sec=3)
    c._sock = sock
    c._ip = "10.1.1.1"
    c._is_connected = True
    return c


class TestLaRespuestaSeLeeCompleta:
    def test_una_trama_partida_por_la_red_no_se_rellena_con_ceros(self):
        """El caso que la librería convertía en contador 0."""
        completa = trama([240, 17])
        sock = SocketFalso(por_peticion=[[completa[:5], completa[5:12], completa[12:]]])

        assert cliente(sock).batchread_wordunits("D100", 2) == [240, 17]

    def test_si_no_llega_completa_es_timeout_no_ceros(self):
        """
        Sigue siendo "timed out": el lector ya sabe reintentar eso sin
        reconectar.
        """
        sock = SocketFalso(por_peticion=[[trama([240, 17])[:12]]])

        with pytest.raises(TimeoutError, match="timed out"):
            cliente(sock).batchread_wordunits("D100", 2)

    def test_si_el_plc_cierra_a_media_respuesta_es_error_de_conexion(self):
        sock = SocketFalso(por_peticion=[[trama([240, 17])[:12], FIN]])

        with pytest.raises(ConnectionResetError):
            cliente(sock).batchread_wordunits("D100", 2)


class TestUnaRespuestaAjenaNoSeTomaComoLectura:
    def test_la_respuesta_tardia_se_descarta_antes_de_pedir(self):
        """
        Llegó la respuesta de una petición que ya se había dado por vencida.
        Sin descartarla, sería la "lectura" de esta petición.
        """
        sock = SocketFalso(pendiente=[trama([999])], por_peticion=[[trama([5])]])

        assert cliente(sock).batchread_wordunits("D100", 1) == [5]

    def test_una_respuesta_de_otro_tamano_se_rechaza(self):
        """Si aun así llega algo que no es lo pedido, no se decodifica."""
        sock = SocketFalso(por_peticion=[[trama([999, 998, 997])]])

        with pytest.raises(RespuestaInvalida):
            cliente(sock).batchread_wordunits("D100", 1)

    def test_lo_que_no_es_una_trama_3e_se_rechaza(self):
        sock = SocketFalso(por_peticion=[[b"\x00" * 13]])

        with pytest.raises(RespuestaInvalida):
            cliente(sock).batchread_wordunits("D100", 1)

    def test_rechazarla_obliga_a_reconectar(self):
        """Es un error de conexión: el lector cierra y abre otra limpia."""
        assert issubclass(RespuestaInvalida, ConnectionError)

    def test_el_error_que_reporta_el_plc_sigue_llegando_como_tal(self):
        """Código de fin distinto de 0: trae detalle del error, no datos."""
        sock = SocketFalso(por_peticion=[[trama(codigo_fin=0xC056, detalle=b"\x00" * 9)]])

        with pytest.raises(MCProtocolError):
            cliente(sock).batchread_wordunits("D100", 1)


class TestCierre:
    def test_cierra_con_rst_para_que_el_plc_suelte_la_conexion(self):
        sock = SocketFalso()
        c = cliente(sock)

        c.close()

        assert (socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_ABORTIVO) in sock.opciones
        assert sock.cerrado
        assert c._is_connected is False

    def test_cerrar_sin_haber_conectado_no_truena(self):
        ClienteMC().close()

    def test_linger_activo_con_cero_segundos(self):
        """Activo y 0 s es lo que convierte el cierre en RST."""
        formato = "HH" if len(_LINGER_ABORTIVO) == 4 else "ii"
        assert struct.unpack(formato, _LINGER_ABORTIVO) == (1, 0)


class TestElLectorSueltaLaConexionAlTerminar:
    def test_al_cancelarlo_cierra_el_cliente(self, monkeypatch):
        """
        Apagar el servicio o quitar la IP de la configuración cancela el
        lector. Antes el socket se abandonaba y el sistema lo cerraba con FIN;
        ahora pasa por ClienteMC.close(), que cierra con RST.
        """
        eventos = []

        class ClienteFalso:
            def setaccessopt(self, **_):
                pass

            def connect(self, ip, port):
                eventos.append("connect")

            def close(self):
                eventos.append("close")

            def batchread_wordunits(self, headdevice, readsize):
                return [0] * readsize

        monkeypatch.setattr(Prensas, "ClienteMC", ClienteFalso)
        monkeypatch.setattr(Prensas, "ip_data_queues", {})
        monkeypatch.setattr(Prensas, "get_station_logger", lambda _e: logging.getLogger("test"))
        info = {"estaciones": [], "all_addresses": set(), "station_configs": {},
                "area": "Chasis", "port": 5002, "_version": 1}

        async def correr_y_cancelar():
            lector = asyncio.create_task(Prensas.plc_reader("10.9.9.9", 5002, info))
            await asyncio.sleep(0.3)
            lector.cancel()
            with pytest.raises(asyncio.CancelledError):
                await lector

        asyncio.run(correr_y_cancelar())

        assert "connect" in eventos
        assert eventos[-1] == "close", eventos
