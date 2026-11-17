"""Clasificación de los errores de SQL Server que cambian lo que hay que hacer."""

import pyodbc


def es_interbloqueo(e) -> bool:
    """
    Si SQL Server eligió esta transacción como víctima de un interbloqueo.

    Es el único error que NUNCA se puede tolerar a media transacción: el motor
    ya revirtió TODO lo escrito en ella, así que seguir como si nada y hacer
    commit confirmaría solo lo que venga después, y la línea base avanzaría
    sobre producción que ya no está en la base. Quien lo atrape tiene que
    volver a lanzarlo para que el ciclo completo se reintente.
    """
    if isinstance(e, pyodbc.Error) and e.args and e.args[0] == '40001':
        return True
    texto = str(e)
    return '1205' in texto and 'deadlock' in texto.lower()
