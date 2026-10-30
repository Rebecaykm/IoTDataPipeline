"""
Acceso a datos: conexiones, SQL y archivo de estado.

Todo el SQL contra SQL Server vive aquí, repartido en dos módulos:

    ordenes.py      production_records: buscar la orden abierta más antigua,
                    llenarla, cerrarla al completarse. Es donde vive la regla
                    de negocio de cómo se registra la producción.
    repositorio.py  el resto: catálogo de partes e histories.
    catalogo.py     resolución de MDI y multiplicadores (antes venía de AS400).
    estado.py       el caché en disco de la línea base de cada contador.

Las funciones reciben el `cursor` de quien las llama: la transacción la maneja
el llamador, que es quien sabe dónde empieza y termina una unidad de trabajo.
"""
