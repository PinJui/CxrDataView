# Design

The architecture of this system and how the code is divided. Sections 1–3 are
the design brief (what a spec means, why the layers exist); sections 4 onwards
record what was actually built and why it departs from the brief in places.

## 1. Principles
1. **The database stores results, never mutable business rules.** Dedup,
   splitting and conflict resolution are pure Python; changing a rule must not
   require a schema migration.
2. **Start simple, upgrade on evidence.** A spec is a linear list of steps in
   JSONB; content-addressed normalisation waits for a real pain point.
3. **Exploration and record are separate.** Users work against a mutable,
   reversible session; the spec is what the system compiles out of it, and that
   is what goes into git and the database.
4. **Provenance is precise to the step.** The same image may be dropped for
   different reasons in different rounds, so "why" is only answerable when
   bound to a specific step.
5. **Splits are deterministic and patient-level.** Seeded hashing keyed on
   `subject_id`, so one patient's films never straddle a train/val boundary.
6. **Gaps in external input are reported, never skipped.** An imported file
   list records every matched and missing name so it can be fixed in one pass.
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
                        (mapping) · db/crud.py (read-side queries)
```

Users only ever interact with Layer 3 or 4. Layer 2 is the compiled artifact
worth reviewing and committing to git; Layer 1 holds no business logic. The
schema alone answers "how is the result stored" but not: how does a user
*describe* a combination of batches without looking up ids; how is its
derivation recorded so it can be reproduced; and how does someone build it
incrementally, by trial and error, since nobody writes a correct spec first try.

## 3. Spec language
A spec is a list of steps with dependencies — mostly linear, occasionally
branching where two sources merge. Each step names itself; that name is both
its reference in `input` and its key in the provenance tables.

| op | input | purpose |
|---|---|---|
| `source` | none (leaf) | pull in a whole image or annotation batch, with the annotations on it |
| `import_list` | none (leaf) | pull in an external list of file names |
| `filter` | one | narrow the current result |
| `union` / `intersect` / `except` | many | combine candidate sets |
| `dedup` | one | drop duplicates by `blake3_hash`; prefers the annotated copy |
| `category_map` | one | local category → target category |
| `conflict_resolve` | one | settle contradictory annotations on one image |
| `manual_override` | one | name an id and include or exclude it, with a reason |

`filter` has three modes:

- **`sample`** — deterministic splitting. `hash(seed + key_field value) % mod`,
  keeping the remainders listed in `keep_remainder`. Not random: the same spec
  always cuts the same films. `key_field` should be `subject_id` so a patient's
  images never split; images with no subject fall back to `image_id` and the
  build reports how many were handled that way.
- **`predicate`** — an expression over the image (`file_name`, `width`,
  `date_captured`, `subject_id`, …) *and* over its annotations as they stand in
  the current set: `labels`, `targets`, `annotators`, `n_annotations`, so
  `'Pneumonia' in labels` works and shifts as earlier steps map or resolve.
- **`balance`** — cap every class at `max_per_class`. Multi-label makes "exactly
  N of each" unsatisfiable since one image fills two quotas, so quotas are
  filled rarest class first, preferring images already chosen: rare classes take
  their share before common ones consume the shared images. Selection is
  `hash(seed + image_id)`, deterministic like `sample`.
- **`explicit_list`** — narrow to named files that must already be present.
  `on_missing` is `error` (default), `warn` or `ignore`.

`import_list` differs from `explicit_list` by being a leaf: it pulls the named
files in rather than filtering an existing set. Both accept
`file_names_ref: {sha256, source}` instead of an inline list; the body lives in
`manual_set_import_lists`, keyed by a hash of its content, so a spec referencing
ten thousand names stays ten lines long. `cxr lists add` registers one; the
session switches to a ref above `INLINE_LIST_MAX` (200) names. The hash is
reproducible anywhere, so a spec carried to another database works once the list
is registered there.

```yaml
steps:
  - {id: aws, op: source, original_set: aws_images, annotation_batch: V2}
  - {id: tb,  op: source, original_set: TB-portal, annotation_batch: V1}
  - {id: pooled, op: union, inputs: [aws, tb]}
  - {id: deduped, op: dedup, input: pooled, source_priority: [TB-portal, aws_images]}
  - id: train
    op: filter
    input: deduped
    criterion: sample
    key_field: subject_id
    mod: 4
    keep_remainder: [0, 1, 2]
    seed: pneumonia-2026-split
  - id: mapped
    op: category_map
    input: train
    mapping:
      aws_images@V2: {Pneumonia: pneumonia, Normal: normal}
      TB-portal@V1:  {TB: tb, Normal: normal}
  - id: resolved
    op: conflict_resolve
    input: mapped
    rules:
      - {rule: annotator_precedence, annotator_precedence: [radiologist_senior, dr_lee]}
      - {rule: highest_score}
final: resolved
```

Runnable examples live in `specs/`; `pneumonia_train.yaml` and
`pneumonia_val.yaml` are complementary splits of one seed.

The session API mirrors these ops, plus `preview`, `checkpoint`, `rollback`,
`undo`, `checkout`, `compile` and `commit`. `undo` removes the step the head is
on; `rollback` returns to a named checkpoint, restoring both the step list and
where the head was standing. `preview()` returns a statistical
summary — counts, distributions, the diff against the previous step, unmapped
categories, how many images lacked subject information — never a dump of rows,
because exploration has to be fast.

## 4. Module responsibilities
| Module | Owns | Explicitly does not |
|---|---|---|
| `db/schema.sql` | Tables, composite FKs, the dangling-category trigger | Any business rule |
| `db/models.py` | SQLAlchemy mapping of that schema | Queries |
| `db/crud.py` | Read-side queries, plus deletion | Building a manual-set |
| `db/engine.py` | Connection pool, applying the schema | — |
| `core/schema.py` | Spec syntax: fields, types, step dependency graph | Anything needing the database |
| `core/types.py` | `CandidateSet`, `Catalog` (in-memory metadata cache) | Business rules |
| `core/ops.py` | Every rule: dedup, split, mapping, conflict resolution | DB writes, I/O, global state |
| `core/predicate.py` | Safe evaluation of `filter` expressions | `eval` |
| `core/engine.py` | Running steps in order; writing results and provenance | Deciding *what* a step means |
| `core/export.py` | COCO / CSV / zip output | Re-deriving anything |
| `session/builder.py` | Accumulating steps, checkpoint/rollback, compile | Persisting exploration |
| `session/analyzer.py` | Preview statistics | Mutating state |
| `cli/main.py` | Argument parsing, formatting | Business logic |
| `cli/repl.py` | Translating typed commands into session calls | Business logic |
| `seed.py` | Mock data generation | Production concerns |
| `scripts/tools/` | One-off data migration: parquet import, blake3 backfill, lineage | Anything the runtime depends on |

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
                              ├─ execute_spec()   ops run in declared order
                              ├─ manual_set_build_specs   (the spec — the whole
                              │                            record of how)
                              └─ manual_set_images / _cls_annotations /
                                 _det_annotations / _target_categories /
                                 _category_mappings
```

`CandidateSet` carries only id sets plus the accumulated category mapping, so
set operations stay cheap; metadata comes from `Catalog`, which loads each batch
once and answers from memory. A build is "one pass of reads, then pure Python",
with no cache layer — every build re-runs the whole spec. `CandidateSet` keeps
one invariant: every annotation's image is also in the set, restored by
`prune()` after any removal, which is why the composite foreign keys never
reject a commit.

## 6. Design decisions
**Provenance keys on `build_spec_id`, not a run id.** Exploration never touches
the database, a spec is written only at commit, and maps 1:1 to a version
(`UNIQUE`). Re-running a spec means verifying reproducibility (`--dry-run`) or
producing a new version, so a run table would hold one row per spec. Dropped,
along with the separate exploration-history table.

**Conflicts are grouped by image, not by (image, target_category).** Grouped the
latter way the conflict that matters most is invisible: when source A says
*normal* and B says *pneumonia*, the two annotations land in different target
groups, each of size one, and look consistent. Grouping by image and comparing
each source's label *set* surfaces those as `contradiction`. Resolution picks a
winning *source* per image — keeping the senior's *pneumonia* beside the
junior's *normal* would manufacture a self-contradictory row.

**Detection annotations are excluded from conflict resolution.** Disagreement
between boxes is geometric (IoU), not two exclusive answers to one question.

**Dedup shows you the duplicates and lets you choose.** `dedup` is an explicit
decision to discard duplicate images and their annotations, so which copy
survives is the user's call: `duplicates` lists every group with each copy's id,
source, subject and labels, and `keep: [id, ...]` pins the winners. Unpinned
groups fall back to `prefer_annotated` — identical `blake3` is the same picture,
so keeping the unannotated copy discards labels for nothing — then to source
priority. Moving an annotation onto the surviving row is not offered; a
composite foreign key ties it to its own image. A bidirectional foreign key is
not expressible anyway: FK targets must be unique (forbidding multi-label) and
reference one table, not "cls or det".

**A manual-set may not contain unannotated images.** An original-set image may
be "not yet labelled"; a manual-set is training-ready, so every image must carry
an annotation. A deferred constraint trigger enforces this — deferred because a
build writes images before annotations — and `build()` checks first so the error
reports the count and the fix. `filter criterion=annotated` drops them
explicitly, keeping the removal visible in the spec. This reverses what the
original schema said on `manual_set_images`, whose composite foreign keys only
constrained the opposite direction.

**Sourcing images brings their annotations.** `source ... --image` and
`import_list` pull every annotation on the images they bring in, including
batches that disagree — `conflict_resolve` settles that, not a silent choice at
load time.

**Exploration is permissive, the compiled spec is strict.** `map_category` maps
one batch at a time, so it must *not* drop annotations not yet mapped —
otherwise the first mapping deletes every other batch's labels. `compile()` then
tightens the last `category_map` to `require_total=True` and the last
`conflict_resolve` to `strict=True`. Rules that cannot decide never fall back
silently: they fail and name the cases.

**Provenance is recomputed, not stored.** A version and the spec that produced
it are the entire record; how it was built is a pure function of (spec, data),
both still in the database, so `cxr why` re-runs the spec and watches the
entity's membership change step by step. Two tables were tried here and removed
for the same reason: per-entity rulings outnumbered the datasets themselves and
86% restated the spec mechanically, while per-step results duplicated the spec
in two columns, made the step count redundant with
`jsonb_array_length(spec -> 'steps')`, and carried statistics nothing read. Both
also froze whatever `ops.py` concluded at build time, drifting from the rules as
soon as those changed. Recomputing costs one build per `cxr why`.

**`manual_override` names an id; everything else is a rule.** `filter`, `dedup`
and the conflict rules describe a criterion and apply it everywhere.
`manual_override` is the escape hatch for judgements that are not rules — a film
is unusable, a patient withdrew consent, one label is wrong — and naming ids in
the spec makes that reproducible and auditable instead of a hand-edit nobody can
trace, with the `reason` travelling into `cxr why`. Everything is addressed by
id, images included: file names are unique only within a batch, so a
`set/version/name` path must be parsed and resolved, and every such step can
point at the wrong row. Including an image brings its annotations, mirroring
`source --image`; naming an annotation loads its batch on demand. None of the
four actions can silently do nothing — a no-op reporting success is worse than
a failure.

**`filter` expressions are parsed, not evaluated.** `core/predicate.py` walks
the Python AST against a node whitelist — a spec is data, never code.

**Deletion is the one exception to immutability.** `cxr rm` exists because a
mistaken build otherwise strands a version number forever. It prints what will
be lost, refuses to run unattended without `--yes`, and tells you to save the
spec first — that file is the only thing that can reproduce the dataset. Source
data is never touched: CASCADE removes the membership rows and the spec, while
`ON DELETE RESTRICT` on the source tables keeps the originals safe.

**Version conflicts are resolved optimistically.** The pre-flight check exists
only for a friendly message; the real guarantee is `UNIQUE (manual_set_id,
version)`. When two people commit the same version at once, one hits the
constraint and the error names who won. No locks, so nothing blocks.

**Every version records who built it.** `created_by_name` / `created_by_email`
are plain columns, not a reference to `annotators` — that table records who
*labelled*. This is attribution, not authentication.

**A build is one transaction, and failure leaves nothing.** Any failing step
rolls the whole thing back — no half-built version, and no record of the
failure either. `--dry-run` likewise writes nothing at all. What went wrong is
the error message's job, not the database's.

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

`scripts/verify.sql` re-checks these in SQL, bypassing the Python so a bug in
`ops.py` cannot make its own verification pass.

## 8. Testing
165 tests, against a real PostgreSQL instance rather than SQLite: the deferred
constraint triggers, composite foreign keys, `ARRAY` and `JSONB` are all
PostgreSQL-specific, and SQLite would test constraints that do not exist.

| File | Covers |
|---|---|
| `test_spec.py` | Spec syntax; malformed specs must fail at parse time |
| `test_predicate.py` | Expression semantics; code injection must be refused |
| `test_ops.py` | The rules themselves — the behavioural contract |
| `test_engine.py` | Reproducibility, transactional integrity, provenance |
| `test_session.py` | Checkpoint/rollback, compile, exploration leaves no trace |
| `test_repl.py` | Command parsing, and the explore → save → build round trip |
| `test_cli.py` | Every command actually executes and fails cleanly |
| `test_import_lists.py` | Long file-name lists: storage, refs, the round trip |

`test_cli.py` exists because `cxr show` was once guaranteed to crash on a
mistyped dict key and survived a long time — documented and recommended, but
never run. Unit tests covered the query, not the line printing it.

## 9. Known limitations
- Exploration state lives in the process. Leaving `cxr explore` discards it;
  `save` to a spec file and `load` it back to continue later.
- Choosing which annotation survives a conflict is `resolve manual`, not
  `manual_override`; the two are easy to confuse.
- Export produces COCO and a CSV manifest; there is no YOLO writer.
- `scripts/tools/` holds the migration utilities: parquet import (original-set
  and manual-set), blake3 backfill, and lineage building. They need
  pandas/pyarrow, which the runtime does not.
- `cxr image` shows everything recorded about one image — annotations, lineage,
  duplicates, which datasets use it — but not the film itself. See `TODO.md`.
- Largest tested scale is ~1,000 images. `Catalog` holds every batch it touches
  in memory. The tool is expected to run on the server beside the database, so
  latency and memory have not been treated as constraints.
- There is no access control: anyone who can reach the database can build and
  read every dataset. This is deliberate for an internal tool — the builder's
  name and email are recorded for attribution, not authentication.
