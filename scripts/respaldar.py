"""Respaldo desatendido. Lo ejecuta GitHub Actions con cron.

    python scripts/respaldar.py [--incremental-desde <cas>]

Escribe el paquete en respaldos/ y deja en respaldos/ultimo.json los datos
que el workflow necesita para publicar la release.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.respaldo import conectar, estado, exportar  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incremental-desde", type=int, default=None)
    parser.add_argument("--etiqueta", default="programado")
    args = parser.parse_args()

    ajustes = get_settings()
    if not ajustes.configured:
        print("ERROR: faltan CB_CONNECTION_STRING, CB_USERNAME o CB_PASSWORD",
              file=sys.stderr)
        return 1

    cluster = conectar(ajustes)
    e = estado(cluster, ajustes)
    print(f"conectado a {ajustes.bucket}.{ajustes.scope}.{ajustes.collection}")
    print(f"  documentos:  {e['documentos']}")
    print(f"  cas maximo:  {e['cas_maximo']}")

    tipo = "incremental" if args.incremental_desde else "completo"
    marca = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    destino = pathlib.Path("respaldos") / f"{tipo}-{marca}.tar.gz"

    manifiesto = exportar(
        cluster, ajustes, destino,
        desde_cas=args.incremental_desde, etiqueta=args.etiqueta,
    )

    print()
    print(f"paquete: {destino}  ({manifiesto['bytes_paquete']} bytes)")
    print(f"  documentos guardados: {manifiesto['documentos']}")
    print(f"  hasta_cas:            {manifiesto['hasta_cas']}")

    pathlib.Path("respaldos/ultimo.json").write_text(json.dumps({
        "archivo": str(destino),
        "nombre": destino.name,
        "tipo": tipo,
        "momento": manifiesto["momento"],
        "hasta_cas": manifiesto["hasta_cas"],
        "documentos": manifiesto["documentos"],
    }, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
