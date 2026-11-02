"""Motor de respaldo y restauracion para Couchbase Capella.

Por que este archivo existe
---------------------------
Capella es Couchbase gestionado y el plan gratuito no incluye
`cbbackupmgr`, que es la herramienta nativa. Igual que en Cassandra, la
unica via es un **respaldo logico**: leer por el SDK y escribir a disco.

Pero los problemas son distintos, y ahi esta lo interesante:

1. **El incremental SI ahorra lectura.** Cada documento lleva un `CAS`
   (compare-and-swap), un numero que crece en cada escritura. Y SQL++ deja
   filtrar por el en el `WHERE`. En Cassandra, el `WRITETIME` existe pero no
   se puede filtrar: hay que leer todo igual.

2. **Restaurar invalida tu linea base.** El `CAS` lo asigna el servidor; no
   se puede escribir. Al restaurar, cada documento recibe un `CAS` nuevo y
   mayor, asi que el siguiente incremental cree que todo cambio y vuelve a
   copiarlo entero.

3. **Lo borrado reaparece.** Un export logico no guarda tombstones. Si
   restauras sin modo espejo, los documentos eliminados vuelven a la vida.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings

VERSION_FORMATO = 1
ARCHIVO_DOCS = "productos.jsonl"


# ----------------------------------------------------------------- conexion

def conectar(s: Settings):
    from datetime import timedelta

    from couchbase.auth import PasswordAuthenticator
    from couchbase.cluster import Cluster
    from couchbase.options import ClusterOptions

    opciones = ClusterOptions(PasswordAuthenticator(s.username, s.password))
    opciones.apply_profile("wan_development")
    cluster = Cluster(s.connection_string, opciones)
    cluster.wait_until_ready(timedelta(seconds=25))
    return cluster


def _coleccion(cluster, s: Settings):
    return cluster.bucket(s.bucket).scope(s.scope).collection(s.collection)


def _consultar(cluster, sentencia: str, **parametros):
    from couchbase.options import QueryOptions

    return list(cluster.query(sentencia, QueryOptions(named_parameters=parametros)))


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for trozo in iter(lambda: f.read(65536), b""):
            h.update(trozo)
    return h.hexdigest()


# -------------------------------------------------------------- exportacion

def exportar(cluster, s: Settings, destino: Path, desde_cas: int | None = None,
             etiqueta: str = "") -> dict:
    """Exporta la coleccion a un paquete .tar.gz.

    Si viene `desde_cas`, el filtro va en el WHERE y el servidor devuelve
    solo lo que cambio: el incremental ahorra lectura de verdad.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    momento = datetime.now(UTC)
    tipo = "incremental" if desde_cas else "completo"

    if desde_cas:
        sentencia = (
            f"SELECT META(p).id AS _id, META(p).cas AS _cas, p.* "
            f"FROM {s.fuente} AS p WHERE META(p).cas > $desde ORDER BY META(p).cas"
        )
        filas = _consultar(cluster, sentencia, desde=desde_cas)
    else:
        sentencia = (
            f"SELECT META(p).id AS _id, META(p).cas AS _cas, p.* "
            f"FROM {s.fuente} AS p ORDER BY META(p).cas"
        )
        filas = _consultar(cluster, sentencia)

    total_en_base = _consultar(
        cluster, f"SELECT COUNT(*) AS n FROM {s.fuente}"
    )[0]["n"]

    with tempfile.TemporaryDirectory() as tmp:
        carpeta = Path(tmp)
        archivo = carpeta / ARCHIVO_DOCS
        corte = desde_cas or 0

        with archivo.open("w", encoding="utf-8") as f:
            for fila in filas:
                corte = max(corte, int(fila.get("_cas", 0)))
                f.write(json.dumps(fila, ensure_ascii=False) + "\n")

        manifiesto = {
            "formato": VERSION_FORMATO,
            "tipo": tipo,
            "etiqueta": etiqueta,
            "momento": momento.isoformat(),
            "bucket": s.bucket,
            "scope": s.scope,
            "coleccion": s.collection,
            "desde_cas": desde_cas,
            "hasta_cas": corte,
            "documentos": len(filas),
            "documentos_en_la_base": int(total_en_base),
            "sha256": _sha256(archivo),
            "aviso": (
                "Respaldo logico: no guarda tombstones, asi que restaurar sin "
                "modo espejo resucita los documentos borrados."
            ),
        }
        (carpeta / "manifiesto.json").write_text(
            json.dumps(manifiesto, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        with tarfile.open(destino, "w:gz") as tar:
            for f in sorted(carpeta.iterdir()):
                tar.add(f, arcname=f.name)

    manifiesto["archivo"] = destino.name
    manifiesto["bytes_paquete"] = destino.stat().st_size
    return manifiesto


def leer_manifiesto(paquete: Path) -> dict:
    with tarfile.open(paquete, "r:gz") as tar:
        return json.loads(tar.extractfile("manifiesto.json").read().decode("utf-8"))


def _documentos(paquete: Path) -> list[dict]:
    with tarfile.open(paquete, "r:gz") as tar:
        if ARCHIVO_DOCS not in tar.getnames():
            return []
        crudo = tar.extractfile(ARCHIVO_DOCS).read().decode("utf-8")
    return [json.loads(linea) for linea in crudo.splitlines() if linea.strip()]


# ------------------------------------------------------------ restauracion

def restaurar(cluster, s: Settings, paquete: Path, espejo: bool = False) -> dict:
    """Vuelca el paquete sobre la coleccion.

    El `CAS` del respaldo NO se puede reescribir: lo asigna el servidor. Por
    eso tras restaurar hay que tomar un respaldo completo nuevo; el
    incremental anterior ya no sirve de linea base.
    """
    coleccion = _coleccion(cluster, s)
    docs = _documentos(paquete)

    escritos = 0
    for doc in docs:
        ident = doc["_id"]
        cuerpo = {k: v for k, v in doc.items() if k not in ("_id", "_cas")}
        coleccion.upsert(ident, cuerpo)
        escritos += 1

    borrados = 0
    if espejo:
        en_respaldo = {d["_id"] for d in docs}
        actuales = _consultar(
            cluster, f"SELECT RAW META(p).id FROM {s.fuente} AS p"
        )
        from couchbase.exceptions import DocumentNotFoundException

        for ident in actuales:
            if ident not in en_respaldo:
                try:
                    coleccion.remove(ident)
                    borrados += 1
                except DocumentNotFoundException:
                    pass

    return {
        "escritos": escritos,
        "borrados": borrados,
        "espejo": espejo,
        "nota": (
            "el CAS lo asigna el servidor: tras restaurar, el proximo "
            "incremental vera todos los documentos como nuevos"
        ),
        "manifiesto": leer_manifiesto(paquete),
    }


# ------------------------------------------------------------------ estado

def estado(cluster, s: Settings) -> dict:
    total = _consultar(cluster, f"SELECT COUNT(*) AS n FROM {s.fuente}")[0]["n"]
    cas = _consultar(
        cluster, f"SELECT MAX(META(p).cas) AS maximo FROM {s.fuente} AS p"
    )[0]["maximo"]
    categorias = _consultar(
        cluster,
        f"SELECT p.categoria, COUNT(*) AS n FROM {s.fuente} AS p "
        "GROUP BY p.categoria ORDER BY n DESC",
    )
    atributos = _consultar(
        cluster,
        f"SELECT DISTINCT RAW campo FROM {s.fuente} AS p "
        "UNNEST OBJECT_NAMES(p.atributos) AS campo ORDER BY campo",
    )
    return {
        "documentos": int(total),
        "cas_maximo": int(cas or 0),
        "categorias": categorias,
        "atributos": atributos,
    }


def provocar_desastre(cluster, s: Settings, cuantos: int) -> dict:
    """Borra documentos de verdad, para poder enseñar la restauracion."""
    from couchbase.exceptions import DocumentNotFoundException

    coleccion = _coleccion(cluster, s)
    ids = _consultar(
        cluster, f"SELECT RAW META(p).id FROM {s.fuente} AS p LIMIT $n", n=cuantos
    )
    borrados = 0
    for ident in ids:
        try:
            coleccion.remove(ident)
            borrados += 1
        except DocumentNotFoundException:
            pass
    return {"borrados": borrados}
