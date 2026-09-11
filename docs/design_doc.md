# Design

The architecture and the decisions behind it. §1–3 are the brief; §4 onwards
record what was built.

## 1. Principles
1. **The database stores results, never mutable rules.** Dedup, splitting and
   conflict resolution are pure Python; changing a rule never needs a migration.
2. **Start simple, upgrade on evidence.** A spec is a flat list of steps in YAML;
   caching and normalisation wait for a real pain point.
3. **Exploration and record are separate.** Users work in a mutable, reversible
   session; the spec compiled out of it is the record.
4. **Provenance is precise to the step.** The same image may be dropped for
   different reasons in different rounds, so "why" is bound to a step.
5. **Splits are deterministic and patient-level.** Seeded hashing on
   `subject_id`, so one patient never straddles train and val.
6. **Gaps in external input are reported, never skipped.** Every matched and
   missing name of an imported list is recorded.
7. **Every interface shares one API.** The CLI and a notebook are skins over
   `ManualSetSession`; the logic exists once.

## 2. Layers
```
Layer 4  Interfaces     cli/main.py · cli/repl.py (cxr explore) · cli/meta.py
Layer 3  Exploration    session/builder.py (ManualSetSession) · session/analyzer.py
Layer 2  Spec           core/schema.py · core/ops.py (one pure function per op)
                        · core/engine.py (execute and write)
Layer 1  Persistence    db/schema.sql · db/models.py · db/crud.py · storage.py
```

Users touch Layers 3 and 4; Layer 2 is the artifact worth reviewing; Layer 1
holds no business logic. Dependencies run inward, `cli/` → `session/` → `core/`
→ `db/`, with `settings.py` and `storage.py` as leaves. The one exception is
`db/crud.py` re-running a spec for `cxr why`.

## 3. Spec language
A spec is a list of named steps with dependencies, mostly linear. A step's id is
how later steps refer to it and how `cxr why` reports it.

| op | input | purpose |
|---|---|---|
| `source` | none (leaf) | a whole image or annotation batch, annotations included |
| `import_list` | none (leaf) | an external list of file names |
| `filter` | one | narrow the current result |
| `union` / `intersect` / `except` | many | combine candidate sets |
| `dedup` | one | drop duplicates by `blake3_hash`; prefers the annotated copy |
| `category_map` | one | local category → target category |
| `conflict_resolve` | one | settle contradictory annotations on one image |
| `manual_override` | one | include or exclude one id, with a reason |

`filter` criteria:

- **`sample`** — `hash(seed + key) % mod`, keeping `keep_remainder`. The key is
  `subject_id`; images without one fall back to `image_id`, and the build says
  how many.
- **`predicate`** — an expression over image fields and the image's current
  annotations (`labels`, `targets`, `annotators`, `n_annotations`). The AST is
  walked against a whitelist, never evaluated: a spec is data, not code.
- **`balance`** — cap each class at `max_per_class`. Quotas fill rarest class
  first, preferring images already chosen, since one multi-label image fills
  several quotas. Selection is seeded like `sample`.
- **`explicit_list`** — narrow to named files already present; `on_missing` is
  `error`, `warn` or `ignore`.
- **`annotated`** — drop unannotated images explicitly.

Lists longer than `INLINE_LIST_MAX` (200) are stored in
`manual_set_import_lists` by content hash and referenced as
`file_names_ref: {sha256, source}`, so a spec stays short and portable.

```yaml
steps:
  - {id: aws, op: source, original_set: aws_images, annotation_batch: V2}
  - {id: tb,  op: source, original_set: TB-portal, annotation_batch: V1}
  - {id: pooled, op: union, inputs: [aws, tb]}
  - {id: deduped, op: dedup, input: pooled, source_priority: [TB-portal, aws_images]}
  - {id: train, op: filter, input: deduped, criterion: sample,
     key_field: subject_id, mod: 4, keep_remainder: [0, 1, 2], seed: split-2026}
  - {id: mapped, op: category_map, input: train, mapping: {
      aws_images@V2: {Pneumonia: pneumonia, Normal: normal},
      TB-portal@V1: {TB: tb, Normal: normal}}}
  - {id: resolved, op: conflict_resolve, input: mapped, rules: [
      {rule: annotator_precedence, annotator_precedence: [radiologist_senior]},
      {rule: highest_score}]}
final: resolved
```

The session mirrors these ops plus `preview`, `checkpoint`, `rollback`, `undo`,
`checkout`, `compile` and `commit`. `preview()` returns statistics, never rows;
its per-target distribution is computed independently of `cxr show`'s SQL, and a
test asserts the two agree.

## 4. Module responsibilities
| Module | Owns | Explicitly does not |
|---|---|---|
| `db/schema.sql` | Tables, composite FKs, deferred triggers | Any business rule |
| `db/models.py` · `db/engine.py` | ORM mapping, connections, applying the schema | Queries |
| `db/crud.py` | Read-side queries, statistics, deletion | Building a manual-set |
| `storage.py` | Object keys and access: images, spec.yaml, `__meta__.md` | Knowing what they mean |
| `core/schema.py` | Spec syntax and the step dependency graph | Anything needing the database |
| `core/types.py` | `CandidateSet`, `Catalog` (metadata cache) | Business rules |
| `core/ops.py` | Every rule: dedup, split, mapping, conflict resolution | DB writes, I/O, global state |
| `core/predicate.py` | Safe evaluation of `filter` expressions | `eval` |
| `core/engine.py` | Running steps; writing the version, spec and meta | Deciding what a step means |
| `core/export.py` | COCO / CSV / zip / manual-set parquet | Re-deriving anything |
| `core/meta.py` | The three `__meta__.md` templates: counts in, markdown out | Asking, storing |
| `session/builder.py` | Accumulating steps, checkpoint/rollback, compile | Persisting exploration |
| `session/analyzer.py` | Preview statistics | Mutating state |
| `cli/*` | Parsing, formatting, asking | Business logic |
| `seed.py` · `scripts/tools/` | Mock data; importers and one-off utilities | Anything the runtime imports |

Every op is `(catalog, inputs, step) -> StepResult`: no writes, no global state.

## 5. Data flow
```
explore / notebook     steps accumulate in memory only
        │  preview() · checkpoint() · rollback() · undo() · checkout()
     compile()         a clean spec; abandoned branches are already gone
        │
      commit() ──────► build()
                         ├─ execute_spec()     ops in declared order
                         ├─ spec.yaml          → object storage
                         ├─ manual_set_* rows  (flushed, not committed)
                         ├─ __meta__.md        → beside spec.yaml
                         └─ commit
```

`CandidateSet` holds only id sets and the accumulated category mapping; metadata
comes from `Catalog`, which loads each batch once. Its one invariant — every
annotation's image is in the set, restored by `prune()` — is why the composite
foreign keys never reject a commit.

## 6. Design decisions
**The spec is a file, not a row.** It is read, edited, diffed and versioned, so
it lives in object storage at `manual-sets/{name}/annotations/{version}/spec.yaml`,
a location computed from (name, version). The database keeps only its sha256, to
tell whether two versions share a recipe and to catch a spec changed afterwards.
The object is written before the transaction commits: a failed commit leaves an
unreferenced file, the reverse order would leave an unreproducible version.

**A spec has three operations: save, view, load.** Save writes the file
directly, view renders it wrapped, load reads a file or a `name@V1` ref. stdout
is a display channel; redirecting it once silently truncated a spec.

**Every batch and version carries a `__meta__.md`.** Whatever can be counted
comes from the database; only what a person knows is asked (`cxr meta`, or at
commit). A commit asks after the selection is flushed and before it commits, so
the statistics are final and a failing build asks nothing. It is documentation:
no column refers to it.

**Conflicts are grouped by image, not by (image, target).** Grouped by target,
A saying *normal* and B saying *pneumonia* look like two consistent groups.
Comparing each source's label *set* per image exposes them as contradictions.
Resolution picks a winning *source* per image, never a mix of labels. Detection
boxes are excluded: their disagreement is geometric.

**Dedup lets the user choose.** It discards duplicate images *and* their
annotations, so `duplicates` shows each copy and `keep` pins winners; unpinned
groups prefer the annotated copy, then source priority. Annotations are never
moved to the survivor — a composite FK ties each to its own image.

**A manual-set may not contain unannotated images.** It is training-ready. A
deferred trigger enforces this, `build()` checks first to name the count and the
fix, and `filter criterion=annotated` drops them explicitly. For the same reason
`source --image` and `import_list` bring every annotation on their images;
disagreements are `conflict_resolve`'s job.

**Exploration is permissive, the compiled spec is strict.** `map_category` maps
one batch at a time and must not drop the rest; `compile()` tightens the last
`category_map` to `require_total` and the last `conflict_resolve` to `strict`, so
anything undecided fails and is named.

**Provenance is recomputed, not stored.** How a version was built is a pure
function of (spec, data), so `cxr why` re-runs the spec and watches membership
change. Stored rulings and step results were tried and removed: they outgrew the
datasets and drifted from the rules. The cost is one build per `cxr why`.

**`manual_override` names an id; everything else is a rule.** It is the escape
hatch for judgements — an unusable film, a withdrawn consent — made reproducible
with a `reason`. Everything is addressed by id, since file names are unique only
within a batch. No override may silently do nothing.

**Deletion is the one exception to immutability.** `cxr rm` shows what will be
lost, needs `--yes` unattended, and never touches source data (CASCADE on
membership, RESTRICT on originals). Objects go after the transaction succeeds.

**Version conflicts are resolved optimistically.** `UNIQUE (manual_set_id,
version)` is the guarantee; the pre-flight check only gives a friendlier message.
The builder is recorded as plain name/email columns, not as an annotator.

**A build is one transaction.** Any failure rolls it back, leaving no version;
`--dry-run` writes nothing, object storage included.

## 7. Guarantees, and where they are enforced
| Guarantee | Enforced by |
|---|---|
| An annotation's image is in the same version | Composite FKs, upheld by `prune()` |
| A category is never dangling | Deferred constraint trigger |
| Every manual-set image is annotated | Deferred constraint trigger + check in `build()` |
| Versions are immutable | `UNIQUE (manual_set_id, version)` |
| A split never separates a patient | `hash(seed + subject_id) % mod` |
| The same spec yields the same set | Seeded hashing, sorted iteration, id tie-breaks |
| No annotation ships without a label name | `_assert_annotations_are_mapped()` |
| Unresolvable conflicts stop the build | `strict=True` on the compiled `conflict_resolve` |
| POS + NEG + UNKNOWN equals the image count | One cls row per (image, category); `verify.sql` |
| A batch never holds the same annotation twice | `UNIQUE`, `NULLS NOT DISTINCT` for det |
| A spec.yaml cannot be swapped unnoticed | `spec_sha256`, re-checked on every read |

`scripts/verify.sql` re-checks these in SQL, bypassing the Python;
`scripts/acceptance.sh` rebuilds everything and runs the end-to-end checks.

## 8. Testing
198 tests against real PostgreSQL and MinIO — SQLite has none of the deferred
triggers, composite FKs, `ARRAY` or `JSONB` the guarantees rest on.

| File | Covers |
|---|---|
| `test_spec.py` · `test_predicate.py` | Syntax; malformed specs and code injection refused |
| `test_ops.py` | The rules — the behavioural contract |
| `test_engine.py` | Reproducibility, transactions, provenance, spec integrity |
| `test_session.py` · `test_repl.py` | Exploration, and explore → save → build round trips |
| `test_cli.py` | Every command executes and fails cleanly |
| `test_import_lists.py` · `test_meta.py` | Long lists; `__meta__.md` templates |
| `test_import_image_batch.py` | The image importer against the real bucket |

Interface code has failed where unit tests could not see (`fixed_issues.md`), so
output paths are tested as output paths. Interactive prompts need a real pty;
under pytest they take their defaults.

## 9. Known limitations
- Exploration state lives in the process; `save` / `load` carry it across.
- Before `conflict_resolve`, `preview` can show POS + NEG above the image count.
- Nothing shows a film; see `TODO.md`. No YOLO export.
- Nothing sweeps object storage for missing or edited spec.yaml files (`fsck`).
- Tested up to ~1,000 images; `Catalog` holds every touched batch in memory.
- No access control; name and email are attribution, not authentication.
