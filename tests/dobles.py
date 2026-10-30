"""
Dobles de prueba para la capa de persistencia.

La idea no es simular SQL Server —eso sería reescribirlo— sino fijar el
CONTRATO: qué consultas se lanzan, en qué orden, con qué parámetros, y qué
hace el código con lo que le devuelven.
"""

import re


def normalizar(sql):
    """Colapsa los espacios para poder buscar fragmentos sin pelear con el formato."""
    return re.sub(r"\s+", " ", str(sql)).strip()


class CursorFalso:
    """
    Cursor de pyodbc mínimo.

    `respuestas` es la lista de resultados que devolverá cada execute(), en
    orden. Una lista vacía significa "la consulta no encontró nada".
    """

    def __init__(self, respuestas=None):
        self.respuestas = [list(r) for r in (respuestas or [])]
        self.ejecutados = []          # [(sql, params), ...] tal cual se lanzaron
        self.rowcount = 1
        self._filas = []

    def execute(self, sql, params=()):
        self.ejecutados.append((sql, tuple(params)))
        self._filas = self.respuestas.pop(0) if self.respuestas else []
        self.rowcount = len(self._filas)
        return self

    def fetchone(self):
        return self._filas[0] if self._filas else None

    def fetchall(self):
        return list(self._filas)

    # ── ayudas para las aserciones ────────────────────────────────────────
    @property
    def sql(self):
        """La última consulta, normalizada."""
        return normalizar(self.ejecutados[-1][0])

    @property
    def params(self):
        """Los parámetros de la última consulta."""
        return self.ejecutados[-1][1]

    def sql_de(self, indice):
        return normalizar(self.ejecutados[indice][0])

    def params_de(self, indice):
        return self.ejecutados[indice][1]

    def __len__(self):
        return len(self.ejecutados)
