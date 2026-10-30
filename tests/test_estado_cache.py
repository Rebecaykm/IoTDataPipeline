"""
El archivo de estado: qué se guarda y qué se descarta.

Sin este archivo, al arrancar no se sabe desde dónde contar y se pierde la
producción del hueco. Lo que NO debe pasar es que arrastre campos del modelo
anterior, porque confunden a quien abre el JSON a diagnosticar.
"""

import json
import time

from persistence import estado as estado_store

IP = "10.1.1.8"


def registro(**extra):
    base = {
        "contador_registro": 1152,
        "multiplicador": 4,
        "numero_original": "VA4070/71361",
        "lado": "LH",
        "id_en_curso": 232224,
    }
    base.update(extra)
    return base


class TestGuardado:
    def test_guarda_solo_lo_que_se_usa(self, tmp_path):
        archivo = tmp_path / "state.json"
        estado_store.guardar_estado(archivo, {"600T_X_LH": registro()}, IP)

        guardado = json.loads(archivo.read_text(encoding="utf-8"))["600T_X_LH"]
        assert sorted(guardado) == ["contador_registro", "id_en_curso", "lado",
                                    "multiplicador", "numero_original"]

    def test_no_inventa_id_registro(self):
        """El modelo anterior metía un id_registro=0 fantasma."""
        assert "id_registro" in estado_store.OBSOLETOS

    def test_la_escritura_es_atomica(self, tmp_path):
        """Nunca debe quedar un JSON a medias: o el viejo entero o el nuevo entero."""
        archivo = tmp_path / "state.json"
        estado_store.guardar_estado(archivo, {"a": registro()}, IP)
        estado_store.guardar_estado(archivo, {"b": registro(contador_registro=9)}, IP)

        assert list(json.loads(archivo.read_text(encoding="utf-8"))) == ["b"]
        assert not (tmp_path / "state.json.tmp").exists()


class TestCarga:
    def test_conserva_la_linea_base(self, tmp_path):
        archivo = tmp_path / "state.json"
        archivo.write_text(json.dumps({"600T_X_LH": registro()}), encoding="utf-8")

        cargado = estado_store.cargar_estado(archivo, IP)
        assert cargado["600T_X_LH"]["contador_registro"] == 1152
        assert cargado["600T_X_LH"]["id_en_curso"] == 232224

    def test_descarta_los_campos_del_modelo_anterior(self, tmp_path):
        """
        Un archivo escrito antes del cambio a órdenes. La línea base se respeta
        —es lo que evita recontar producción— y lo demás se tira.
        """
        viejo = {
            "2500T  TR_DGH970051_LH": {
                "id_registro": 232224,
                "quantity_planeada": 0,
                "multiplicador": 1,
                "contador_registro": 1152,
                "hora_cambio": "13:40:51",
                "numero_original": "DGH9 70 051",
                "lado": "LH",
                "necesita_production_start": False,
                "error_bd": None,
                "registro_creado": True,
                "delta_inicial": 0,
            }
        }
        archivo = tmp_path / "state.json"
        archivo.write_text(json.dumps(viejo), encoding="utf-8")

        cargado = estado_store.cargar_estado(archivo, IP)["2500T  TR_DGH970051_LH"]
        assert cargado["contador_registro"] == 1152, "la línea base NO se toca"
        assert cargado["multiplicador"] == 1
        for obsoleto in estado_store.OBSOLETOS:
            assert obsoleto not in cargado

    def test_el_archivo_queda_limpio_al_siguiente_guardado(self, tmp_path):
        archivo = tmp_path / "state.json"
        archivo.write_text(json.dumps({
            "MK05_DA6A5361YA_LH": {"contador_registro": 5, "numero_original": "DA6A5361YA",
                                   "id_registro": 99, "hora_cambio": "10:00:00"}
        }), encoding="utf-8")

        estado_store.guardar_estado(archivo, estado_store.cargar_estado(archivo, IP), IP)

        guardado = json.loads(archivo.read_text(encoding="utf-8"))["MK05_DA6A5361YA_LH"]
        assert guardado["contador_registro"] == 5
        assert guardado["numero_original"] == "DA6A5361YA"
        for obsoleto in estado_store.OBSOLETOS:
            assert obsoleto not in guardado


class TestVistoEn:
    """
    Cuándo se vio por última vez cada parte. Decide si al desaparecer se
    conserva su línea base o se olvida.
    """

    def test_a_un_archivo_viejo_se_le_pone_la_hora_de_arranque(self, tmp_path):
        """
        Sin esto valdría 0 —o sea, ausente desde 1970— y el primer hueco del
        PLC borraría TODAS las líneas base que se acaban de recuperar.
        """
        archivo = tmp_path / "state.json"
        archivo.write_text(json.dumps({
            "MK05_DA6A5361YA_LH": {"contador_registro": 5,
                                   "numero_original": "DA6A5361YA"}
        }), encoding="utf-8")

        antes = time.time()
        cargado = estado_store.cargar_estado(archivo, IP)["MK05_DA6A5361YA_LH"]

        assert cargado["visto_en"] >= antes

    def test_respeta_el_que_ya_traia(self, tmp_path):
        archivo = tmp_path / "state.json"
        archivo.write_text(json.dumps({
            "MK05_DA6A5361YA_LH": {"contador_registro": 5,
                                   "numero_original": "DA6A5361YA",
                                   "visto_en": 1_700_000_000.0}
        }), encoding="utf-8")

        cargado = estado_store.cargar_estado(archivo, IP)["MK05_DA6A5361YA_LH"]

        assert cargado["visto_en"] == 1_700_000_000.0

    def test_sin_archivo_arranca_vacio(self, tmp_path):
        assert estado_store.cargar_estado(tmp_path / "no_existe.json", IP) == {}

    def test_un_json_corrupto_se_preserva_en_vez_de_pisarse(self, tmp_path):
        archivo = tmp_path / "state.json"
        archivo.write_text("{esto no es json", encoding="utf-8")

        assert estado_store.cargar_estado(archivo, IP) == {}
        assert not archivo.exists(), "se renombró"
        assert list(tmp_path.glob("*.corrupto-*")), "y se conservó para revisión"
