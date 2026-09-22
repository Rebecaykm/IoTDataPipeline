"""
Una base de datos en memoria que entiende las consultas de persistence.ordenes.

No pretende ser SQL Server: implementa exactamente las ocho consultas que el
recolector usa, con su misma semántica (el tope del UPDATE, el WHERE que impide
cerrar una orden a medias). A cambio, las pruebas pueden afirmar sobre el
ESTADO FINAL de los registros —"la orden quedó en 900 y Completada"— en vez de
sobre la forma del SQL, que es lo que de verdad importa en la planta.
"""

import copy
import datetime as _dt

from persistence import ordenes as ord_sql


def _fecha(valor):
    """
    Normaliza a 'AAAA-MM-DD'.

    El driver real acepta indistintamente date, datetime o texto; el doble
    tiene que ser igual de permisivo o inventa fallos que no existen.
    """
    if isinstance(valor, (_dt.date, _dt.datetime)):
        return valor.strftime("%Y-%m-%d")
    return str(valor)[:10]


class Registro:
    """Una fila de production_records."""

    def __init__(self, id, estacion, numero, orden=None, planned=0, produced=0,
                 status=ord_sql.PENDIENTE, planned_date="2026-08-19", shift=1,
                 created_at="2026-08-19 00:00:00", synced=0):
        self.id = id
        self.estacion = estacion
        self.numero = numero.replace(" ", "")
        self.orden = orden
        self.planned = planned
        self.produced = produced
        self.status = status
        self.planned_date = _fecha(planned_date)
        self.shift = shift
        self.created_at = created_at
        self.synced = synced
        self.production_start = None
        self.production_end = None

    @property
    def falta(self):
        return self.planned - self.produced

    def __repr__(self):
        return (f"Registro(id={self.id}, orden={self.orden!r}, "
                f"{self.produced}/{self.planned}, status={self.status})")


class BaseFalsa:
    """
    Cursor que opera sobre un diccionario de registros.

    `partes` es el catálogo: los (estación, número) que existen en part_numbers.
    Si una parte no está, el INSERT de No planeado no encuentra nada — igual que
    en la base real.
    """

    def __init__(self, registros=(), partes=None, multiplicadores=None):
        self.registros = {r.id: r for r in registros}
        self.partes = set(partes or [(r.estacion, r.numero) for r in registros])
        #: pieces_per_shot por (estacion, numero). Solo Estampado lo consulta.
        self.multiplicadores = dict(multiplicadores or {})
        self.historias = []          # [(id_registro, orden, cantidad)] del callback
        self.histories = []          # filas tal como entrarían a la tabla
        self.consultas = []          # SQL lanzado, para contar viajes a la base
        self.transiciones = []       # [(id, status_antes, status_despues)]
        self._filas = []
        self._siguiente_id = max([r.id for r in registros], default=0) + 1000

    # ── protocolo de cursor ───────────────────────────────────────────────
    def execute(self, sql, params=()):
        self.consultas.append(sql)
        self._filas = self._despachar(" ".join(str(sql).split()), tuple(params))
        self.rowcount = len(self._filas)
        return self

    def fetchone(self):
        return self._filas[0] if self._filas else None

    def fetchall(self):
        return list(self._filas)

    # ── despacho ──────────────────────────────────────────────────────────
    def _despachar(self, sql, p):
        if "AS falta" in sql:
            return self._capacidades(*p)
        if "produced_quantity + ? > planned_quantity" in sql:
            return self._sumar_a_orden(p[0], p[2], p[4])
        if "SET produced_quantity = produced_quantity + ?" in sql:
            return self._sumar_a_no_planeado(p[0], p[1], p[2], p[4])
        if f"status_id = {ord_sql.COMPLETADO}" in sql:
            return self._completar(p[1], p[0])
        if "UPDATE pr SET" in sql:
            return self._detener_estacion(p[1], p[0])
        if "SET status_id = CASE WHEN shop_order_number IS NULL" in sql:
            return self._detener(p[1], p[0])
        if "pr.shop_order_number IS NULL" in sql:
            return self._buscar_no_planeado(*p)
        if f"pr.status_id = {ord_sql.EN_PROGRESO}" in sql and "SELECT TOP(1)" in sql:
            return self._corrida_en_progreso(*p)
        if "INSERT INTO production_records" in sql:
            return self._crear_no_planeado(p[0], p[3], p[4], p[5])
        if "pieces_per_shot" in sql:
            return self._multiplicador(p[1], p[2])
        if "SELECT pn.id FROM part_numbers" in sql:
            return self._part_number_id(p[1], p[0])
        if "INSERT INTO histories" in sql:
            return self._insertar_history(sql, p)
        raise AssertionError(f"consulta no contemplada por la base falsa:\n{sql}")

    def _cambiar_status(self, registro, nuevo):
        """Deja rastro de cada cambio de estado, aunque se revierta después."""
        if registro.status != nuevo:
            self.transiciones.append((registro.id, registro.status, nuevo))
            registro.status = nuevo

    # ── implementación de cada consulta ───────────────────────────────────
    def _capacidades(self, estacion, numero, desde):
        vivas = [
            r for r in self.registros.values()
            if r.estacion == estacion
            and r.numero == numero.replace(" ", "")
            and (r.synced or 0) != 1
            and r.produced < r.planned
            and (r.status == ord_sql.EN_PROGRESO
                 or (r.status in (ord_sql.PENDIENTE, ord_sql.DETENIDO)
                     and r.orden is not None
                     and r.planned_date >= _fecha(desde)))
        ]
        vivas.sort(key=lambda r: (0 if r.status == ord_sql.EN_PROGRESO else 1,
                                  r.planned_date, r.shift, r.created_at, r.id))
        return [(r.id, r.orden, r.falta) for r in vivas[:ord_sql.MAX_ORDENES]]

    def _sumar_a_orden(self, piezas, fecha, id_registro):
        r = self.registros.get(id_registro)
        if r is None or r.status not in (ord_sql.PENDIENTE, ord_sql.EN_PROGRESO,
                                         ord_sql.DETENIDO):
            return []
        antes = r.produced
        r.produced = min(r.produced + piezas, r.planned)
        r.production_start = r.production_start or fecha
        r.production_end = fecha
        self._cambiar_status(r, ord_sql.EN_PROGRESO)
        return [(antes, r.produced, r.planned)]

    def _sumar_a_no_planeado(self, piezas, fecha_plan, fecha, id_registro):
        r = self.registros.get(id_registro)
        if (r is None or r.orden is not None
                or r.status not in (ord_sql.EN_PROGRESO, ord_sql.NO_PLANEADO)):
            return []
        r.produced += piezas
        r.planned_date = _fecha(fecha_plan)
        # El CASE del SQL lee el status ANTERIOR: si venía cerrado, esta suma
        # abre una corrida nueva y el inicio se mueve.
        if r.status == ord_sql.NO_PLANEADO or r.production_start is None:
            r.production_start = fecha
        r.production_end = fecha
        self._cambiar_status(r, ord_sql.EN_PROGRESO)
        return [(r.produced,)]

    def _completar(self, id_registro, fecha):
        r = self.registros.get(id_registro)
        if r is None or r.produced < r.planned:
            return []
        self._cambiar_status(r, ord_sql.COMPLETADO)
        r.production_end = fecha
        return [(r.id,)]

    def _detener(self, id_registro, fecha):
        r = self.registros.get(id_registro)
        if r is None or r.status != ord_sql.EN_PROGRESO:
            return []
        self._cambiar_status(
            r, ord_sql.NO_PLANEADO if r.orden is None else ord_sql.DETENIDO)
        r.production_end = fecha
        return [(r.id,)]

    def _detener_estacion(self, estacion, fecha):
        tocados = [r for r in self.registros.values()
                   if r.estacion == estacion and r.status == ord_sql.EN_PROGRESO]
        for r in tocados:
            self._cambiar_status(
                r, ord_sql.NO_PLANEADO if r.orden is None else ord_sql.DETENIDO)
            r.production_end = fecha
        return [(r.id,) for r in tocados]

    def _buscar_no_planeado(self, estacion, numero, desde):
        candidatos = [r for r in self.registros.values()
                      if r.estacion == estacion
                      and r.numero == numero.replace(" ", "")
                      and r.status in (ord_sql.EN_PROGRESO, ord_sql.NO_PLANEADO)
                      and r.orden is None
                      and r.planned_date >= _fecha(desde)]
        candidatos.sort(key=lambda r: r.id, reverse=True)
        return [(candidatos[0].id,)] if candidatos else []

    def _corrida_en_progreso(self, estacion, numero):
        return [(r.id,) for r in self.registros.values()
                if r.estacion == estacion
                and r.numero == numero.replace(" ", "")
                and r.status == ord_sql.EN_PROGRESO][:1]

    def _crear_no_planeado(self, turno, fecha_plan, numero, estacion):
        if (estacion, numero.replace(" ", "")) not in self.partes:
            return []
        nuevo = Registro(self._siguiente_id, estacion, numero, orden=None,
                         planned=0, produced=0, status=ord_sql.EN_PROGRESO,
                         planned_date=fecha_plan, shift=turno)
        self.registros[nuevo.id] = nuevo
        self._siguiente_id += 1
        return [(nuevo.id,)]

    def _multiplicador(self, numero, estacion):
        """El atributo pieces_per_shot; ausente significa 1 pieza por golpe."""
        valor = self.multiplicadores.get((estacion, numero.replace(" ", "")))
        return [(str(valor),)] if valor is not None else []

    def _part_number_id(self, estacion, numero):
        clave = (estacion, numero.replace(" ", ""))
        if clave not in self.partes:
            return []
        return [(abs(hash(clave)) % 100000,)]

    def _insertar_history(self, sql, p):
        """Guarda la fila tal como quedaría en histories, con sus columnas."""
        columnas = sql[sql.index("(") + 1:sql.index(")")].split(", ")
        self.histories.append(dict(zip(columnas, p)))
        return []

    # ── ayudas para las pruebas ───────────────────────────────────────────
    def anotar_history(self, id_registro, orden, cantidad):
        """Se pasa como callback a registrar_incremento()."""
        self.historias.append((id_registro, orden, cantidad))

    def __getitem__(self, id_registro):
        return self.registros[id_registro]

    @property
    def acumuladores(self):
        """Registros sin orden, vivos o ya cerrados como No planeado."""
        return [r for r in self.registros.values() if r.orden is None]

    @property
    def no_planeados(self):
        """Solo los que ya quedaron en No planeado (corrida terminada)."""
        return [r for r in self.registros.values() if r.status == ord_sql.NO_PLANEADO]

    def reclamar_para_orden(self, id_registro, orden, piezas):
        """
        Simula la pasada horaria que asigna órdenes a producción ya registrada.

        Toma del acumulador lo que la orden necesita:
          - si se lleva TODO, el registro se convierte en el de esa orden
          - si SOBRA, se crea un registro aparte para la orden y el acumulador
            conserva el resto
          - si FALTA, la orden nace Pendiente con lo que había, y el recolector
            le llenará lo que reste

        Devuelve (piezas_tomadas, id_del_registro_de_la_orden).
        """
        r = self.registros[id_registro]
        toma = min(piezas, r.produced)
        r.produced -= toma

        if r.produced == 0 and toma == piezas:
            # Cuadró exacto: el acumulador ES el registro de la orden.
            r.orden = orden
            r.planned = piezas
            r.produced = toma
            self._cambiar_status(r, ord_sql.COMPLETADO)
            return toma, r.id

        # Sobró acumulado, o no alcanzó: la orden va en su propio registro.
        nuevo_id = self._siguiente_id
        self._siguiente_id += 1
        registro = Registro(nuevo_id, r.estacion, r.numero, orden=orden,
                            planned=piezas, produced=toma,
                            status=ord_sql.COMPLETADO if toma == piezas
                                   else ord_sql.PENDIENTE,
                            planned_date=r.planned_date)
        registro.production_start = r.production_start
        registro.production_end = r.production_end
        self.registros[nuevo_id] = registro
        return toma, nuevo_id

    def total_producido(self):
        return sum(r.produced for r in self.registros.values())

    def total_conteo(self):
        """Lo que pasó por el callback anotar_history, en la unidad del área."""
        return sum(c for _, _, c in self.historias)

    def conteo_en_histories(self):
        """Lo que acabó como filas de la tabla histories."""
        return sum(h["quantity"] for h in self.histories)


class ConexionFalsa:
    """
    La conexión que devuelve create_connection() en las pruebas.

    Tiene transacción de verdad —snapshot al abrir el cursor, restaurar en
    rollback— porque sin eso no se puede comprobar lo que importa de un
    interbloqueo: que al revertirse NO queda nada escrito a medias y que el
    reintento no duplica lo que el primer intento alcanzó a aplicar.
    """

    class _Contexto:
        def __init__(self, conexion):
            self.conexion = conexion

        def __enter__(self):
            self.conexion._abrir_transaccion()
            return self.conexion.base

        def __exit__(self, *_):
            return False

    def __init__(self, base):
        self.base = base
        self.commits = 0
        self.rollbacks = 0
        self._respaldo = None

    def cursor(self):
        return self._Contexto(self)

    def _abrir_transaccion(self):
        if self._respaldo is None:
            self._respaldo = copy.deepcopy((
                self.base.registros, self.base.historias, self.base.histories,
                self.base.transiciones, self.base._siguiente_id,
            ))

    def commit(self):
        self.commits += 1
        self._respaldo = None

    def rollback(self):
        self.rollbacks += 1
        if self._respaldo is None:
            return
        (self.base.registros, self.base.historias, self.base.histories,
         self.base.transiciones, self.base._siguiente_id) = self._respaldo
        self._respaldo = None
