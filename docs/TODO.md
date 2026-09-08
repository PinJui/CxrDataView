# TODO

## Viewing images

The CLI can say that 394 films were selected and why, but never shows one. When
`duplicates` lists two byte-identical copies and asks which to keep, you see
file names and labels — not the picture.

The plan is `cxr view <manual-set>@<version>`, which builds a FiftyOne dataset
from the selection and launches its app, rather than writing a browser UI from
scratch. FiftyOne already does image grids, label overlays and side-by-side
comparison. The open question is how the pixels reach it: FiftyOne OSS wants
local file paths, so the selected images have to be pulled out of object storage
into a local cache first.

## Known issues

Found while walking through `cxr explore` as a user. All fixed; kept here as a
record of what the interface got wrong.

1. **Tab completion was broken for every real batch name.** (fixed)
   readline's default word delimiters include `@` and `-`, so `source aws@V<Tab>`
   matched nothing and `source TB-<Tab>` broke on the hyphen. Only completion
   from the very start of the name worked. Fixed by narrowing the delimiters to
   whitespace.

2. **`dedup` decided for you which copy survives.** (fixed)
   Running `dedup` is an explicit decision to discard duplicate images and the
   annotations on them, so the choice of which copy to keep belongs to the
   user. `duplicates` now lists each group with every copy's image id, source,
   subject and the annotations it carries; `dedup --keep <id>,<id>` pins the
   winners. Groups you do not pin fall back to `prefer_annotated` (identical
   `blake3` is the same picture, so keeping the unannotated copy would discard
   labels for nothing) and then to source priority. Re-attaching annotations to
   the surviving image row is not offered — the composite foreign keys require
   an annotation to travel with its own image.

8. **A manual-set could contain unannotated images.** (fixed)
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

3. **`manual_override` only half worked, and half of it was invisible.** (fixed)
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

4. **No way to settle a conflict by hand.** (fixed)
   The `manual` conflict rule existed in the spec and the ops, and the session
   exposed it, but the REPL never wired it up — and `conflicts` did not print
   annotation ids, so there was nothing to point at. Both fixed:
   `conflicts` shows ids, `resolve manual <image> <annotation_id> ["reason"]`
   applies them.

5. **`undo` deleted the wrong step, once branches existed.** (fixed)
   It removed the last entry in the step list, which was fine while the head
   was always the last step — but `checkout` broke that assumption, so standing
   on `source_1` and typing `undo` silently deleted `source_2` on another
   branch. `undo` now removes the step the head is on and moves the head to its
   input, refusing when another step depends on it. `checkpoint` records the
   head as well as the length, so `rollback` puts you back where you were
   standing rather than at the end of the list.

6. **No way to move between branches.** (fixed)
   Every `source` and `import` opens a branch and makes it current, but nothing
   could switch back, so a branch could only be extended while it happened to
   be the head. Added `checkout <step_id>`. `steps` now distinguishes two
   things that had been conflated: `← head` is where the next command lands,
   and `末端` marks a branch tip nothing has consumed yet — the tips are what
   `union` collects and what `commit` merges automatically.

7. **`source --image` left the images unlabelled.** (fixed)
   Sourcing an image batch pulled no annotations, so the obvious first command
   produced a dataset with no labels. It now pulls the annotations that exist
   on those images and reports which batches they came from; `--no-annotations`
   opts out. (Annotation-free manual-sets are legal and do commit — the schema
   allows an image to be "not yet labelled" — it was simply never what the user
   wanted from this command.)
