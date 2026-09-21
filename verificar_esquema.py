"""
Comprueba que la base tiene lo que el modelo de órdenes da por sentado.

Correr ANTES de arrancar el servicio con el nuevo registro por órdenes:

    poetry run python verificar_esquema.py

No modifica nada: solo lee el catálogo del sistema y una muestra de datos.
"""

import os
import sys

import pyodbc
from dotenv import load_dotenv

load_dotenv()

# La consola de Windows suele venir en cp1252 y no sabe imprimir los símbolos
# de abajo; sin esto el script muere con UnicodeEncodeError en vez de informar.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

COLUMNAS = {
    "production_records": [
        ("shop_order_number", "la orden que se está llenando"),
        ("planned_quantity", "cuántas piezas pide la orden"),
        ("produced_quantity", "cuántas lleva"),
        ("planned_date", "para ordenar de la más antigua a la más nueva"),
        ("shift_id", "desempate cuando dos órdenes son del mismo día"),
        ("created_at", "último desempate"),
        ("status_id", "3 Pendiente / 4 Completado / 7 En progreso / 8 Detenido / 24 No planeado"),
        ("synced_to_infor", "lo ya enviado a Infor no se vuelve a tocar"),
        ("production_start", None),
        ("production_end", None),
    ],
    "histories": [
        ("shop_order_number", "a qué orden se atribuyó cada golpe"),
        ("quantity", "el incremento crudo del contador"),
        ("production_per_cycle", None),
        ("sequence", "el troquel, solo en Estampado"),
    ],
}

ESTADOS = {3: "Pendiente", 4: "Completado", 7: "En progreso",
           8: "Detenido", 24: "No planeado"}


def conectar():
    cadena = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={os.getenv('DB_SERVER')};DATABASE={os.getenv('DB_NAME')};"
        f"UID={os.getenv('DB_USER')};PWD={os.getenv('DB_PASSWORD')};"
        f"TrustServerCertificate=yes;"
    )
    return pyodbc.connect(cadena, timeout=10)


def revisar_columnas(cursor):
    problemas = []
    for tabla, esperadas in COLUMNAS.items():
        cursor.execute(
            "SELECT LOWER(COLUMN_NAME) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?",
            (tabla,))
        presentes = {f[0] for f in cursor.fetchall()}
        if not presentes:
            problemas.append(f"la tabla {tabla} no existe")
            continue
        print(f"\n  {tabla}")
        for columna, para_que in esperadas:
            hay = columna.lower() in presentes
            print(f"    {'✓' if hay else '✗'} {columna:<22}{para_que or ''}")
            if not hay:
                problemas.append(f"{tabla}.{columna} no existe")
    return problemas


def revisar_datos(cursor):
    print("\n  Estados en uso en production_records")
    cursor.execute("SELECT status_id, COUNT(*) FROM production_records "
                   "GROUP BY status_id ORDER BY status_id")
    for estado, cuantos in cursor.fetchall():
        print(f"    {estado:>3}  {ESTADOS.get(estado, '(desconocido)'):<14}{cuantos:>8} registros")

    print("\n  Órdenes que el recolector podría llenar hoy")
    cursor.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN shop_order_number IS NULL THEN 1 ELSE 0 END),
               SUM(CASE WHEN synced_to_infor IS NULL THEN 1 ELSE 0 END)
        FROM production_records
        WHERE status_id IN (3, 8) AND produced_quantity < planned_quantity
          AND planned_date >= DATEADD(day, -7, CAST(GETDATE() AS date))
    """)
    total, sin_orden, sin_sync = cursor.fetchone()
    print(f"    {total or 0} pendientes con hueco en los últimos 7 días")
    print(f"    {sin_orden or 0} sin shop_order_number  (el recolector las salta)")
    print(f"    {sin_sync or 0} con synced_to_infor NULL")
    if sin_sync:
        print("      ⚠️ La consulta usa ISNULL(synced_to_infor, 0) para incluirlas;")
        print("         con la comparación anterior (!= 1) quedaban fuera en silencio.")

    print("\n  Órdenes que ningún número entero de golpes puede llenar exacto")
    cursor.execute("""
        SELECT TOP(5) pr.id, pr.shop_order_number, pr.planned_quantity
        FROM production_records pr
        WHERE pr.status_id IN (3, 8) AND pr.planned_quantity > 0
        ORDER BY pr.planned_quantity ASC
    """)
    for id_reg, orden, plan in cursor.fetchall():
        print(f"    registro {id_reg}  orden {orden}  planeadas {plan}")
    print("    (con multiplicador > 1 estas se cierran con el golpe que las rebasa)")


def main():
    print(f"Base: {os.getenv('DB_SERVER')} / {os.getenv('DB_NAME')}")
    try:
        conexion = conectar()
    except Exception as e:
        print(f"\n❌ No se pudo conectar: {e}")
        return 2

    with conexion.cursor() as cursor:
        problemas = revisar_columnas(cursor)
        try:
            revisar_datos(cursor)
        except Exception as e:
            print(f"\n⚠️ No se pudo revisar los datos: {e}")

    if problemas:
        print("\n❌ Falta algo antes de arrancar:")
        for p in problemas:
            print(f"    · {p}")
        return 1

    print("\n✅ El esquema tiene todo lo que el modelo de órdenes necesita.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
