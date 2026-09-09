"""每一個 CLI 指令都要被真的執行過一次。

存在的理由：`cxr show` 曾經因為讀錯一個 dict 鍵而必定 crash，卻活了很久——
因為它被寫進 README、被推薦給使用者，就是沒有任何測試或腳本真的跑過它。
單元測試測得到 crud/ops 的邏輯，測不到「輸出那一行字取錯鍵」。

所以這裡不細看內容對不對（那是別的測試的事），只確保：
每個指令都跑得起來、回傳碼正確、而且沒有吐出 traceback。
"""

import json
import uuid

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from cxr_dataset_manager.cli.main import app
from cxr_dataset_manager.db import crud

runner = CliRunner()


def run(*args: str):
    result = runner.invoke(app, list(args))
    if result.exception and not isinstance(result.exception, SystemExit):
        raise AssertionError(
            f"`cxr {' '.join(args)}` 拋出 {type(result.exception).__name__}: "
            f"{result.exception}\n{result.output}"
        )
    assert "Traceback" not in result.output, f"`cxr {' '.join(args)}` 吐了 traceback\n{result.output}"
    return result


def ok(*args: str) -> str:
    result = run(*args)
    assert result.exit_code == 0, (
        f"`cxr {' '.join(args)}` 應該成功，卻回 {result.exit_code}\n{result.output}"
    )
    return result.output


def fails(*args: str) -> str:
    result = run(*args)
    assert result.exit_code != 0, (
        f"`cxr {' '.join(args)}` 應該失敗，卻成功了\n{result.output}"
    )
    return result.output


# ---------------------------------------------------------------------------
# 一次性建好一個版本，讓下面所有查詢類指令都有東西可查
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built(request):
    from cxr_dataset_manager.db.engine import new_session

    name = f"clitest_{uuid.uuid4().hex[:8]}"
    spec = f"/tmp/{name}.yaml"
    with open(spec, "w") as fh:
        fh.write(
            """
steps:
  - {id: aws, op: source, original_set: aws_images, annotation_batch: V1}
  - {id: tb, op: source, original_set: TB-portal, annotation_batch: V1}
  - {id: pooled, op: union, inputs: [aws, tb]}
  - {id: deduped, op: dedup, input: pooled, source_priority: [TB-portal, aws_images]}
  - {id: half, op: filter, input: deduped, criterion: sample,
     key_field: subject_id, mod: 2, keep_remainder: [0], seed: cli-test}
  - {id: mapped, op: category_map, input: half, merge_identical: true}
final: mapped
"""
        )
    ok("build", spec, "-m", name, "-v", "V1", "--author-name", "pytest", "--author-email", "pytest@example.com")
    ok("build", spec, "-m", name, "-v", "V2", "--author-name", "pytest", "--author-email", "pytest@example.com")

    def cleanup():
        db = new_session()
        db.execute(text("DELETE FROM manual_sets WHERE name = :n"), {"n": name})
        db.commit()
        db.close()

    request.addfinalizer(cleanup)
    return {"name": name, "spec": spec}


# ---------------------------------------------------------------------------
# 每一個指令
# ---------------------------------------------------------------------------


def test_help_lists_every_command():
    output = ok("--help")
    for command in ("validate", "build", "show", "spec", "why", "diff",
                    "check-leakage", "export", "explore", "db", "ls"):
        assert command in output


@pytest.mark.parametrize("group,command", [
    ("ls", "sets"), ("ls", "batches"), ("ls", "categories"),
    ("ls", "annotators"), ("ls", "manual-sets"), ("ls", "history"),
    ("db", "status"),
])
def test_readonly_subcommands_run(group, command, db):
    ok(group, command)


def test_ls_batches_with_a_filter(db):
    output = ok("ls", "batches", "aws_images")
    assert "aws_images" in output and "DrLee" not in output


def test_validate(built):
    output = ok("validate", built["spec"])
    assert "合法" in output


def test_show(built):
    """回歸測試：這支指令曾經 100% crash 在 subjects['distinct']。"""
    output = ok("show", f"{built['name']}@V1")
    assert "影像" in output and "病患數" in output
    assert "來源組成" in output and "Target category" in output
    # 每個 target category 的正／負／未知分布
    assert "Category distribution" in output
    for column in ("CLS POS", "CLS NEG", "CLS UNKNOWN", "DET POS"):
        assert column in output, column


def test_spec_view_shows_every_line(built):
    """view 是顯示，不是資料通道——但畫面上也不該少字。

    Syntax(word_wrap=True) 是把太長的行折行；預設的 word_wrap=False 是裁掉，
    那才是原本讓 spec 憑空少一句話的原因。
    """
    from cxr_dataset_manager.storage import get_store

    output = ok("spec", f"{built['name']}@V1")
    stored = get_store().get_spec(built["name"], "V1")
    # 折行會插入換行、上色會加控制碼，所以比對的是「每個字都還在」
    flat_out = "".join(output.split())
    for line in stored.splitlines():
        assert "".join(line.split()) in flat_out, line


def test_spec_saves_a_byte_identical_file(built, tmp_path):
    """save 這條路徑直接寫檔，一個位元組都不能變——它才是保存 spec 的方式。"""
    from cxr_dataset_manager.core.schema import BuildSpec
    from cxr_dataset_manager.storage import get_store

    path = tmp_path / "saved.yaml"
    ok("spec", f"{built['name']}@V1", "-o", str(path))
    assert path.read_text() == get_store().get_spec(built["name"], "V1")
    assert BuildSpec.from_yaml(path.read_text()).final


def test_why(built, db):
    version_id = crud.resolve_version(db, built["name"], "V1")
    image_id = db.execute(
        text("SELECT image_id FROM manual_set_images WHERE manual_set_version_id = :v LIMIT 1"),
        {"v": version_id},
    ).scalar_one()
    ref = db.execute(
        text(
            """
            SELECT os.name || '/' || ib.version || '/' || i.file_name
            FROM images i
            JOIN image_batches ib ON ib.id = i.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            WHERE i.id = :i
            """
        ),
        {"i": image_id},
    ).scalar_one()

    output = ok("why", f"{built['name']}@V1", "--image", ref)
    assert "在最終集合裡" in output


def test_why_on_an_image_that_was_never_involved(built):
    output = ok("why", f"{built['name']}@V1", "--image", "aws_images/V1/AWS_00000.png")
    assert "集合裡" in output or "從未進入" in output


def test_diff(built):
    output = ok("diff", f"{built['name']}@V1", f"{built['name']}@V2")
    assert "影像" in output and "cls 標註" in output


def test_check_leakage(built):
    output = ok("check-leakage", f"{built['name']}@V1", f"{built['name']}@V2")
    assert "Leakage" in output


@pytest.mark.parametrize("fmt", ["zip", "coco", "csv"])
def test_export(built, tmp_path, fmt):
    ok("export", f"{built['name']}@V1", "-o", str(tmp_path), "-f", fmt)
    files = list(tmp_path.iterdir())
    assert files and files[0].stat().st_size > 0


def test_export_coco_is_valid_json(built, tmp_path):
    ok("export", f"{built['name']}@V1", "-o", str(tmp_path), "-f", "coco")
    data = json.loads(next(tmp_path.glob("*_coco.json")).read_text())
    assert data["images"] and data["categories"]


def test_build_dry_run(built):
    output = ok("build", built["spec"], "-m", built["name"], "-v", "V-dry", "--dry-run")
    assert "試跑" in output


# ---------------------------------------------------------------------------
# 錯誤路徑：訊息要看得懂，而且不能吐 traceback
# ---------------------------------------------------------------------------


def test_unknown_manual_set_fails_cleanly(built):
    for args in (
        ("show", "no_such_set@V1"),
        ("spec", "no_such_set@V1"),
        ("why", "no_such_set@V1", "--image", "x.png"),
        ("export", "no_such_set@V1"),
        ("diff", "no_such_set@V1", f"{built['name']}@V1"),
        ("check-leakage", "no_such_set@V1", f"{built['name']}@V1"),
    ):
        output = fails(*args)
        assert "找不到" in output, f"`cxr {' '.join(args)}` 的錯誤訊息不夠清楚：{output}"


def test_bad_ref_format_fails_cleanly(built):
    output = fails("show", "missing_the_at_sign")
    assert "name@version" in output or "找不到" in output


def test_missing_spec_file_fails_cleanly():
    assert "找不到" in fails("validate", "/tmp/definitely_not_here.yaml")


def test_check_leakage_needs_two_versions(built):
    assert "至少" in fails("check-leakage", f"{built['name']}@V1")


def test_duplicate_version_is_refused(built):
    assert "已經有版本" in fails(
        "build", built["spec"], "-m", built["name"], "-v", "V1", "--author-name", "pytest", "--author-email", "pytest@example.com"
    )


def test_rm_refuses_without_confirmation_when_not_interactive(built):
    """腳本裡誤打一行不該就把資料集刪掉。"""
    output = fails("rm", f"{built['name']}@V2")
    assert "--yes" in output
    assert crud.resolve_version.__name__  # sanity


def test_rm_deletes_and_warns_about_the_spec(built, db):
    output = ok("rm", f"{built['name']}@V2", "--yes")
    assert "spec" in output and "已刪除" in output
    assert crud.resolve_version(db, built["name"], "V2") is None
    assert crud.resolve_version(db, built["name"], "V1") is not None


def test_rm_on_a_missing_target_fails_cleanly(built):
    assert "找不到" in fails("rm", "no_such_set@V1", "--yes")


def test_image_shows_annotations_lineage_and_duplicates(db):
    """`cxr image` 是唯一能查影像血緣的入口——build_lineage.py 建的邊，
    在這支指令出現之前沒有任何介面讀得到。"""
    from sqlalchemy import text

    edge = db.execute(
        text("SELECT parent_image_id, child_image_id FROM image_lineage LIMIT 1")
    ).one_or_none()
    if edge is None:
        pytest.skip("這份資料沒有血緣邊，先跑 scripts/tools/build_lineage.py")
    parent, child = edge

    out = ok("image", str(parent))
    assert "血緣" in out and "衍生出" in out

    # 反向也查得到
    assert "來自" in ok("image", str(child))


def test_image_accepts_an_id_or_a_path(db):
    from sqlalchemy import text

    image_id, ref = db.execute(
        text(
            """
            SELECT i.id, os.name || '/' || ib.version || '/' || i.file_name
            FROM images i
            JOIN image_batches ib ON ib.id = i.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            LIMIT 1
            """
        )
    ).one()
    assert f"image #{image_id}" in ok("image", str(image_id))
    assert f"image #{image_id}" in ok("image", ref)


def test_image_reports_which_datasets_use_it(built, db):
    from sqlalchemy import text

    image_id = db.execute(
        text(
            """
            SELECT msi.image_id FROM manual_set_images msi
            JOIN manual_set_versions mv ON mv.id = msi.manual_set_version_id
            JOIN manual_sets ms ON ms.id = mv.manual_set_id
            WHERE ms.name = :n LIMIT 1
            """
        ),
        {"n": built["name"]},
    ).scalar_one()
    out = ok("image", str(image_id))
    assert "被這些資料集用了" in out and built["name"] in out


def test_image_on_a_missing_or_ambiguous_target_fails_cleanly():
    assert "找不到影像" in fails("image", "99999999")
    assert "找不到影像" in fails("image", "no_such_file.png")


def test_a_warning_never_ends_up_inside_the_saved_file(built, tmp_path):
    """spec 被動過手腳時要警告使用者，但警告是講給人聽的，不能寫進檔案。

    存檔走的是 write_text，跟顯示完全分開，所以檔案裡只會有 spec 本身。
    """
    from cxr_dataset_manager.core.schema import BuildSpec
    from cxr_dataset_manager.storage import get_store

    original = get_store().get_spec(built["name"], "V1")
    tampered = BuildSpec.from_yaml(original)
    tampered.description = "被動過手腳"
    get_store().put_spec(built["name"], "V1", tampered.to_yaml())
    try:
        path = tmp_path / "warned.yaml"
        output = ok("spec", f"{built['name']}@V1", "-o", str(path))
        assert "有人改過這份" in "".join(output.split()), output

        saved = path.read_text()
        assert "⚠" not in saved and "有人改過" not in saved
        assert saved == get_store().get_spec(built["name"], "V1")
        assert BuildSpec.from_yaml(saved).description == "被動過手腳"
    finally:
        get_store().put_spec(built["name"], "V1", original)
