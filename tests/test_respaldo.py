"""Pruebas del motor de respaldo.

Un cluster falso imita lo justo del SDK y de SQL++ para ejercitar la
exportacion, el incremental por CAS, la restauracion y el modo espejo. Asi el
pipeline valida la logica sin credenciales de Capella ni red.
"""

from __future__ import annotations

import tarfile

import pytest

from app.config import Settings
from app.respaldo import (
    estado,
    exportar,
    leer_manifiesto,
    provocar_desastre,
    restaurar,
)

AJUSTES = Settings(
    connection_string="couchbases://falso",
    username="u",
    password="p",
    bucket="vitrina",
    scope="_default",
    collection="productos",
    gh_repo="FeRnAn101002/boveda",
)


class ColeccionFalsa:
    def __init__(self, cluster):
        self.cluster = cluster

    def upsert(self, ident, cuerpo):
        self.cluster.cas += 1
        self.cluster.docs[ident] = {"cuerpo": dict(cuerpo), "cas": self.cluster.cas}

    def remove(self, ident):
        from couchbase.exceptions import DocumentNotFoundException

        if ident not in self.cluster.docs:
            raise DocumentNotFoundException()
        del self.cluster.docs[ident]


class ClusterFalso:
    """El CAS lo asigna este 'servidor', igual que hace Couchbase."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.cas = 1000
        self.leidos_ultima_consulta = 0

    def guardar(self, ident, cuerpo):
        self.cas += 1
        self.docs[ident] = {"cuerpo": dict(cuerpo), "cas": self.cas}

    def bucket(self, _):
        return self

    def scope(self, _):
        return self

    def collection(self, _):
        return ColeccionFalsa(self)

    def query(self, sentencia, opciones=None):
        parametros = getattr(opciones, "_params", {}) or {}
        s = sentencia.strip()

        # El GROUP BY va primero: esa consulta tambien lleva COUNT(*).
        if "GROUP BY" in s:
            cuenta: dict[str, int] = {}
            for d in self.docs.values():
                cat = d["cuerpo"].get("categoria", "sin")
                cuenta[cat] = cuenta.get(cat, 0) + 1
            return [{"categoria": k, "n": v} for k, v in
                    sorted(cuenta.items(), key=lambda x: -x[1])]
        if "COUNT(*)" in s:
            return [{"n": len(self.docs)}]
        if "MAX(META(p).cas)" in s:
            return [{"maximo": max((d["cas"] for d in self.docs.values()), default=0)}]
        if "RAW META(p).id" in s:
            ids = list(self.docs)
            if "LIMIT" in s:
                ids = ids[: parametros.get("n", len(ids))]
            return ids
        if "OBJECT_NAMES" in s:
            campos = set()
            for d in self.docs.values():
                campos.update((d["cuerpo"].get("atributos") or {}).keys())
            return sorted(campos)
        # SELECT de documentos, con o sin filtro por CAS
        desde = parametros.get("desde")
        filas = []
        for ident, d in sorted(self.docs.items(), key=lambda x: x[1]["cas"]):
            if desde is not None and d["cas"] <= desde:
                continue
            filas.append({"_id": ident, "_cas": d["cas"], **d["cuerpo"]})
        self.leidos_ultima_consulta = len(filas)
        return filas


class OpcionesFalsas:
    def __init__(self, named_parameters=None):
        self._params = named_parameters or {}


@pytest.fixture(autouse=True)
def parchear_opciones(monkeypatch):
    import couchbase.options

    monkeypatch.setattr(couchbase.options, "QueryOptions", OpcionesFalsas)


@pytest.fixture
def cluster():
    c = ClusterFalso()
    c.guardar("producto::1", {"nombre": "Laptop", "categoria": "computo",
                              "precio": 2300, "atributos": {"ram_gb": 16}})
    c.guardar("producto::2", {"nombre": "Bicicleta", "categoria": "deporte",
                              "precio": 1400, "atributos": {"talla": "M"}})
    c.guardar("producto::3", {"nombre": "Clean Code", "categoria": "libros",
                              "precio": 145, "atributos": {"paginas": 464}})
    return c


def test_respaldo_completo_guarda_todos_los_documentos(cluster, tmp_path):
    m = exportar(cluster, AJUSTES, tmp_path / "c.tar.gz", etiqueta="prueba")

    assert m["tipo"] == "completo"
    assert m["documentos"] == 3
    assert m["documentos_en_la_base"] == 3
    assert m["sha256"]


def test_el_incremental_lo_filtra_el_servidor(cluster, tmp_path):
    """La diferencia con Cassandra: aqui el WHERE sobre la version funciona."""
    completo = exportar(cluster, AJUSTES, tmp_path / "c.tar.gz")

    inc = exportar(cluster, AJUSTES, tmp_path / "i.tar.gz",
                   desde_cas=completo["hasta_cas"])

    assert inc["documentos"] == 0
    # el servidor no devolvio nada: no hubo lectura que descartar
    assert cluster.leidos_ultima_consulta == 0


def test_el_incremental_recoge_solo_lo_que_cambio(cluster, tmp_path):
    completo = exportar(cluster, AJUSTES, tmp_path / "c.tar.gz")
    cluster.guardar("producto::4", {"nombre": "Monitor", "categoria": "computo",
                                    "precio": 650, "atributos": {"pulgadas": 24}})

    inc = exportar(cluster, AJUSTES, tmp_path / "i.tar.gz",
                   desde_cas=completo["hasta_cas"])

    assert inc["documentos"] == 1


def test_restaurar_devuelve_los_documentos_borrados(cluster, tmp_path):
    paquete = tmp_path / "c.tar.gz"
    exportar(cluster, AJUSTES, paquete)

    provocar_desastre(cluster, AJUSTES, 2)
    assert len(cluster.docs) == 1

    r = restaurar(cluster, AJUSTES, paquete)
    assert r["escritos"] == 3
    assert len(cluster.docs) == 3


def test_restaurar_invalida_la_linea_base_incremental(cluster, tmp_path):
    """El CAS lo asigna el servidor: tras restaurar, todo parece nuevo."""
    paquete = tmp_path / "c.tar.gz"
    completo = exportar(cluster, AJUSTES, paquete)
    base = completo["hasta_cas"]

    restaurar(cluster, AJUSTES, paquete)

    inc = exportar(cluster, AJUSTES, tmp_path / "i.tar.gz", desde_cas=base)
    assert inc["documentos"] == 3


def test_sin_espejo_lo_borrado_reaparece(cluster, tmp_path):
    paquete = tmp_path / "c.tar.gz"
    exportar(cluster, AJUSTES, paquete)

    cluster.guardar("producto::9", {"nombre": "Intruso", "categoria": "otros",
                                    "precio": 1, "atributos": {}})

    restaurar(cluster, AJUSTES, paquete, espejo=False)
    assert len(cluster.docs) == 4

    r = restaurar(cluster, AJUSTES, paquete, espejo=True)
    assert r["borrados"] == 1
    assert len(cluster.docs) == 3


def test_el_manifiesto_avisa_de_los_tombstones(cluster, tmp_path):
    paquete = tmp_path / "c.tar.gz"
    exportar(cluster, AJUSTES, paquete)
    assert "tombstones" in leer_manifiesto(paquete)["aviso"]


def test_el_paquete_lleva_documentos_y_manifiesto(cluster, tmp_path):
    paquete = tmp_path / "c.tar.gz"
    exportar(cluster, AJUSTES, paquete)
    with tarfile.open(paquete) as tar:
        assert set(tar.getnames()) == {"manifiesto.json", "productos.jsonl"}


def test_el_estado_describe_la_coleccion(cluster):
    e = estado(cluster, AJUSTES)

    assert e["documentos"] == 3
    assert e["cas_maximo"] > 0
    assert {c["categoria"] for c in e["categorias"]} == {"computo", "deporte", "libros"}
    assert e["atributos"] == ["paginas", "ram_gb", "talla"]


def test_el_desastre_borra_solo_lo_pedido(cluster):
    d = provocar_desastre(cluster, AJUSTES, 2)

    assert d["borrados"] == 2
    assert len(cluster.docs) == 1
