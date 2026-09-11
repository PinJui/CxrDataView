"""scripts/tools/import_image_batch.py against the real bucket and database.

What is under test is `--on-image-exists`: an object already in the bucket
that the database does not know about — left by an earlier run that failed
after uploading, or put there by hand — used to be overwritten silently.
"""

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from cxr_dataset_manager.storage import get_store, object_key_for_image

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tools" / "import_image_batch.py"
OLDER = b"an older object that must survive"


def _script():
    spec = importlib.util.spec_from_file_location("import_image_batch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def batch(tmp_path, db):
    name = f"pytest_import_{uuid.uuid4().hex[:8]}"
    folder = tmp_path / "images"
    folder.mkdir()
    for i, value in enumerate((1000, 2000)):
        cv2.imwrite(str(folder / f"img_{i}.png"), np.full((8, 8), value, np.uint16))
    orphan = object_key_for_image(name, "V1", "img_0.png")
    get_store().put(orphan, OLDER)
    yield {"name": name, "dir": folder, "orphan": orphan}

    store = get_store()
    for prefix in (f"{name}/", f"_rollback_backups/{name}/"):
        for key in store.list_keys(prefix):
            store.delete(key)
    db.execute(text("DELETE FROM original_sets WHERE name = :n"), {"n": name})
    db.commit()


def _import(monkeypatch, batch, *flags):
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_image_batch.py", "-d", str(batch["dir"]), "-s", batch["name"],
         "-v", "V1", "--workers", "2", *flags],
    )
    _script().main()


def _registered(db, name) -> set[str]:
    return set(
        db.execute(
            text(
                """
                SELECT i.file_name FROM images i
                JOIN image_batches ib ON ib.id = i.image_batch_id
                JOIN original_sets os ON os.id = ib.original_set_id
                WHERE os.name = :n
                """
            ),
            {"n": name},
        ).scalars()
    )


def test_by_default_an_object_in_the_bucket_stops_the_import(monkeypatch, batch, db, capsys):
    _import(monkeypatch, batch)
    assert "--on-image-exists" in capsys.readouterr().out
    assert get_store().get(batch["orphan"]) == OLDER
    assert _registered(db, batch["name"]) == set()


def test_skip_leaves_the_object_and_the_database_alone(monkeypatch, batch, db):
    _import(monkeypatch, batch, "--on-image-exists", "skip")
    assert get_store().get(batch["orphan"]) == OLDER
    # skipped on both sides: a row for img_0 would describe a file it never read
    assert _registered(db, batch["name"]) == {"img_1.png"}


def test_overwrite_replaces_the_object_and_registers_it(monkeypatch, batch, db):
    _import(monkeypatch, batch, "--on-image-exists", "overwrite")
    assert get_store().get(batch["orphan"]) == (batch["dir"] / "img_0.png").read_bytes()
    assert _registered(db, batch["name"]) == {"img_0.png", "img_1.png"}
    # the backup taken before overwriting is gone once the import succeeded
    assert not get_store().list_keys(f"_rollback_backups/{batch['name']}/")
