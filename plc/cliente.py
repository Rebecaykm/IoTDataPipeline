"""
Cliente MC protocol (trama 3E binaria) que no inventa lecturas.

pymcprotocol.Type3E tiene tres huecos que aquí se cierran:

  1. Lee la respuesta con UN solo recv(). Si la red la entrega partida, la
     librería decodifica lo que llegó y rellena lo que falta con CEROS, sin
     error. Un contador leído así en 0 parece un reinicio, y la lectura
     siguiente registra el contador entero como corrida nueva: producción
     fantasma. Aquí la trama se lee completa, según la longitud que trae su
     propia cabecera.

  2. La trama 3E no lleva número de serie: si una respuesta llega tarde
     (después de que el socket se dio por vencido), la librería la toma como
     respuesta de la SIGUIENTE petición, y desde ahí todas las lecturas quedan
     recorridas una posición. Aquí, antes de cada petición, se descarta lo que
     haya quedado pendiente en el socket; y si aun así la respuesta no tiene el
     tamaño de lo que se pidió, se rechaza en vez de decodificarla.

  3. close() cierra con FIN normal y espera a que el PLC complete el cierre.
     Hay módulos Ethernet que no lo completan (MK05, noviembre de 2026): la
     conexión se queda ocupando un lugar en la tabla del PLC, y las conexiones
     nuevas abren pero nunca reciben respuesta. Aquí se cierra con RST, que
     obliga al módulo a soltarla en el momento. El recolector solo cierra para
     abandonar una conexión (error o reconexión), así que no hay nada en vuelo
     que se pierda por no esperar.

Solo trama binaria, que es la única que usa el recolector.
"""

import logging
import os
import socket
import struct
import time

from pymcprotocol import Type3E

logger = logging.getLogger("supervisor")

#: Subencabezado de una RESPUESTA 3E binaria.
SUBENCABEZADO_RESPUESTA = b"\xd0\x00"
#: Subencabezado (2) + red (1) + PC (1) + E/S destino (2) + estación (1) +
#: longitud de datos (2). La longitud cuenta desde el código de fin.
LARGO_CABECERA = 9
LARGO_CODIGO_FIN = 2

#: SO_LINGER activo con 0 segundos: close() manda RST en vez de FIN. En
#: Windows el struct linger son dos u_short; en POSIX, dos int.
_LINGER_ABORTIVO = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)


class RespuestaInvalida(ConnectionError):
    """
    Lo que llegó por el socket no es la respuesta a lo que se pidió.

    Es un ConnectionError a propósito: el flujo de bytes ya no es confiable y
    quien lee el PLC lo trata como cualquier error de conexión (cierra y
    reconecta), en vez de decodificar basura.
    """


class ClienteMC(Type3E):
    """Type3E con lectura de trama completa, descarte de respuestas tardías y cierre con RST."""

    #: Palabras que debe traer la respuesta en curso; None si no se sabe.
    _palabras_esperadas = None

    def batchread_wordunits(self, headdevice, readsize):
        self._palabras_esperadas = readsize
        try:
            return super().batchread_wordunits(headdevice, readsize)
        finally:
            self._palabras_esperadas = None

    def close(self):
        sock = getattr(self, "_sock", None)
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_ABORTIVO)
            except OSError:
                pass   # socket ya inservible: se cierra igual
            sock.close()
        self._is_connected = False

    # ── lo que sustituye a la librería ────────────────────────────────────
    def _send(self, send_data):
        self._descartar_pendiente()
        super()._send(send_data)

    def _recv(self):
        limite = time.monotonic() + self.soc_timeout
        cabecera = self._leer_exacto(LARGO_CABECERA, limite)
        if cabecera[:2] != SUBENCABEZADO_RESPUESTA:
            raise RespuestaInvalida(
                f"PLC {self._ip}: la respuesta no empieza como una trama 3E "
                f"({cabecera.hex(' ')})"
            )

        largo = int.from_bytes(cabecera[7:9], "little")
        cuerpo = self._leer_exacto(largo, limite)

        # Con código de fin distinto de 0 el PLC manda el detalle del error en
        # vez de datos: eso lo reporta la librería (_check_cmdanswer).
        codigo_fin = int.from_bytes(cuerpo[:LARGO_CODIGO_FIN], "little")
        esperado = self._palabras_esperadas
        if codigo_fin == 0 and esperado is not None:
            largo_esperado = LARGO_CODIGO_FIN + esperado * self._wordsize
            if largo != largo_esperado:
                raise RespuestaInvalida(
                    f"PLC {self._ip}: se pidieron {esperado} palabras y la "
                    f"respuesta trae {(largo - LARGO_CODIGO_FIN) // self._wordsize}"
                )
        return cabecera + cuerpo

    # ── apoyo ─────────────────────────────────────────────────────────────
    def _leer_exacto(self, n, limite):
        """Exactamente `n` bytes, o TimeoutError si se acaba el plazo."""
        datos = bytearray()
        try:
            while len(datos) < n:
                restante = limite - time.monotonic()
                if restante <= 0:
                    raise TimeoutError("timed out")
                self._sock.settimeout(restante)
                trozo = self._sock.recv(n - len(datos))
                if not trozo:
                    raise ConnectionResetError(
                        f"PLC {self._ip}: cerró la conexión a media respuesta")
                datos += trozo
        finally:
            self._sock.settimeout(self.soc_timeout)
        return bytes(datos)

    def _descartar_pendiente(self):
        """
        Tira lo que esté esperando en el socket antes de pedir algo nuevo.

        Antes de una petición no debería haber nada: lo que haya es una
        respuesta que llegó después de que su petición se dio por vencida, o
        una que el PLC mandó de más. El lector espera al menos un segundo antes
        de reintentar, más que el temporizador del propio PLC, así que una
        respuesta tardía ya llegó para entonces y se descarta aquí.
        """
        if not self._is_connected:
            return
        descartados = bytearray()
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    trozo = self._sock.recv(self._SOCKBUFSIZE)
                except (BlockingIOError, InterruptedError):
                    break
                if not trozo:
                    raise ConnectionResetError(f"PLC {self._ip}: cerró la conexión")
                descartados += trozo
        finally:
            self._sock.settimeout(self.soc_timeout)
        if descartados:
            # El contenido va en el aviso: es lo único que permite saber si fue
            # una respuesta tardía, una duplicada del PLC o basura de la red.
            logger.warning(
                f"🧹 PLC {self._ip}: se descartaron {len(descartados)} bytes que "
                f"nadie pidió; no se toman como lectura: {bytes(descartados[:48]).hex(' ')}"
            )
