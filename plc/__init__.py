"""
Todo lo que tiene que ver con el PLC, sin base de datos:

    cliente.py       la conexión MC protocol: lee tramas completas, descarta
                     respuestas tardías y cierra con RST
    decodificador.py traduce los words leídos a texto y agrupa los tags por lado
"""

from plc.decodificador import decodificar_bloque, parse_tag

__all__ = ["decodificar_bloque", "parse_tag"]
