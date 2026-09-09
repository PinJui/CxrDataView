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

Fixed interface issues are recorded in `fixed_issues.md`.
