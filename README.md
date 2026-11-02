# bóveda — respaldo lógico de Couchbase Capella

Herramienta de respaldo y restauración para una colección de **Couchbase
Capella**, con respaldo completo e **incremental por `CAS`**, simulación de
desastre y restauración. El respaldo lo ejecuta un **cron de GitHub Actions**
y se publica como asset de una *release*.

> Actividad Grupal 3 — Base de Datos II
> Universidad Privada de Tacna · Ing. Patrick Jose Cuadros Quiroga
> Luis Fernando Vilca Barrientos

| | |
|---|---|
| **App en vivo** | `https://boveda.onrender.com` |
| **Base respaldada** | Capella, `vitrina._default.productos` (la de [vitrina-couchbase](https://github.com/FeRnAn101002/vitrina-couchbase)) |
| **Catálogo de respaldos** | [Releases de este repositorio](../../releases) |
| **Automatización** | GitHub Actions: cron diario + despliegue en Render |

---

## El problema

El plan gratuito de Capella **no incluye `cbbackupmgr`**, la herramienta
nativa de respaldo de Couchbase. Igual que en Cassandra, la única vía es un
**respaldo lógico**: leer por el SDK y escribir a disco.

Pero los problemas que aparecen **no son los mismos**, y esa comparación es lo
que hace interesante el ejercicio.

### 1. Aquí el incremental sí ahorra lectura

Cada documento de Couchbase lleva un **`CAS`** (compare-and-swap), un número
que el servidor incrementa en cada escritura. Y SQL++ **deja filtrar por él en
el `WHERE`**:

```sql
SELECT META(p).id AS _id, META(p).cas AS _cas, p.*
FROM `vitrina`.`_default`.`productos` AS p
WHERE META(p).cas > $desde
ORDER BY META(p).cas
```

El servidor devuelve solo lo que cambió. Medido sobre la colección real:

```
RESPALDO COMPLETO       11 documentos de 11
INCREMENTAL después      0 documentos devueltos por el servidor
```

Cero documentos devueltos, no «cero guardados de once leídos». Es una
diferencia de fondo con un motor de familia wide-column, donde el
`WRITETIME` existe pero no se puede filtrar y hay que leer todo igual.

### 2. Restaurar invalida tu línea base

El `CAS` **lo asigna el servidor** y no se puede escribir. Al restaurar, cada
documento recibe uno nuevo y mayor. Resultado: el siguiente incremental cree
que todo cambió.

Medido:

```
CAS máximo antes de restaurar:  1792878215037124608
CAS máximo después:             1793579136874577920
incremental desde la línea base vieja → 11 documentos
```

Once documentos, es decir, la colección entera. **Después de cualquier
restauración hay que tomar un respaldo completo nuevo**; el incremental
anterior ya no sirve de punto de partida. La herramienta lo avisa:

```json
"nota": "el CAS lo asigna el servidor: tras restaurar, el proximo
         incremental vera todos los documentos como nuevos"
```

### 3. Lo borrado reaparece

Un export lógico no guarda *tombstones*. Si restauras sin más, los documentos
eliminados vuelven a la vida. Por eso existe el **modo espejo**, que además
borra lo que está en la colección y no en el respaldo. Es destructivo y por
eso no es el modo por defecto.

### 4. Tampoco hay foto de un instante

La colección sigue aceptando escrituras mientras se exporta. Es la misma
limitación que en Cassandra: un respaldo lógico nunca es un snapshot.

---

## Qué hace la herramienta

| Acción | Qué ocurre |
|---|---|
| Respaldo completo | Exporta la colección a JSONL con `META().id` y `META().cas` |
| Respaldo incremental | Mismo export con `WHERE META(p).cas > $desde` |
| Provocar desastre | Borra documentos de verdad |
| Restaurar | `upsert` de cada documento por su id |
| Restaurar en espejo | Además elimina lo que el respaldo no contiene |

El paquete es un `.tar.gz` con `productos.jsonl` y un `manifiesto.json` con
`sha256`, conteos y el `hasta_cas` que sirve de línea base.

## La automatización

[`.github/workflows/respaldo.yml`](.github/workflows/respaldo.yml) **es** la
estrategia de respaldo:

```
cron 06:30 UTC
   ├─ ejecuta scripts/respaldar.py contra Capella
   └─ publica el .tar.gz como asset de una release
```

Nadie abre un panel ni ejecuta nada a mano. El catálogo durable son las
*releases*, y el panel web las lee desde la API pública de GitHub.

[`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml) despliega: lint →
pruebas → `docker build` → deploy hook de Render → verificación de `/health`.
Render tiene `autoDeploy: false`, así que el único camino a producción es el
pipeline. Y la verificación exige que `/health` responda
`"backend":"couchbase"`, no solo un `200`.

## Ejecutar en local

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements-dev.txt

cp .env.example .env     # endpoint, usuario y contraseña de Capella
uvicorn app.main:app --reload
```

Respaldo desatendido desde la línea de comandos:

```bash
python scripts/respaldar.py
python scripts/respaldar.py --incremental-desde 1792878215037124608
```

Las 10 pruebas corren **sin credenciales**: un cluster falso imita el SDK y
asigna los `CAS` como lo haría el servidor, lo que permite comprobar incluso
el problema de la línea base invalidada.

```bash
pytest -v
ruff check app tests scripts
```

## Variables de entorno

| Variable | Descripción |
|---|---|
| `CB_CONNECTION_STRING` | `couchbases://cb.xxxx.cloud.couchbase.com` |
| `CB_USERNAME` | Usuario de base de datos de Capella |
| `CB_PASSWORD` | Su contraseña |
| `CB_BUCKET` / `CB_SCOPE` / `CB_COLLECTION` | Por defecto `vitrina` / `_default` / `productos` |
| `GH_REPO` | Repositorio cuyas releases forman el catálogo |

> Capella exige lista de IPs permitidas. Render no da IP fija en el plan
> gratuito, así que hay que permitir `0.0.0.0/0`. Es una concesión real.

## Estructura

```
app/
  respaldo.py   exportar, restaurar, espejo, desastre
  main.py       panel web
scripts/
  respaldar.py  respaldo desatendido, el que ejecuta el cron
tests/          10 pruebas con cluster falso, sin red
.github/workflows/
  ci-cd.yml     lint, pruebas, docker build, deploy
  respaldo.yml  cron diario -> release
```

## Licencia

MIT.
