"""Interpretación de los números de parte que manda el PLC."""

from itertools import product
from typing import List

#: Carácter con el que el PLC separa las ALTERNATIVAS dentro de un segmento.
#: Cambiarlo aquí lo cambia en todas las áreas que no son Estampado.
#:
#: Fue '/' hasta septiembre de 2026. Se movió a '_' porque hay números de parte
#: legítimos que llevan diagonal en el catálogo (p. ej. 'VC6753/54T6P' en MS10):
#: con '/' como separador esos números se partían en pedazos inexistentes y la
#: producción no se podía registrar. Ningún número del catálogo contiene '_'.
SEPARADOR_ALTERNATIVAS = "_"


def procesar_numero_parte(numero_plc: str) -> List[str]:
    """
    Expande el número de parte del PLC en todas sus combinaciones.

    El PLC puede condensar varios números en una sola cadena usando '_' como
    separador de alternativas dentro de un segmento:

        'DGH9 53 83 XB_ZB'  ->  ['DGH95383XB', 'DGH95383ZB']
        'ABC 12_34 99'      ->  ['ABC1299', 'ABC3499']

    Se divide primero por espacios (segmentos) y luego cada segmento por '_'
    (alternativas); el producto cartesiano de los segmentos da las combinaciones.
    Los espacios se eliminan del resultado para empatar con la comparación
    REPLACE(pn.number, ' ', '') que usan las consultas contra part_numbers.

    La diagonal ya NO separa: un número que la traiga se busca tal cual, que es
    como viene en el catálogo.

    NO aplica a Estampado: esa área resuelve el MDI contra el catálogo.
    """
    if not numero_plc:
        return []

    segmentos = []
    for seg in numero_plc.split(' '):
        if not seg:
            continue
        # Alternativas del segmento, descartando vacías (p.ej. 'XB_' -> ['XB'])
        alternativas = [alt for alt in seg.split(SEPARADOR_ALTERNATIVAS) if alt]
        if alternativas:
            segmentos.append(alternativas)

    if not segmentos:
        return []

    combinaciones = []
    for combo in product(*segmentos):
        nombre = ''.join(combo).replace(' ', '').strip()
        # Dedup: 'AB_AB' no debe generar dos registros para el mismo número
        if nombre and nombre not in combinaciones:
            combinaciones.append(nombre)

    return combinaciones
