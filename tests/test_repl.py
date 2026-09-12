"""`cxr explore` 的指令解析層。

REPL 只是皮：這裡測的是「指令列有沒有正確翻譯成 session 呼叫」，
以及探索完存出來的 spec 能不能原封不動被 cxr build 重跑。
業務邏輯本身的測試在 test_ops.py / test_engine.py。
"""

import uuid

import pytest
from sqlalchemy import text

from cxr_dataset_manager.cli.repl import ExploreShell
from cxr_dataset_manager.core.engine import execute_spec
from cxr_dataset_manager.core.schema import BuildSpec
from cxr_dataset_manager.core.types import Catalog, SpecError
from cxr_dataset_manager.db import crud


@pytest.fixture
def shell(db):
    sh = ExploreShell(name=f"pytest_{uuid.uuid4().hex[:8]}")
    yield sh
    sh.db.execute(
        text("DELETE FROM manual_sets WHERE name = :n"), {"n": sh.session.name}
    )
    sh.db.commit()
    sh.db.close()


def run(shell, *lines):
    for line in lines:
        shell.onecmd(line)
    return shell.session


def test_source_translates_to_the_right_batch_kind(shell):
    run(shell, "source aws_images@V1 --annotation")
    step = shell.session.steps[-1]
    assert step.annotation_batch == "V1" and step.image_batch is None

    run(shell, "source aws_images@V1 --image")
    step = shell.session.steps[-1]
    assert step.image_batch == "V1" and step.annotation_batch is None


def test_ambiguous_source_refuses_to_guess(shell):
    """aws_images@V1 同時有 image 與 annotation batch —— 猜錯的代價太大。"""
    run(shell, "source aws_images@V1")
    assert not shell.session.steps, "沒指定種類時不該擅自挑一個"


def test_unknown_source_is_reported_not_crashed(shell):
    run(shell, "source no_such_set@V9 --image")
    assert not shell.session.steps


def test_split_parses_its_options(shell):
    run(
        shell,
        "source aws_images@V1 --image",
        "split --mod 4 --keep 0,1,2 --seed my-seed --key subject_id",
    )
    step = shell.session.steps[-1]
    assert (step.mod, step.keep_remainder, step.seed) == (4, [0, 1, 2], "my-seed")
    assert step.key_field == "subject_id"


def test_map_parses_key_value_pairs(shell):
    run(
        shell,
        "source aws_images@V1 --annotation",
        "map aws_images@V1 Pneumonia=pneumonia Normal=normal Effusion=effusion",
    )
    assert shell.session.steps[-1].mapping == {
        "aws_images@V1": {
            "Pneumonia": "pneumonia",
            "Normal": "normal",
            "Effusion": "effusion",
        }
    }


def test_dedup_parses_source_priority(shell):
    run(shell, "source aws_images@V1 --image", "dedup TB-portal, DrLee ,aws_images")
    assert shell.session.steps[-1].source_priority == [
        "TB-portal",
        "DrLee",
        "aws_images",
    ]


def test_a_bad_command_leaves_the_session_intact(shell):
    """打錯字不該讓整個探索作廢。"""
    run(shell, "source aws_images@V1 --image")
    before = len(shell.session.steps)
    run(shell, "split --mod notanumber --keep 0 --seed s", "nonsense_command", "map")
    assert len(shell.session.steps) == before
    run(shell, "split --mod 2 --keep 0 --seed s")
    assert len(shell.session.steps) == before + 1


def test_checkpoint_and_rollback_from_the_command_line(shell):
    run(shell, "source aws_images@V1 --image", "checkpoint cp")
    before = shell.session.current.counts()
    run(shell, "split --mod 2 --keep 0 --seed abandoned")
    assert shell.session.current.counts() != before
    run(shell, "rollback cp")
    assert shell.session.current.counts() == before


def test_save_produces_a_spec_that_cxr_build_reproduces(shell, db, tmp_path):
    """REPL 探索 → save → cxr build，結果必須一模一樣。

    這是「三種介面共用同一套 API」真正的驗收：探索用的路徑跟正式產出
    用的路徑不能分岔。
    """
    run(
        shell,
        "source aws_images@V1 --annotation",
        "source TB-portal@V1 --annotation",
        "union",
        "dedup TB-portal,aws_images",
        "split --mod 4 --keep 0,1,2 --seed roundtrip",
        "merge_identical",
    )
    expected = shell.session.current

    out = tmp_path / "spec.yaml"
    shell.onecmd(f"save {out}")
    assert out.exists()

    replayed = execute_spec(db, BuildSpec.from_yaml(out.read_text()), Catalog(db)).final
    assert replayed.images == expected.images
    assert replayed.cls == expected.cls
    assert replayed.det == expected.det
    assert replayed.category_targets == expected.category_targets


def test_load_resumes_an_existing_spec(shell, tmp_path):
    run(shell, "source TB-portal@V1 --annotation", "merge_identical")
    out = tmp_path / "spec.yaml"
    shell.onecmd(f"save {out}")
    counts = shell.session.current.counts()

    fresh = ExploreShell(name="pytest_reload")
    try:
        fresh.onecmd(f"load {out}")
        assert fresh.session.current.counts() == counts
        # 載回來之後還能繼續往下探索
        fresh.onecmd("split --mod 2 --keep 0 --seed after-load")
        assert fresh.session.current.counts()["images"] < counts["images"]
    finally:
        fresh.db.close()


def test_commit_from_the_repl_creates_a_version(shell, db):
    run(
        shell,
        "source TB-portal@V1 --annotation",
        "merge_identical",
        f"commit -m {shell.session.name} -v V1"
        " --author-name pytest --author-email pytest@example.com",
    )
    from cxr_dataset_manager.db import crud

    version_id = crud.resolve_version(db, shell.session.name, "V1")
    assert version_id is not None
    assert crud.version_summary(db, version_id)["images"] > 0

    # the commit writes the version's __meta__.md beside its spec
    from cxr_dataset_manager.storage import get_store

    md = get_store().get_meta("manual-set", shell.session.name, "V1")
    assert md and "| TB-portal | V1 |" in md
    get_store().delete_meta("manual-set", shell.session.name, "V1")


def test_dry_run_commit_from_the_repl_writes_nothing(shell, db):
    from cxr_dataset_manager.db import crud

    run(
        shell,
        "source TB-portal@V1 --annotation",
        "merge_identical",
        f"commit -m {shell.session.name} -v V1 --dry-run",
    )
    assert crud.resolve_version(db, shell.session.name, "V1") is None


def test_checkout_moves_between_branches(shell):
    """每個 source 都開一條分支；沒有 checkout 就只能加工剛好在頭上的那條。"""
    run(shell, "source aws_images@V1 --annotation", "source DrLee@V1 --annotation")
    assert shell.session.head == "source_2"

    run(shell, "checkout source_1", "split --mod 2 --keep 0 --seed branch-a")
    assert shell.session.steps[-1].input == "source_1"

    run(shell, "checkout source_2", "split --mod 2 --keep 1 --seed branch-b")
    assert shell.session.steps[-1].input == "source_2"

    # 兩條分支都還在，union 應該把兩邊的 filter 結果接起來
    run(shell, "union")
    assert set(shell.session.steps[-1].inputs) == {"filter_1", "filter_2"}


def test_checkout_rejects_an_unknown_step(shell):
    run(shell, "source aws_images@V1 --annotation")
    before = shell.session.head
    run(shell, "checkout no_such_step")
    assert shell.session.head == before


def test_resolve_manual_keeps_the_annotation_you_name(shell):
    """conflicts 印出 annotation id，resolve manual 就用那個 id 指定留哪一筆。"""
    run(
        shell,
        "source aws_images@V1 --annotation",
        "source aws_images@V2 --annotation",
        "union",
        "merge_identical",
    )
    conflicts = shell.session.find_conflicts()
    assert conflicts, "測試前提：要有衝突"

    group = conflicts[0]
    sources = list(group.by_source.values())
    keep_id = sources[0]["annotation_ids"][0]
    doomed = [a for src in sources[1:] for a in src["annotation_ids"]]

    run(shell, f'resolve manual {keep_id} "人工判讀"')
    assert keep_id in shell.session.current.cls
    for ann_id in doomed:
        assert ann_id not in shell.session.current.cls


def test_conflicts_prints_annotation_ids(shell, capsys):
    """沒有 id 就沒東西可以指定給 resolve manual。"""
    run(
        shell,
        "source aws_images@V1 --annotation",
        "source aws_images@V2 --annotation",
        "union",
        "merge_identical",
    )
    capsys.readouterr()
    run(shell, "conflicts 1")
    out = capsys.readouterr().out
    group = shell.session.find_conflicts()[0]
    any_id = next(iter(group.by_source.values()))["annotation_ids"][0]
    assert f"#{any_id}" in out
    assert "resolve manual" in out


def test_tab_completion_handles_at_signs_and_hyphens(shell):
    """回歸測試：readline 預設把 @ 和 - 當斷詞字元，所有真實的 batch 名稱
    （aws_images@V1、TB-portal@V1）補全都是壞的，只有從頭打才有用。"""
    import readline

    assert "@" not in readline.get_completer_delims()
    assert "-" not in readline.get_completer_delims()

    for prefix in ("aws_images@", "aws_images@V"):
        assert shell.complete_source(prefix, f"source {prefix}", 7, 0), prefix
    assert shell.complete_source("TB-", "source TB-", 7, 0) == ["TB-portal@V1"]


def test_tab_is_actually_bound_on_both_readline_backends(shell, monkeypatch):
    """回歸測試：補全函式一直是對的，壞掉的是按鍵綁定——所以上面那個測試全綠，
    實際按 Tab 卻什麼都沒有。兩套 readline 的設定語法不相容，送錯不會報錯只是
    不生效，而部署目標（Ubuntu/GNU）和開發機（macOS/libedit）剛好各用一套。"""
    import readline

    issued: list[str] = []
    monkeypatch.setattr(readline, "parse_and_bind", issued.append)

    for backend, expected in [
        ("editline", "bind ^I rl_complete"),
        ("readline", "tab: complete"),
    ]:
        issued.clear()
        monkeypatch.setattr(readline, "backend", backend, raising=False)
        shell.preloop()
        assert issued == [expected], backend

    # 3.12 沒有 readline.backend，得退回去看 __doc__。
    monkeypatch.delattr(readline, "backend", raising=False)
    for doc, expected in [
        ("... using libedit readline.", "bind ^I rl_complete"),
        ("... using GNU readline.", "tab: complete"),
    ]:
        issued.clear()
        monkeypatch.setattr(readline, "__doc__", doc)
        shell.preloop()
        assert issued == [expected], doc


def test_steps_marks_the_head_and_the_open_branch_tips(shell, capsys):
    """切換分支之後 head 會停在中間，而未合併的末端才是 union 會抓的東西——
    兩者是不同的資訊，要分別標出來。"""
    run(
        shell,
        "source aws_images@V1 --annotation",
        "split --mod 2 --keep 0 --seed a",
        "source DrLee@V1 --annotation",
        "split --mod 2 --keep 1 --seed b",
        "checkout filter_1",
    )

    described = {r["step_id"]: r for r in shell.session.describe()}
    assert described["filter_1"]["head"] and not described["filter_2"]["head"]
    # 兩條 filter 都還沒被消費；兩個 source 已經被各自的 filter 消費掉了
    assert described["filter_1"]["open"] and described["filter_2"]["open"]
    assert not described["source_1"]["open"] and not described["source_2"]["open"]

    capsys.readouterr()
    run(shell, "steps")
    out = capsys.readouterr().out
    assert "← head" in out and "BRANCH END" in out
    assert "2 branches not merged yet" in out


def test_save_without_a_filename_goes_to_a_temp_file(shell, capsys):
    """探索到一半想留個底是很隨手的動作，不該逼使用者當場想一個路徑。"""
    import re
    from pathlib import Path

    run(shell, "source aws_images@V1 --annotation", "save")
    out = capsys.readouterr().out
    match = re.search(r"saved to (\S+\.yaml)", out)
    assert match, out

    path = Path(match.group(1))
    assert path.exists()
    saved = BuildSpec.from_yaml(path.read_text())
    assert saved.sha256() == shell.session.compile().sha256()
    path.unlink()


def test_load_accepts_a_version_ref_not_just_a_file(shell, db, tmp_path):
    """「基於上一版再改一版」不該逼使用者先把 spec 撈到本機。

    spec 的位置本來就由 (名稱, 版本) 算得出來，給 ref 就夠了。
    """
    from cxr_dataset_manager.core.engine import Author, build

    name = f"pytest_{uuid.uuid4().hex[:8]}"
    spec = BuildSpec.from_yaml(
        "steps:\n"
        "  - {id: aws, op: source, original_set: aws_images, annotation_batch: V1}\n"
        "  - {id: mapped, op: category_map, input: aws,\n"
        "     mapping: {'aws_images@V1': {Pneumonia: pneumonia, Normal: normal,\n"
        "                                 Effusion: effusion}}}\n"
        "final: mapped\n"
    )
    build(
        db, spec, name, "V1", author=Author(name="pytest", email="pytest@example.com")
    )
    try:
        # 解析器拿回來的必須跟當初存進去的是同一份
        assert crud.load_spec_from(db, f"{name}@V1").sha256() == spec.sha256()
        # REPL 的 load 把它接進 session（compile() 之後 sha 會變，因為 compile
        # 會把最後一個 category_map 收緊成 require_total，那是刻意的）
        run(shell, f"load {name}@V1")
        assert [st.id for st in shell.session.steps] == ["aws", "mapped"]
        assert shell.session.current.counts()["images"] > 0
    finally:
        db.execute(text("DELETE FROM manual_sets WHERE name = :n"), {"n": name})
        db.commit()


def test_load_refuses_a_minio_storage_directory_and_says_what_to_do(shell, tmp_path):
    """回歸測試：MinIO 把每個物件存成一個目錄，裡面是 xl.meta。

    直接指過去 path.exists() 是 True，read_text() 才炸出 IsADirectoryError——
    一個看不出所以然的錯誤。要明講那是內部儲存，並給出正確寫法。
    """
    obj = tmp_path / "manual-sets" / "m" / "annotations" / "V1" / "spec.yaml"
    obj.mkdir(parents=True)
    (obj / "xl.meta").write_bytes(b"not a spec")

    with pytest.raises(SpecError) as caught:
        shell.session  # 觸發 fixture
        crud.load_spec_from(shell.db, str(obj))
    message = str(caught.value)
    assert "MinIO" in message and "<manual-set>@<version>" in message


def test_a_missing_object_path_suggests_the_ref_form(shell):
    """指向物件儲存的路徑但檔案不在時，提示改用 ref，而不是只說找不到。"""
    with pytest.raises(SpecError) as caught:
        crud.load_spec_from(
            shell.db, "/data/minio/manual-sets/m/annotations/V1/spec.yaml"
        )
    assert "<manual-set>@<version>" in str(caught.value)


# ---------------------------------------------------------------------------
# Every command runs at least once
#
# This file used to cover the nine verbs whose parsing had bitten us and
# nothing else: 29 of the shell's methods had never been executed by any test.
# That is the same gap `cxr show` fell through (see fixed_issues.md), so the
# rule test_cli.py applies to `cxr` applies here too — every command runs, the
# session survives it, and nothing reports an error.
# ---------------------------------------------------------------------------


def _no_failures(capsys) -> str:
    out = capsys.readouterr().out
    assert "Traceback" not in out, out
    assert "✗" not in out, out
    return out


def test_every_command_runs_at_least_once(shell, tmp_path, capsys):
    from collections import Counter

    # with no steps at all these must say so, not crash on the empty session
    run(shell, "", "batches", "preview", "steps", "undo")
    run(shell, "source aws_images@V1 --annotation")

    catalog, current = shell.session.catalog, shell.session.current
    picks = tmp_path / "picks.txt"
    picks.write_text(
        "\n".join(sorted(catalog.image(i).file_name for i in current.images)[:20])
    )

    run(
        shell,
        "categories",  # before mapping: the "not mapped yet" table
        "merge_identical",
        "categories",  # after: every local category is mapped
        "conflicts",
        "duplicates 3",
        "images 3",
        "preview",
        "filter width >= 0",
        f"pick {picks}",
        "balance 100 --seed smoke",
        "spec",
        "debug on",
        "debug off",
    )
    assert shell.session.current.counts()["images"] > 0

    # include / exclude need an id to point at, which only exists now
    image_id = sorted(shell.session.current.images)[0]
    run(shell, f'exclude image {image_id} "smoke test"')
    assert image_id not in shell.session.current.images
    run(shell, f'include image {image_id} "back again"')
    assert image_id in shell.session.current.images

    # excluding an annotation is only legal where the image keeps a label
    per_image = Counter(catalog.cls(a).image_id for a in shell.session.current.cls)
    spare = next((i for i, n in per_image.items() if n >= 2), None)
    if spare is not None:
        doomed = next(a for a in shell.session.current.cls if catalog.cls(a).image_id == spare)
        run(shell, f'exclude cls {doomed} "wrong label"')
        assert doomed not in shell.session.current.cls

    # each set operation consumes the branches that are still open
    run(shell, "source TB-portal@V1 --annotation", "union")
    run(shell, "source DrLee@V1 --annotation", "except")
    run(shell, "source TB-portal@V1 --annotation", "intersect")
    run(shell, "checkpoint smoke", "rollback smoke")
    _no_failures(capsys)


def test_a_command_missing_its_arguments_prints_usage(shell, capsys):
    """Typing a verb with nothing after it is how people discover it."""
    run(shell, "source aws_images@V1 --annotation")
    capsys.readouterr()
    for line in (
        "source",
        "import",
        "split",
        "balance",
        "filter",
        "pick",
        "map",
        "resolve",
        "exclude",
        "include",
        "checkpoint",
        "rollback",
        "checkout",
        "load",
        "dedup --keep",
    ):
        steps_before = len(shell.session.steps)
        run(shell, line)
        out = capsys.readouterr().out
        assert "usage" in out.lower(), f"{line!r} printed no usage: {out}"
        assert "Traceback" not in out, line
        assert len(shell.session.steps) == steps_before, f"{line!r} changed the session"


def test_every_completer_offers_candidates(shell):
    """Tab completion is reachable only through these; nothing else calls them."""
    run(shell, "source aws_images@V1 --annotation")
    assert shell.complete_source("aws", "source aws", 7, 10)
    assert shell.complete_import("aws", "import aws", 7, 10)
    assert shell.complete_dedup("aws", "dedup aws", 6, 9)
    # scopes are offered while categories are still unmapped
    assert shell.complete_map("aws", "map aws", 4, 7)
    assert shell.complete_resolve("a", "resolve a", 8, 9) == ["annotator"]
    assert shell.complete_exclude("i", "exclude i", 8, 9) == ["image"]
    assert shell.complete_include("c", "include c", 8, 9) == ["cls"]

    run(shell, "checkpoint cp")
    assert shell.complete_rollback("c", "rollback c", 9, 10) == ["cp"]
    assert shell.complete_checkout("source", "checkout source", 9, 15) == ["source_1"]


def test_quit_says_what_is_about_to_be_lost(shell, capsys):
    run(shell, "source aws_images@V1 --annotation")
    capsys.readouterr()
    assert shell.onecmd("quit") is True
    assert "will not be kept" in capsys.readouterr().out


def test_ctrl_c_cancels_the_line_not_the_session(monkeypatch, capsys):
    """The session is unsaved work; Ctrl-C must not throw it away."""
    from cxr_dataset_manager.cli import repl as repl_mod

    rounds = []

    def fake_cmdloop(self, intro=""):
        rounds.append(1)
        if len(rounds) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(repl_mod.ExploreShell, "cmdloop", fake_cmdloop)
    repl_mod.run(f"pytest_{uuid.uuid4().hex[:8]}")
    assert len(rounds) == 2, "the loop must resume after a Ctrl-C"
    assert "^C" in capsys.readouterr().out
