"""Panel web de boveda: respaldo y restauracion de Couchbase Capella."""

from __future__ import annotations

import pathlib
import tempfile
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.respaldo import (
    conectar,
    estado,
    exportar,
    leer_manifiesto,
    provocar_desastre,
    restaurar,
)

settings = get_settings()
app = FastAPI(
    title="boveda — respaldo de Couchbase Capella",
    description=(
        "Respaldo logico completo e incremental por CAS de una coleccion "
        "Couchbase, con restauracion y simulacion de desastre."
    ),
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
plantillas = Jinja2Templates(directory="app/templates")

CARPETA = pathlib.Path("respaldos")
CARPETA.mkdir(exist_ok=True)

_cluster = None
_error = ""


def cluster():
    global _cluster, _error
    if _cluster is None and settings.configured:
        try:
            _cluster = conectar(settings)
            _error = ""
        except Exception as exc:  # noqa: BLE001
            _error = str(exc)
    return _cluster


def _locales() -> list[dict]:
    paquetes = []
    for p in sorted(CARPETA.glob("*.tar.gz"), reverse=True):
        try:
            m = leer_manifiesto(p)
        except Exception:  # noqa: BLE001
            continue
        paquetes.append({
            "archivo": p.name,
            "tipo": m.get("tipo"),
            "momento": m.get("momento", "")[:19].replace("T", " "),
            "documentos": m.get("documentos", 0),
            "hasta": m.get("hasta_cas"),
            "bytes": p.stat().st_size,
        })
    return paquetes


def _releases() -> list[dict]:
    url = f"https://api.github.com/repos/{settings.gh_repo}/releases"
    try:
        r = httpx.get(url, timeout=12, headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return []
        salida = []
        for rel in r.json()[:12]:
            for asset in rel.get("assets", []):
                if asset["name"].endswith(".tar.gz"):
                    salida.append({
                        "nombre": asset["name"],
                        "url": asset["browser_download_url"],
                        "bytes": asset["size"],
                        "momento": rel.get("published_at", "")[:19].replace("T", " "),
                        "etiqueta": rel.get("tag_name", ""),
                    })
        return salida
    except Exception:  # noqa: BLE001
        return []


@app.get("/health")
def health() -> JSONResponse:
    c = cluster()
    return JSONResponse({
        "status": "ok",
        "backend": "couchbase" if c else "sin conexion",
        "bucket": settings.bucket,
        "error": _error,
    })


@app.get("/", response_class=HTMLResponse)
def panel(request: Request):
    c = cluster()
    return plantillas.TemplateResponse(
        request=request,
        name="panel.html",
        context={
            "estado": estado(c, settings) if c else None,
            "conectado": bool(c),
            "error": _error,
            "locales": _locales(),
            "releases": _releases(),
            "repo": settings.gh_repo,
            "fuente": f"{settings.bucket}.{settings.scope}.{settings.collection}",
            "aviso": request.query_params.get("aviso", ""),
        },
    )


@app.post("/respaldo")
def crear_respaldo(tipo: str = Form("completo"), desde: str = Form("")):
    c = cluster()
    if not c:
        return RedirectResponse("/?aviso=sin-conexion", status_code=303)

    marca = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    desde_cas = int(desde) if tipo == "incremental" and desde.strip().isdigit() else None
    exportar(c, settings, CARPETA / f"{tipo}-{marca}.tar.gz",
             desde_cas=desde_cas, etiqueta="desde el panel")
    return RedirectResponse(f"/?aviso=respaldo-{tipo}", status_code=303)


@app.post("/desastre")
def desastre(cuantos: int = Form(3)):
    c = cluster()
    if not c:
        return RedirectResponse("/?aviso=sin-conexion", status_code=303)
    d = provocar_desastre(c, settings, max(1, min(cuantos, 50)))
    return RedirectResponse(f"/?aviso=borrados-{d['borrados']}", status_code=303)


@app.post("/restaurar")
def restaurar_local(archivo: str = Form(...), espejo: str = Form("")):
    c = cluster()
    if not c:
        return RedirectResponse("/?aviso=sin-conexion", status_code=303)
    ruta = CARPETA / archivo
    if not ruta.exists():
        return RedirectResponse("/?aviso=no-existe", status_code=303)
    restaurar(c, settings, ruta, espejo=bool(espejo))
    return RedirectResponse("/?aviso=restaurado", status_code=303)


@app.post("/restaurar-release")
def restaurar_release(url: str = Form(...)):
    c = cluster()
    if not c:
        return RedirectResponse("/?aviso=sin-conexion", status_code=303)
    if not url.startswith(f"https://github.com/{settings.gh_repo}/"):
        return RedirectResponse("/?aviso=url-no-permitida", status_code=303)

    with httpx.Client(follow_redirects=True, timeout=60) as cliente:
        r = cliente.get(url)
        r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        f.write(r.content)
        temporal = pathlib.Path(f.name)

    restaurar(c, settings, temporal, espejo=False)
    temporal.unlink(missing_ok=True)
    return RedirectResponse("/?aviso=restaurado-desde-release", status_code=303)


@app.get("/descargar/{archivo}")
def descargar(archivo: str):
    ruta = (CARPETA / archivo).resolve()
    if not ruta.is_file() or CARPETA.resolve() not in ruta.parents:
        return JSONResponse({"detail": "no encontrado"}, status_code=404)
    return FileResponse(ruta, filename=archivo, media_type="application/gzip")


@app.get("/api/estado")
def api_estado() -> JSONResponse:
    c = cluster()
    return JSONResponse({
        "conectado": bool(c),
        "estado": estado(c, settings) if c else None,
        "respaldos_locales": len(_locales()),
        "respaldos_en_releases": len(_releases()),
    })
