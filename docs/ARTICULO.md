---
title: "Respaldar Couchbase sin cbbackupmgr: el incremental que sí funciona y la trampa del CAS"
published: false
description: "Capella en plan gratuito no incluye la herramienta nativa de respaldo. Construí la alternativa y la medí: aquí el incremental sí ahorra lectura, pero restaurar invalida la línea base y el siguiente incremental se lleva la colección entera."
tags: couchbase, nosql, devops, backup
cover_image: ""
---

Couchbase tiene una herramienta de respaldo excelente. Se llama
`cbbackupmgr`, hace respaldos completos e incrementales reales, los almacena
en un repositorio con histórico y restaura con granularidad fina.

No la puedes usar en el plan gratuito de Couchbase Capella.

Capella es Couchbase gestionado: no hay nodos a los que entrar, no hay
binarios que ejecutar, y la función de backup del servicio pertenece a los
planes de pago. Lo que tienes es un endpoint, un usuario de base de datos y
SQL++. Punto.

Así que construí la alternativa —un respaldo lógico— y la medí contra la
colección real de un proyecto anterior. Esperaba encontrarme los mismos
problemas que en un motor wide-column. Encontré otros, y la comparación
resultó ser lo más interesante del ejercicio.

- **Repositorio:** https://github.com/FeRnAn101002/boveda
- **Panel en vivo:** https://boveda-w0l1.onrender.com
- **Colección respaldada:** la de https://github.com/FeRnAn101002/vitrina-couchbase

---

## Qué es un respaldo lógico aquí

Leer la colección por SQL++ y escribir una línea JSON por documento, más un
manifiesto con el `sha256`, todo en un `.tar.gz`:

```
respaldo-20251101-075130.tar.gz
├── manifiesto.json
└── productos.jsonl
```

Cada línea del `.jsonl` lleva el cuerpo del documento más dos metadatos que
el servidor expone:

```sql
SELECT META(p).id AS _id, META(p).cas AS _cas, p.*
FROM `vitrina`.`_default`.`productos` AS p
ORDER BY META(p).cas
```

`META().id` es la clave del documento. `META().cas` es el **CAS**
(compare-and-swap): un número de 64 bits que el servidor incrementa en cada
escritura. Es el protagonista de todo lo que viene después.

## Lo bueno: aquí el incremental sí ahorra lectura

Este fue el hallazgo que no esperaba, y se entiende mejor por contraste.

En Cassandra existe `WRITETIME`, que te dice cuándo se escribió una columna.
Pero **no se puede poner en el `WHERE`**: no es una columna, es metadato de
celda. Un respaldo incremental allí tiene que leer la tabla entera y
descartar en el cliente. Ahorra disco, no ahorra lectura.

En Couchbase el `CAS` sí se puede filtrar:

```sql
SELECT META(p).id AS _id, META(p).cas AS _cas, p.*
FROM `vitrina`.`_default`.`productos` AS p
WHERE META(p).cas > $desde
ORDER BY META(p).cas
```

Lo filtra el servidor. Medido sobre la colección real, un completo seguido de
un incremental inmediato:

```
RESPALDO COMPLETO      11 documentos de 11
INCREMENTAL después     0 documentos devueltos por el servidor
```

Cero devueltos. No «cero guardados de once leídos»: cero transferidos, cero
documentos materializados. Es una diferencia de fondo entre los dos motores,
y decide si un incremental nocturno tiene sentido o es un respaldo completo
disfrazado.

El manifiesto guarda el corte para la próxima vez:

```json
"hasta_cas": 1792878215037124608
```

## Lo malo: restaurar invalida tu línea base

Y aquí viene la trampa.

El `CAS` **lo asigna el servidor**. No es un campo que puedas escribir, no
hay forma de preservarlo. Cuando restauras un documento, recibe un CAS nuevo
y mayor, porque para Couchbase es una escritura más.

Consecuencia: después de restaurar, tu último `hasta_cas` deja de significar
nada. Medido:

```
CAS máximo antes de restaurar:   1792878215037124608
CAS máximo después:              1793579136874577920
incremental desde la línea base vieja  ->  11 documentos
```

Once documentos: la colección entera. El incremental cree, correctamente
según sus reglas, que todo cambió.

Esto no es un bug que se pueda arreglar. Es una propiedad del motor, y la
regla operativa que se deduce es concreta: **después de cualquier
restauración hay que tomar un respaldo completo nuevo**, porque la cadena de
incrementales anterior ya no engancha. La herramienta lo devuelve en el
resultado de la restauración para que no haya que recordarlo:

```json
"nota": "el CAS lo asigna el servidor: tras restaurar, el proximo
         incremental vera todos los documentos como nuevos"
```

Es un detalle pequeño que cambia por completo el calendario de respaldos. Si
restauras un martes y tu cron sigue haciendo incrementales desde el domingo,
el miércoles te llevas la colección completa sin saber por qué.

## Lo que comparten los dos motores: lo borrado resucita

Un export lógico guarda lo que el `SELECT` devuelve, y un `SELECT` nunca
devuelve documentos borrados. Así que el respaldo **no tiene constancia de
los borrados**.

Si restauras un paquete tomado antes de una limpieza, los documentos
eliminados vuelven. Es el mismo problema que en Cassandra con los tombstones,
por la misma razón: el respaldo describe lo que había, no lo que se quitó.

Por eso hay dos modos de restauración:

| Modo | Qué hace |
|---|---|
| Normal | `upsert` de cada documento del respaldo. Lo demás se queda. |
| Espejo | `upsert`, **y además elimina** lo que está en la colección y no en el respaldo. |

El modo espejo deja la colección idéntica al respaldo. Es destructivo y por
eso no es el predeterminado. La elección no es técnica: depende de si quieres
*recuperar algo perdido* o *volver a un estado conocido*.

## Y tampoco hay foto de un instante

La colección sigue aceptando escrituras mientras se exporta. Igual que en
Cassandra, el paquete contiene *lo que existió en algún momento del barrido*,
no el estado de un segundo concreto. El `ORDER BY META(p).cas` hace el
recorrido determinista, pero no lo congela.

El manifiesto lo dice:

```json
"aviso": "Respaldo logico: no guarda tombstones, asi que restaurar sin
          modo espejo resucita los documentos borrados."
```

## La prueba: romper la colección a propósito

Un respaldo que nunca se restauró es un archivo, no un respaldo. La
herramienta tiene un botón que provoca un desastre real —borra documentos— y
el ciclo completo se midió así:

```
Estado inicial     11 documentos, 21 atributos distintos
Respaldo completo  11 documentos en el paquete
Desastre            3 documentos borrados  ->  8
Restauración       escritos=11
Estado final       11 documentos
```

Y justo después, la comprobación que da sentido al artículo: un incremental
desde la línea base previa a la restauración devolvió los 11. El ciclo de
recuperación funciona; la cadena de incrementales, no.

## El esquema flexible también es un problema de respaldo

La colección de productos usa un subdocumento `atributos` distinto por
categoría: una laptop tiene `ram_gb`, una bicicleta tiene `talla`, un libro
tiene `paginas`. Es la gracia de un motor de documentos.

Para el respaldo significa que no hay un esquema que validar. Lo que sí se
puede hacer es **medir la forma de los datos** en el momento de la copia:

```sql
SELECT DISTINCT RAW campo
FROM `vitrina`.`_default`.`productos` AS p
UNNEST OBJECT_NAMES(p.atributos) AS campo
ORDER BY campo
```

Veintiún atributos distintos entre once documentos. Ese número, guardado
junto al respaldo, es la forma más barata de detectar que algo cambió de raíz
entre dos copias.

## La estrategia es el workflow, no el panel

Hay un panel web para ver el estado y lanzar las operaciones a mano, y sirve
para enseñar el mecanismo. Pero ningún respaldo real depende de que alguien
abra una página.

La estrategia es un archivo YAML:

```yaml
on:
  schedule:
    - cron: "30 6 * * *"   # 06:30 UTC = 01:30 en Perú
```

Cada noche, GitHub Actions ejecuta el respaldo contra Capella y publica el
`.tar.gz` **como asset de una release**, con el `hasta_cas` en las notas para
que un incremental posterior tenga de dónde partir.

Las releases son durables, versionadas, descargables por URL y viven fuera
del proveedor de base de datos y fuera del proveedor de hosting. Si mañana
desaparecen Capella y Render, los respaldos siguen ahí. El panel no los
guarda: los lee desde la API pública de GitHub.

El despliegue sigue la misma lógica: `autoDeploy: false` en Render, así que
el único camino a producción es el pipeline, y la verificación final no se
conforma con un `200`:

```bash
if echo "$respuesta" | grep -q '"backend":"couchbase"'; then
  echo "Conectado a Capella."
  exit 0
fi
echo "ERROR: responde, pero sin conexion a Couchbase."
exit 1
```

La aplicación tiene un backend en memoria para que las pruebas corran sin
credenciales. Sin esa comprobación, un despliegue con las credenciales mal
puestas queda verde, con HTTP 200 y una página que funciona, hablando con una
base de datos que no existe.

## Las pruebas corren sin credenciales

Las 10 pruebas usan un cluster falso que imita lo justo del SDK y de SQL++, y
que **asigna los CAS como lo haría el servidor**. Eso permite comprobar en CI
el problema central del artículo sin tocar Capella:

```python
def test_restaurar_invalida_la_linea_base_incremental(cluster, tmp_path):
    completo = exportar(cluster, AJUSTES, paquete)
    base = completo["hasta_cas"]

    restaurar(cluster, AJUSTES, paquete)

    inc = exportar(cluster, AJUSTES, otro, desde_cas=base)
    assert inc["documentos"] == 3   # todos: la linea base ya no sirve
```

Una prueba que afirma un comportamiento incómodo vale más que un comentario
en el README, porque si alguien lo «arregla» sin entenderlo, el pipeline se
queja.

## Un detalle de infraestructura que conviene decir

Capella exige una lista de IPs permitidas. Render, en plan gratuito, no da IP
fija. La consecuencia práctica es que hay que permitir `0.0.0.0/0`, es decir,
desde cualquier parte.

Es una concesión real y no me parece honesto esconderla. Se mitiga con un
usuario de base de datos de permisos acotados y rotación de contraseña, pero
la superficie de red queda abierta. En un entorno serio, la respuesta es una
IP fija de salida, que es exactamente una de las cosas por las que se paga un
plan superior.

## Lo que me llevo

El respaldo no es una funcionalidad, es un conjunto de garantías. En Couchbase
gestionado, sin `cbbackupmgr`, las mías son estas:

- Tengo una copia verificable por `sha256` de todos los documentos visibles.
- El incremental por `CAS` **sí** ahorra lectura: lo filtra el servidor.
- **No** tengo consistencia de un instante.
- Restaurar **invalida** la línea base: después hay que hacer un completo.
- Los borrados no están en el respaldo; recuperarlos exige el modo espejo.

Comparado con un motor wide-column, el incremental es mejor y la cadena de
incrementales es más frágil. Ninguna de las dos cosas se ve leyendo la
documentación: se ven midiendo, y por eso construir la herramienta enseñó
bastante más que leer sobre ella.

---

*Actividad Grupal 3 — Base de Datos II · Universidad Privada de Tacna ·
Ing. Patrick Jose Cuadros Quiroga · Luis Fernando Vilca Barrientos*
