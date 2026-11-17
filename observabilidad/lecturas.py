"""
Últimas lecturas del PLC, tal como las ve el recolector.

Se llena en cada ciclo de lectura, ANTES de cualquier decisión de negocio: aquí
está lo que el PLC mandó, aunque después la parte se rechace o el contador no
avance. Es el equivalente a mirar la tabla del recolector, pero por HTTP y
usando la MISMA conexión al PLC — sin abrir una segunda.

Cada fila dice además en qué ESTADO está, porque "lo último que se leyó" y "lo
que el PLC manda ahora" no son lo mismo:

    En vivo              el PLC respondió y ese lado trae número de parte
    Rechazada            trae número, pero no se resolvió contra el catálogo
    Sin número de parte  el PLC respondió y el lado viene en blanco: se muestra
                         el contador, sin número, y la antigüedad sigue fresca
    PLC sin respuesta    no llega una lectura de ese lado hace más de
                         UMBRAL_SIN_RESPUESTA: el PLC no está mandando NADA,
                         así que número, contador, ciclo y troquel salen
                         vacíos. Lo último que mandó se conserva aparte, en
                         los campos `ultimo_*`, por si hace falta consultarlo

Las columnas del PLC son SIEMPRE lo leído en ese ciclo, tal cual: si manda 0 se
ve 0, y si no manda nada no se ve nada. El contador que el recolector recuerda
para contar (state_cache) es otra cosa y no se muestra aquí.

Antes solo se anotaba cuando el lado traía número. Una estación que dejaba de
mandarlo (fin de semana, cambio de modelo) se quedaba para siempre con el último
número y contador que tuvo, y la única pista era la columna de antigüedad.
"""

import threading
from datetime import datetime
from typing import Dict, Tuple


#: Segundos sin lectura a partir de los cuales una fila ya no es "en vivo".
#: Holgado a propósito: un timeout con sus reintentos y una reconexión caben
#: aquí sin que el tablero parpadee.
UMBRAL_SIN_RESPUESTA = 30

EN_VIVO = "En vivo"
RECHAZADA = "Rechazada"
SIN_NUMERO = "Sin número de parte"
SIN_RESPUESTA = "PLC sin respuesta"

#: Lo que viene del PLC en cada lectura. Sin respuesta, estas columnas no
#: tienen valor "en vivo" que mostrar.
CAMPOS_DEL_PLC = ("numero_plc", "numero_validado", "contador", "tiempo_ciclo",
                  "troquel", "validado", "error")


class RegistroLecturas:
    """Última lectura por (estación, lado). Lo escribe el hilo del colector."""

    def __init__(self):
        self._lecturas: Dict[Tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def anotar(self, estacion, lado, ip=None, area=None, numero_plc=None,
               numero_validado=None, contador=None, tiempo_ciclo=None,
               troquel=None, validado=None, error=None, sin_parte=False):
        """
        Guarda lo que se acaba de leer. Sobrescribe la lectura anterior.

        `sin_parte` marca un lado que el PLC SÍ respondió pero viene sin número:
        la fila se queda, con su contador y sin número, en vez de seguir
        mostrando el último que tuvo.
        """
        with self._lock:
            self._lecturas[(estacion, lado)] = {
                "estacion": estacion,
                "lado": lado,
                "ip": ip,
                "area": area,
                "numero_plc": numero_plc,          # crudo, con espacios
                "numero_validado": numero_validado,  # resuelto, si se resolvió
                "contador": contador,
                "tiempo_ciclo": tiempo_ciclo,
                "troquel": troquel,
                "validado": validado,
                "error": error,
                "sin_parte": bool(sin_parte),
                "ts": datetime.now(),
            }

    def conservar_lados(self, estacion, lados):
        """
        Quita los lados de la estación que NO vinieron en una lectura completa.

        Solo se llama con lectura completa del PLC: ahí un lado que no aparece
        es que ya no existe (se quitó su tag), no que no se pudo leer.
        """
        lados = set(lados)
        with self._lock:
            for clave in [k for k in self._lecturas
                          if k[0] == estacion and k[1] not in lados]:
                del self._lecturas[clave]

    def olvidar_ip(self, ip, excepto=()):
        """
        Quita las filas de un PLC, salvo las estaciones de `excepto`.

        Para cuando una estación se da de baja o se mueve de IP: sin esto su
        última lectura se quedaba en el tablero hasta reiniciar el servicio.
        """
        excepto = set(excepto)
        with self._lock:
            for clave in [k for k, f in self._lecturas.items()
                          if f.get("ip") == ip and k[0] not in excepto]:
                del self._lecturas[clave]

    def olvidar_estacion(self, estacion):
        with self._lock:
            for clave in [k for k in self._lecturas if k[0] == estacion]:
                del self._lecturas[clave]

    def snapshot(self, ip=None, estacion=None):
        """Lista ordenada por estación y lado, con la antigüedad de cada dato."""
        with self._lock:
            filas = list(self._lecturas.values())

        if ip:
            filas = [f for f in filas if f.get("ip") == ip]
        if estacion:
            filas = [f for f in filas if f.get("estacion") == estacion]

        ahora = datetime.now()
        salida = []
        for f in sorted(filas, key=lambda x: (x["estacion"], x["lado"])):
            d = dict(f)
            d["ts"] = f["ts"].isoformat(timespec="seconds")
            d["hace_segundos"] = round((ahora - f["ts"]).total_seconds(), 1)
            if d["hace_segundos"] > UMBRAL_SIN_RESPUESTA:
                d["estado"] = SIN_RESPUESTA
                # El PLC no está mandando nada: mostrar su último contador en
                # la columna "en vivo" es justo la confusión que este tablero
                # no debe causar. Se aparta en `ultimo_*` y la columna va vacía.
                d["ultimo_numero_plc"] = d.get("numero_plc")
                d["ultimo_contador"] = d.get("contador")
                for campo in CAMPOS_DEL_PLC:
                    d[campo] = None
            elif f.get("sin_parte"):
                d["estado"] = SIN_NUMERO
            elif f.get("error") or f.get("validado") is False:
                d["estado"] = RECHAZADA
            else:
                d["estado"] = EN_VIVO
            salida.append(d)
        return salida


#: Registro global, compartido entre el colector y el servidor HTTP.
LECTURAS = RegistroLecturas()
