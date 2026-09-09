# Design

The architecture and how the code is divided. Sections 1–3 are the design brief
(what a spec means, why the layers exist); 4 onwards record what was actually
built and where it departs from the brief.

## 1. Principles
1. **The database stores results, never mutable business rules.** Dedup,
   splitting and conflict resolution are pure Python; changing a rule must never
   require a schema migration.
2. **Start simple, upgrade on evidence.** A spec is a flat list of steps in a
   YAML file; content-addressed normalisation waits for a real pain point.
3. **Exploration and record are separate.** Users work against a mutable,
   reversible session; the spec compiled out of it is the record — in git, and
   beside the version in object storage.
4. **Provenance is precise to the step.** The same image may be dropped for
   different reasons in different rounds, so "why" is answerable only when bound
   to a specific step.
5. **Splits are deterministic and patient-level.** Seeded hashing keyed on
   `subject_id`, so one patient's films never straddle a train/val boundary.
6. **Gaps in external input are reported, never skipped.** An imported file list
   records every matched and missing name so it can be fixed in one pass.
7. **Every interface shares one API.** The CLI and a notebook are skins over
   `ManualSetSession`; the logic exists once.

## 2. Layers
```
Layer 4  Interfaces     cli/main.py (run specs, query) · cli/repl.py
                        (cxr explore) · a notebook driving the session
Layer 3  Exploration    session/builder.py (ManualSetSession)
         mutable, reversible      session/analyzer.py (preview summaries)
Layer 2  Spec           core/schema.py (definition) · core/ops.py (one pure
         immutable               function per op) · core/engine.py (execute)
Layer 1  Persistence    db/schema.sql (the data model) · db/models.py
                        (mapping) · db/crud.py (reads) · storage.py (objects)
```

Users only ever interact with Layer 3 or 4. Layer 2 is the compiled artifact
worth reviewing and committing to git; Layer 1 holds no business logic. The
schema alone answers "how is the result stored" but not: how does a user
*describe* a combination of batches without looking up ids; how is that
derivation recorded so it can be reproduced; and how does someone build it by
trial and error, since nobody writes a correct spec first try.

Dependencies run inward, `cli/` → `session/` → `core/` → `db/`, with
`settings.py` and `storage.py` as leaves importing nothing above them; the
exception is `db/crud.py` reaching back into `core` to re-run a spec for
`cxr why`, a read path that is arguably a missing service layer.

## 3. Spec language
A spec is a list of steps with dependencies — mostly linear, occasionally
branching where two sources merge. Each step names itself, and that name is how
later steps refer to it in `input` and how `cxr why` reports the step.

| op | input | purpose |
|---|---|---|
| `source` | none (leaf) | pull in a whole image or annotation batch, annotations included |
| `import_list` | none (leaf) | pull in an external list of file names |
| `filter` | one | narrow the current result |
| `union` / `intersect` / `except` | many | combine candidate sets |
| `dedup` | one | drop duplicates by `blake3_hash`; prefers the annotated copy |
| `category_map` | one | local category → target category |
| `conflict_resolve` | one | settle contradictory annotations on one image |
| `manual_override` | one | name an id and include or exclude it, with a reason |

`filter` has five criteria:

- **`sample`** — deterministic splitting. `hash(seed + key_field value) % mod`,
  keeping the remainders in `keep_remainder`. Not random: the same spec always
  cuts the same films. `key_field` should be `subject_id` so a patient's images
  never split; images with no subject fall back to `image_id`, and the build
  says how many.
- **`predicate`** — an expression over the image (`file_name`, `width`,
  `date_captured`, `subject_id`, …) *and* its annotations as they stand in the
  current set (`labels`, `targets`, `annotators`, `n_annotations`), so
  `'Pneumonia' in labels` works and shifts as earlier steps map or resolve.
  `core/predicate.py` walks the Python AST against a node whitelist rather than
  evaluating it — a spec is data, never code.
- **`balance`** — cap every class at `max_per_class`. Multi-label makes "exactly
  N of each" unsatisfiable since one image fills two quotas, so quotas fill
  rarest class first, preferring images already chosen: rare classes take their
  share before common ones consume the shared images. Selection is
  `hash(seed + image_id)`, deterministic like `sample`.
- **`explicit_list`** — narrow to named files that must already be present;
  `on_missing` is `error` (default), `warn` or `ignore`.
- **`annotated`** — drop images with no annotation, the explicit way to satisfy
  the training-ready rule below.

`import_list` differs from `explicit_list` by being a leaf: it pulls the named
files in rather than filtering an existing set. Both accept
`file_names_ref: {sha256, source}` instead of an inline list; the body lives in
`manual_set_import_lists`, keyed by a hash of its content, so a spec referencing
ten thousand names stays ten lines long. `cxr lists add` registers one and the
session switches to a ref above `INLINE_LIST_MAX` (200). The hash is the same
anywhere, so a spec carried to another database works once its list is there.

```yaml
steps:
  - {id: aws, op: source, original_set: aws_images, annotation_batch: V2}
  - {id: tb,  op: source, original_set: TB-portal, annotation_batch: V1}
  - {id: pooled, op: union, inputs: [aws, tb]}
  - {id: deduped, op: dedup, input: pooled, source_priority: [TB-portal, aws_images]}
  - {id: train, op: filter, input: deduped, criterion: sample,
     key_field: subject_id, mod: 4, keep_remainder: [0, 1, 2],
     seed: pneumonia-2026-split}
  - {id: mapped, op: category_map, input: train, mapping: {
      aws_images@V2: {Pneumonia: pneumonia, Normal: normal},
      TB-portal@V1: {TB: tb, Normal: normal}}}
  - {id: resolved, op: conflict_resolve, input: mapped, rules: [
      {rule: annotator_precedence, annotator_precedence: [radiologist_senior]},
      {rule: highest_score}]}
final: resolved
```

Runnable examples live in `specs/`; `pneumonia_train.yaml` and
`pneumonia_val.yaml` are complementary splits of one seed. The session API
mirrors these ops plus `preview`, `checkpoint`, `rollback`, `undo`, `checkout`,
`compile`, `commit`. `undo` removes the step the head is on; `rollback` returns
to a checkpoint, restoring the step list and where the head stood. `preview()`
returns statistics — counts, the diff against the previous step, unmapped
categories, images lacking subject information, and a per-target distribution
(CLS POS / NEG / UNKNOWN in images, DET POS in boxes) — never a dump of rows.
`cxr show` reports the same distribution from SQL; the two implementations are
independent, so a test asserts they agree row for row.

## 4. Module responsibilities
| Module | Owns | Explicitly does not |
|---|---|---|
| `db/schema.sql` | Tables, composite FKs, the dangling-category trigger | Any business rule |
| `db/models.py` · `db/engine.py` | SQLAlchemy mapping, connection pool, applying the schema | Queries, business rules |
| `db/crud.py` | Read-side queries, plus deletion | Building a manual-set |
| `storage.py` | Object keys and access: images, and each version's spec.yaml | Knowing what a spec means |
| `core/schema.py` | Spec syntax: fields, types, step dependency graph | Anything needing the database |
| `core/types.py` | `CandidateSet`, `Catalog` (metadata cache) | Business rules |
| `core/ops.py` | Every rule: dedup, split, mapping, conflict resolution | DB writes, I/O, global state |
| `core/predicate.py` | Safe evaluation of `filter` expressions | `eval` |
| `core/engine.py` | Running steps in order; writing the version and its spec | Deciding *what* a step means |
| `core/export.py` | COCO / CSV / zip output | Re-deriving anything |
| `seed.py` · `scripts/tools/` | Mock data; one-off migration utilities | Anything the runtime imports |
| `session/builder.py` | Accumulating steps, checkpoint/rollback, compile | Persisting exploration |
| `session/analyzer.py` | Preview statistics | Mutating state |
| `cli/main.py` · `cli/repl.py` | Argument and command parsing, formatting | Business logic |

Every op has the signature `(catalog, inputs, step) -> StepResult`: no database
writes, no framework dependency, no global state.

## 5. Data flow
```
explore / notebook          steps accumulate in memory only
        │
        ├── preview()       statistics, never a dump of rows
        ├── checkpoint()    remembers a list length
        └── rollback()      truncates back to it
        │
     compile()              a clean spec; abandoned branches are already gone
        │
      commit()  ──────────► build()
                              ├─ execute_spec()  ops run in declared order
                              ├─ spec.yaml → manual-sets/{name}/annotations/
                              │              {version}/, beside the version
                              └─ manual_set_images / _cls_annotations /
                                 _det_annotations / _target_categories /
                                 _category_mappings
```

`CandidateSet` carries only id sets plus the accumulated category mapping, so
set operations stay cheap; metadata comes from `Catalog`, which loads each batch
once and answers from memory. A build is one pass of reads then pure Python,
with no cache layer. `CandidateSet` keeps one invariant — every annotation's
image is also in the set, restored by `prune()` after any removal — which is why
the composite foreign keys never reject a commit.

## 6. Design decisions
**The spec is a file, not a row.** It is a YAML document — read, edited, diffed
and kept in version control — so committing writes it to object storage at
`manual-sets/{name}/annotations/{version}/spec.yaml`, beside the version it
produced; the location is computed from (name, version), never stored. A copy of
the body in the database would be one more thing that can disagree with the
file. What stays is the one thing the file cannot vouch for itself: the sha256,
saying whether two versions came from the same recipe and catching a spec.yaml
edited or deleted after the fact (`cxr show` reports both).
`manual_set_build_specs` is gone, with the run and history tables before it.
The object is written before the transaction commits; the two are not one
transaction, so the order is the guarantee — a failed commit leaves an
unreferenced file, the reverse would leave an unreproducible version.

**Conflicts are grouped by image, not by (image, target_category).** Grouped the
latter way the conflict that matters most is invisible: when source A says
*normal* and B says *pneumonia*, the two annotations land in different target
groups, each of size one, and look consistent. Grouping by image and comparing
each source's label *set* surfaces those as `contradiction`. Resolution picks a
winning *source* per image — keeping the senior's *pneumonia* beside the junior's
*normal* would manufacture a self-contradictory row. Detection boxes are
excluded: disagreement there is geometric (IoU), not two exclusive answers.

**Dedup shows you the duplicates and lets you choose.** `dedup` explicitly
discards duplicate images *and* their annotations, so which copy survives is the
user's call: `duplicates` lists every group with each copy's id, source, subject
and labels, and `keep: [id, ...]` pins the winners. Unpinned groups fall back to
`prefer_annotated` — identical `blake3` is the same picture, so keeping the
unannotated copy discards labels for nothing — then to source priority. Moving an
annotation onto the survivor is not offered; a composite FK ties it to its own
image, and a bidirectional FK is not expressible: targets must be unique
(forbidding multi-label) and name one table, not "cls or det".

**A manual-set may not contain unannotated images.** An original-set image may be
"not yet labelled"; a manual-set is training-ready, so every image must carry an
annotation. A deferred constraint trigger enforces it (deferred because a build
writes images before annotations), `build()` checks first so the error names the
count and the fix, and `filter criterion=annotated` drops them explicitly so the
removal stays visible in the spec.

**Sourcing images brings their annotations.** `source --image` and `import_list`
pull every annotation on the images they bring in, disagreements included; those
are `conflict_resolve`'s job, not a silent choice at load time.

**Exploration is permissive, the compiled spec is strict.** `map_category` maps
one batch at a time, so it must *not* drop annotations not yet mapped, or the
first mapping deletes every other batch's labels. `compile()` then tightens the
last `category_map` to `require_total=True` and the last `conflict_resolve` to
`strict=True`. Rules that cannot decide fail and name the cases.

**Provenance is recomputed, not stored.** A version and its spec are the entire
record; how it was built is a pure function of (spec, data), so `cxr why` re-runs
the spec and watches the entity's membership change step by step. Two tables were
tried and removed for the same reason: per-entity rulings outnumbered the
datasets themselves and 86% restated the spec mechanically, while per-step
results duplicated it in two columns and carried statistics nothing read. Both
froze whatever `ops.py` concluded at build time, drifting from the rules as soon
as those changed. The cost is one build per `cxr why`, plus an intact spec.yaml.

**`manual_override` names an id; everything else is a rule.** `filter`, `dedup`
and the conflict rules state a criterion and apply it everywhere.
`manual_override` is the escape hatch for judgements that are not rules — a film
is unusable, a patient withdrew consent, one label is wrong — and naming ids in
the spec makes that reproducible and auditable instead of an untraceable
hand-edit, with the `reason` travelling into `cxr why`. Everything is addressed
by id, images included: file names are unique only within a batch, so a
`set/version/name` path must be parsed and resolved, and can resolve to the wrong
row. Including an image brings its annotations, mirroring `source --image`. None
of the four actions can silently do nothing — a no-op reporting success is worse
than a failure.

**Deletion is the one exception to immutability.** `cxr rm` exists because a
mistaken build otherwise strands a version number forever. It prints what will be
lost, refuses to run unattended without `--yes`, and tells you to save the spec
first. Source data is never touched: CASCADE removes the membership rows,
`ON DELETE RESTRICT` keeps the originals. The spec.yaml goes after the
transaction succeeds — an orphan object is litter, a missing spec is worse.

**Version conflicts are resolved optimistically.** The pre-flight check exists
only for a friendly message; the real guarantee is `UNIQUE (manual_set_id,
version)`. When two people commit the same version at once, one hits the
constraint and the error names who won — no locks, nothing blocks. That row also
records the builder: `created_by_name` / `created_by_email` are plain columns,
not a reference to `annotators`, which records who *labelled*.

**A build is one transaction, and failure leaves no version.** Any failing step
rolls the whole thing back — no half-built version, no record of the failure —
and `--dry-run` writes nothing at all, object storage included. The one thing
outside the transaction is the spec object, written first on purpose.

## 7. Guarantees, and where they are enforced
| Guarantee | Enforced by |
|---|---|
| An annotation's image is in the same version | Composite FKs in `schema.sql`, upheld by `prune()` |
| A category is never dangling | Deferred constraint trigger in `schema.sql` |
| Versions are immutable | `UNIQUE (manual_set_id, version)` + a pre-flight check in `build()` |
| A split never separates a patient | `hash(seed + subject_id) % mod` in `ops._filter_sample` |
| The same spec yields the same set | Seeded hashing, sorted iteration, priority-then-id tie-breaks |
| No annotation ships without a label name | `_assert_annotations_are_mapped()` before any write |
| Unresolvable conflicts stop the build | `strict=True` on the compiled `conflict_resolve` |
| POS + NEG + UNKNOWN equals the image count | One cls row per (image, category); `verify.sql` check 14 |
| A spec.yaml cannot be swapped unnoticed | `spec_sha256` on the version, re-checked on every read |

`scripts/verify.sql` re-checks these in SQL, bypassing the Python so a bug in
`ops.py` cannot make its own verification pass; `scripts/acceptance.sh` rebuilds
everything from scratch and runs 48 end-to-end checks.

## 8. Testing
176 tests against a real PostgreSQL instance, not SQLite: the deferred triggers,
composite foreign keys, `ARRAY` and `JSONB` are PostgreSQL-specific, and SQLite
would test constraints that do not exist.

| File | Covers |
|---|---|
| `test_spec.py` | Spec syntax; malformed specs must fail at parse time |
| `test_predicate.py` | Expression semantics; code injection must be refused |
| `test_ops.py` | The rules themselves — the behavioural contract |
| `test_engine.py` | Reproducibility, transactions, provenance, spec integrity |
| `test_session.py` | Checkpoint/rollback, compile, exploration leaves no trace |
| `test_repl.py` | Command parsing, and the explore → save → build round trip |
| `test_cli.py` | Every command actually executes and fails cleanly |
| `test_import_lists.py` | Long file-name lists: storage, refs, the round trip |

`test_cli.py` exists because `cxr show` was once guaranteed to crash on a
mistyped dict key while being documented and recommended: unit tests covered the
query, not the line printing it. The same gap recurred twice — Tab completion
tested by calling the completer while the key was never bound, and
`cxr spec > file.yaml` dropping text because Rich crops rather than wraps without
a tty — so both are now tested through the output path itself.

## 9. Known limitations
- Exploration state lives in the process; leaving `cxr explore` discards it.
  `save` writes the spec to a file (a temp path if unnamed), `load` resumes it.
- In `preview`, POS + NEG can exceed the image count before `conflict_resolve`
  (two sources calling one image positive and negative); `cxr show` cannot.
- Choosing which annotation survives a conflict is `resolve manual`, not
  `manual_override` — the two are easy to confuse.
- Export produces COCO and a CSV manifest; there is no YOLO writer.
- `scripts/tools/` holds the migration utilities — parquet import, blake3
  backfill, lineage building — needing pandas/pyarrow; the runtime does not.
- `cxr image` shows everything recorded about one image — annotations, lineage,
  duplicates, which datasets use it — but not the film. See `TODO.md`.
- A version spans two stores, and nothing enforces that spec.yaml still exists
  or matches: `spec_sha256` detects drift but cannot prevent it, and no
  `cxr fsck` sweeps every version.
- Largest tested scale is ~1,000 images; `Catalog` holds every batch it touches
  in memory. The tool runs on the server beside the database, so latency and
  memory have not been treated as constraints.
- No access control: anyone who can reach the database can build and read every
  dataset. Deliberate for an internal tool — the builder's name and email are
  attribution, not authentication.
