"""
Estampado: la única área que hoy difiere del comportamiento general.

Dos diferencias:
  1. El PLC manda un MDI (receta del troquel), no un número de parte. Se resuelve
     contra el catálogo y puede mapear a VARIOS números, todos con el mismo
     contador.
  2. El troquel se guarda en la columna `sequence` de histories.
"""

from typing import List, Optional, Tuple

from areas.base import AreaPipeline, ContextoArea


class EstampadoPipeline(AreaPipeline):

    nombre = "estampado"

    # Un golpe del troquel puede producir varias piezas: aquí sí se consulta
    # attributes.pieces_per_shot.
    usa_multiplicador = True

    # El MDI se traduce consultando el catálogo en la base.
    resuelve_en_catalogo = True

    def resolver_partes(self, raw: str, ctx: ContextoArea) -> Tuple[List[str], Optional[str]]:
        """El MDI se resuelve contra el catálogo, no con la expansión por '/'."""
        if not raw or not raw.strip():
            return [], "NO_PART_NUMBER_ESTAMPADO"

        if ctx.validar_estampado is None:
            ctx.log.error("❌ Estampado sin validador de MDI configurado")
            return [], "NO_PART_NUMBER_ESTAMPADO"

        numeros, error = ctx.validar_estampado(raw.strip(), ctx.estacion, ctx.log)
        if numeros:
            return numeros, None
        return [], error or "NO_PART_NUMBER_ESTAMPADO"

    def extras_history(self, dato: dict) -> dict:
        """El troquel viaja en la columna `sequence`."""
        return {"sequence": dato.get("troquel_id", 0)}
