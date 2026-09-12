# Fixed issues

Interface mistakes found by using the tool, each with what was wrong and what
replaced it. Kept because several of them were the *second* instance of the same
mistake, and because a few changed the design rather than just the code.

Open work lives in `TODO.md`; the architecture is in `design_doc.md`.

1. **Tab completion did nothing at all.**
   Two separate faults. readline's default word delimiters include `@` and `-`,
   so `source aws@V<Tab>` matched nothing and `source TB-<Tab>` broke on the
   hyphen; the delimiters are now narrowed to whitespace. Underneath that, the
   Tab key was never bound: `cmd.Cmd` hardcodes `parse_and_bind("tab: complete")`,
   GNU readline syntax that macOS's libedit build silently ignores. `preloop()`
   now issues the right string for the backend in use, so completion works both
   on the deployment target (Ubuntu, GNU) and on macOS. The first fix was
   verified by calling the completer directly, which is exactly why the missing
   binding went unnoticed — see `design_doc.md` §8.

2. **`dedup` decided for you which copy survives.**
   Running `dedup` is an explicit decision to discard duplicate images and the
   annotations on them, so the choice of which copy to keep belongs to the
   user. `duplicates` now lists each group with every copy's image id, source,
   subject and the annotations it carries; `dedup --keep <id>,<id>` pins the
   winners. Groups you do not pin fall back to `prefer_annotated` (identical
   `blake3` is the same picture, so keeping the unannotated copy would discard
   labels for nothing) and then to source priority. Re-attaching annotations to
   the surviving image row is not offered — the composite foreign keys require
   an annotation to travel with its own image.

3. **A manual-set could contain unannotated images.**
   The original schema said on `manual_set_images` that "an image with no
   annotation may still be selected", and its composite foreign keys only
   constrain the other direction (selecting an annotation requires its image).
   But a manual-set is a training-ready dataset, so the rule is now the
   reverse: every image in it must carry at least one annotation, enforced by a
   deferred constraint trigger, with an application-level check first so the
   error can name the count and the fix. `filter --annotated` drops unlabelled
   images explicitly, and `source`/`import_list` now pull the annotations that
   exist on the images they bring in. **This is a deliberate departure from the
   original SQL**; the file it came from is kept in `legacy/db-before-merge/`.

4. **`manual_override` only half worked, and half of it was invisible.**
   The concept is simple — name an id, include or exclude it — but three things
   got in the way. Naming an annotation from a batch the spec had not sourced
   failed, because `Catalog` had no way to load one by id (`ensure_images` had
   existed all along; the annotation equivalent was simply missing). Including
   an image did not bring its annotations, unlike `source --image`, so the
   result violated the "every image is annotated" rule. And excluding something
   that was not in the set silently reported success. All three fixed, and
   `cxr explore` now exposes the whole thing as
   `include` / `exclude <image|cls|det> <id> ["reason"]`. Images are addressed
   by id like everything else — file names are unique only within a batch, so
   the old `set/version/name` path form had to be parsed and resolved, which is
   a chance to point at the wrong row for no benefit. The redundant image field
   on the manual conflict rule went too: an annotation id already determines
   its image, and a redundant field is one that eventually disagrees.

5. **No way to settle a conflict by hand.**
   The `manual` conflict rule existed in the spec and the ops, and the session
   exposed it, but the REPL never wired it up — and `conflicts` did not print
   annotation ids, so there was nothing to point at. Both fixed:
   `conflicts` shows ids, `resolve manual <image> <annotation_id> ["reason"]`
   applies them.

6. **`undo` deleted the wrong step, once branches existed.**
   It removed the last entry in the step list, which was fine while the head
   was always the last step — but `checkout` broke that assumption, so standing
   on `source_1` and typing `undo` silently deleted `source_2` on another
   branch. `undo` now removes the step the head is on and moves the head to its
   input, refusing when another step depends on it. `checkpoint` records the
   head as well as the length, so `rollback` puts you back where you were
   standing rather than at the end of the list.

7. **No way to move between branches.**
   Every `source` and `import` opens a branch and makes it current, but nothing
   could switch back, so a branch could only be extended while it happened to
   be the head. Added `checkout <step_id>`. `steps` now distinguishes two
   things that had been conflated: `← head` is where the next command lands,
   and `末端` marks a branch tip nothing has consumed yet — the tips are what
   `union` collects and what `commit` merges automatically.

8. **`source --image` left the images unlabelled.**
   Sourcing an image batch pulled no annotations, so the obvious first command
   produced a dataset with no labels. It now pulls the annotations that exist
   on those images and reports which batches they came from; `--no-annotations`
   opts out. (Written before issue 3 made annotations mandatory; back then an
   unlabelled manual-set still committed, it just was not what anyone wanted
   from this command. Now it would be refused outright.)

9. **Saving a spec silently dropped text.**
   `cxr spec x@V1 > x.yaml` was the documented way to keep a spec, but the
   command only *printed* — the shell's redirect made the file, so the data went
   out through Rich, whose `Syntax` crops at the console width instead of
   wrapping. Without a tty the width is 80, CJK takes two cells each, and the
   saved file lost a whole clause without any error. Fixed twice over: a spec
   now has exactly three operations — save (`-o`, or `save` in the REPL) writes
   the file directly, view renders it wrapped so nothing vanishes on screen
   either, and load reads one back. Redirecting a command's output into a file
   is no longer a supported path; stdout is a display channel.

10. **Importing images could overwrite an object nobody had registered.**
    `import_image_batch.py` checked for conflicts in the database only, so an
    object already in the bucket under the same key — left by an earlier run
    that failed after uploading, or put there by hand — was replaced without a
    word. `--on-image-exists` now decides, the way `--on-conflict` does for
    rows: `error` (the default) stops and names them; `skip` leaves the object
    and does not register the file either, since a row would describe a file
    it never read; `overwrite` backs the object up first and restores it if
    the import fails. One listing of the batch's prefix finds them all.

11. **The standard manual-set parquet needed a separate script.**
    `scripts/tools/export_manual_set_version.py` re-queried what `cxr export`
    already reads, and is gone: `cxr export name@V1 -f parquet` writes the five
    parquet files and the version's `__meta__.md` into
    `<out>/manual-sets/<name>/annotations/<version>/`. Its output matches the
    script's value for value on the same version; the columns now carry their
    types even when a table is empty, where pandas wrote untyped `object`
    columns. Categories are numbered by name through one function that COCO
    and the `__meta__.md` share. An unknown `-f` used to fall through to zip
    silently; it is now an error.

12. **The CLI spoke only Chinese.**
    Every prompt, message, table header and help text of `cxr` and
    `cxr explore` is English now, including the errors raised further down —
    the `SpecError`s of the spec, ops, engine and session — that reach the
    terminal. Comments and docstrings no user sees were left as they were.

13. **`__meta__.md` was written by local scripts and never reached MinIO.**
    `generate_images_meta.py` and `generate_annotations_meta.py` read a local
    folder and wrote a file beside it. The templates now live in
    `core/meta.py` and fill themselves from the database; `cxr meta images`
    and `cxr meta annotations <set>@<version>` ask only what a person knows
    and store the file beside the batch in the original-sets bucket. The image
    data type, which the database does not hold, comes from each PNG's
    25-byte header instead of downloading the film. Committing a manual-set
    version — `commit` in `cxr explore`, or `cxr build` — asks the same way
    after the selection is written and before the transaction commits, so the
    statistics are final and a failed build asks nothing; the file lands
    beside `spec.yaml`, and `cxr rm` removes it with the spec. With no terminal
    the free-text answers are N/A and the statistics are still written;
    `cxr meta manual-set name@V1 --yes` fills them in later, `--view` shows it.
    The class distribution uses `cxr show`'s definitions (POS is `score > 0`),
    where the two scripts had disagreed with each other.
