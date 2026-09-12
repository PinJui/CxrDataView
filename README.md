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
| Why is this image in the set? | `cxr why name@V1 --image <path>`, or `cxr image 315` for its annotations, lineage and duplicates |
| Do train and val share a patient? | `cxr check-leakage train@V1 val@V1` |
| Can I rebuild this in three months? | Yes — every version keeps its spec; splits use seeded hashing |
| What changed between versions? | `cxr diff name@V1 name@V2` |
| Which annotations were dropped, why? | `cxr why` re-runs the spec and reports each step's verdict |

## Setup
```bash
colima start                        # macOS only: the Linux VM docker runs in
docker compose -f docker/docker-compose.yml up -d postgres minio   # 5433 · 9010
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
cxr db init --drop && cxr db seed   # schema + mock data with real problems in it
cxr explore my_dataset
```

The mock data is deliberately messy: 60 cross-source duplicates, 253 images
labelled by several batches, seven inconsistent category namespaces, ~8% with no `subject_id`.

## Importing an original-set
```bash
python scripts/tools/import_image_batch.py -d <dir> -s <set> -v V1
cxr meta images <set>@V1          # its __meta__.md, stored in MinIO beside the images
python scripts/tools/import_annotation_batch.py -d <dir> -s <set> -v V1
cxr meta annotations <set>@V1
```
Counts and distributions come from the database; you answer the rest. A file
already registered is handled by `--on-conflict`, one only in the bucket by
`--on-image-exists` (`skip` / `overwrite`; both default to `error`).

## Exploring — `cxr explore`
An interactive session; nothing reaches the database until you commit.
```
cxr(pneumonia_v5 [NO IMGS NOW])> source aws_images@V1 --annotation
  290 img · 290 cls · 0 det   head=source_1
cxr(pneumonia_v5 290img)> source TB-portal@V1 --annotation
cxr(pneumonia_v5 155img)> union
cxr(pneumonia_v5 445img)> dedup TB-portal,aws_images
  ⚠  step 'dedup_1': dedup also removed 4 annotations attached to duplicate images
cxr(pneumonia_v5 441img)> checkpoint after_dedup
cxr(pneumonia_v5 441img)> split --mod 4 --keep 0,1,2 --seed s1
cxr(pneumonia_v5 333img)> filter 'Pneumonia' in labels   # filter by annotation
cxr(pneumonia_v5 333img)> rollback after_dedup    # not happy? go back
cxr(pneumonia_v5 441img)> commit -m pneumonia -v V5 --dry-run
```

Other commands: `import` `pick` `balance` `intersect` `except` `map` `resolve`
`conflicts` `include` `exclude` `preview` `steps` `images` `duplicates` `undo`
`checkout` `save` `load`; `help <command>` explains each. `save` keeps a spec
without committing; leaving discards the session. A real `commit` also asks for
the version's `__meta__.md` and stores it beside the spec.

## Running and inspecting
```bash
cxr ls batches                    # how to name sources in a spec
cxr ls manual-sets                # existing datasets; ls history, ls categories
cxr validate spec.yaml            # syntax only, no database
cxr build spec.yaml -m name -v V1 [--dry-run]   # or build name@V1 to reuse a recipe

cxr show name@V1                  # composition, sources, category distribution
cxr spec name@V1 [-o out.yaml]    # view the spec that made it, or save it (before rm!)
cxr why name@V1 --image aws_images/V1/AWS_00344.png   # cxr image 315 for one film
cxr diff name@V1 name@V2          # cxr check-leakage train@V1 val@V1
cxr export name@V1 -f zip -o ./out                 # COCO json + manifest csv + spec
cxr export name@V1 -f parquet -o ChestDatasetsRoot  # manual-set parquet + __meta__.md
cxr meta manual-set name@V1 --view                 # --yes rewrites it
cxr rm name@V1                       # delete a version
cxr lists add picks.txt              # register a long file-name list
```

A spec has three operations: **save** (`-o`, or `save` in the REPL), **view**
(`cxr spec name@V1`), **load** (`cxr build` or `load`, from a file or a `name@V1`
ref) — never redirect output into a file, that is the display channel. Each
version keeps its spec and `__meta__.md` at `<name>/annotations/<version>/` in the
manual-sets bucket. Prefer `--dry-run` first; `CXR_DEBUG=1` gives tracebacks.

## Verifying
```bash
python -m pytest tests/ -q                        # 221 tests
docker exec -i local-postgres psql -U postgres -d cxr < scripts/verify.sql
./scripts/acceptance.sh                           # rebuilds and checks everything
```

`scripts/verify.sql` bypasses all Python and recomputes the guarantees straight
from the data, so a bug in the logic cannot make its own checks pass. In `docs/`:
`design_doc.md` (architecture), `TODO.md`, `issues.md` (open), `fixed_issues.md`.
