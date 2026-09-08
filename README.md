# CXR Dataset Manager

Assemble chest X-ray images and annotations scattered across separate
original-sets into training datasets that are **reproducible, traceable, and
free of patient-level leakage**.

Building such a set means combining sources, dropping duplicate films, unifying
inconsistent label names (`Pneumonia` / `PNEU` / `pneumonia`), and settling cases
where a junior radiologist said *normal* and a senior said *pneumonia*.
Afterwards, nobody can usually answer these:

| Question | Answer |
|---|---|
| Why is this image in the set? | `cxr why name@V1 --image aws_images/V1/AWS_00344.png` |
| Do train and val share a patient? | `cxr check-leakage train@V1 val@V1` |
| Can I rebuild this in three months? | Yes — the spec is stored; splits use seeded hashing |
| What changed between versions? | `cxr diff name@V1 name@V2` |
| Which annotations were dropped, why? | Recorded per entity; `cxr why` reads it back |

## Setup
```bash
docker compose -f docker/docker-compose.yml up -d   # postgres + minio
# no Docker? ./scripts/dev.sh start — same ports, same credentials
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
cxr db init --drop && cxr db seed   # schema + mock data with real problems in it
cxr explore my_dataset
```

The mock data is deliberately messy: 60 cross-source duplicate images, 253 images
labelled by more than one batch (many disagreeing), seven inconsistent category
namespaces, ~8% of images with no `subject_id`.

## Exploring — `cxr explore`

An interactive session; nothing reaches the database until you commit.

```
$ cxr explore pneumonia_v5

cxr(pneumonia_v5 empty)> source aws_images@V1 --annotation
  290 img · 290 cls · 0 det   head=source_1
cxr(pneumonia_v5 290img)> source TB-portal@V1 --annotation
cxr(pneumonia_v5 155img)> union
  445 img · 445 cls · 76 det
cxr(pneumonia_v5 445img)> dedup TB-portal,aws_images
  ⚠  step 'dedup_1': removed 4 annotations attached to duplicate images
  441 img · 441 cls · 76 det
cxr(pneumonia_v5 441img)> checkpoint after_dedup
cxr(pneumonia_v5 441img)> split --mod 4 --keep 0,1,2 --seed s1
cxr(pneumonia_v5 333img)> categories      # which local categories are unmapped
cxr(pneumonia_v5 333img)> conflicts       # show contradictory annotations
cxr(pneumonia_v5 333img)> rollback after_dedup    # not happy? go back
cxr(pneumonia_v5 441img)> save pneumonia_v5.yaml
cxr(pneumonia_v5 441img)> commit -m pneumonia -v V5 --dry-run
```

Other commands: `import` `filter` `pick` `intersect` `except` `map` `resolve`
`exclude` `preview` `steps` `images` `batches` `undo` `checkout` `spec` `load`;
`help <command>` explains each. Every `source` opens a branch — `checkout
<step>` moves between them. Leaving discards the session; `save` or `commit` it.

## Running and inspecting

```bash
cxr ls batches                    # how to name sources in a spec
cxr ls categories                 # local category namespaces per batch
cxr ls manual-sets                # existing datasets and versions
cxr ls history                    # every version and the spec that made it

cxr validate spec.yaml            # syntax only, no database access
cxr build spec.yaml -m name -v V1 [--dry-run]   # --dry-run writes nothing

cxr show name@V1                  # composition, sources, category mapping
cxr spec name@V1                  # the spec that produced it (save before rm!)
cxr why name@V1 --image aws_images/V1/AWS_00344.png
cxr diff name@V1 name@V2
cxr check-leakage train@V1 val@V1
cxr export name@V1 -f zip -o ./out   # COCO json + manifest csv + spec
cxr rm name@V1                       # delete a version

cxr lists add picks.txt              # register a long file-name list, get a sha256
```

Lists over 200 names are stored in the database and referenced from the spec by
`sha256`, so a spec stays ten lines long even with ten thousand file names.
`import` and `pick` in the REPL do this for you.

Prefer `--dry-run` first: it prints every step's counts and warnings without
touching the database. `CXR_DEBUG=1` gives tracebacks.

## Verifying
```bash
python -m pytest tests/ -q                        # 101 tests
psql -h localhost -p 5433 -U postgres -d cxr -f scripts/verify.sql
./scripts/acceptance.sh                           # rebuilds and checks everything
```

`scripts/verify.sql` bypasses all Python and recomputes the guarantees straight
from the data, so a bug in the logic cannot make its own checks pass.
Architecture: `docs/design_doc.md`.
