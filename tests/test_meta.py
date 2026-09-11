"""core/meta.py: the __meta__.md templates are pure, so they are tested as text."""

from cxr_dataset_manager.core import meta


def test_a_multi_line_answer_stays_inside_its_list_item():
    assert meta.field("Description", "first line\n\tindented\n\n") == (
        "- **Description:**\n    first line\n        indented"
    )
    assert meta.field("Description", "   ") == "- **Description:** N/A"


def test_a_table_cell_never_breaks_the_row():
    assert meta.table_cell("kept all\nof them") == "kept all<br>of them"
    assert meta.table_cell("") == "N/A"


def test_png_dtype_comes_from_the_header_alone():
    header = meta.PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"\x00" * 8
    assert meta.png_dtype(header + bytes([16])) == "uint16"
    assert meta.png_dtype(header + bytes([8])) == "uint8"
    assert meta.png_dtype(b"GIF89a" + b"\x00" * 30) is None


def _summary(**overrides):
    summary = {
        "created_by": "Ada <ada@example.com>",
        "images": 3,
        "cls": 3,
        "det": 1,
        "composition": [
            {"original_set": "aws_images", "version": "V1", "images": 2},
            {"original_set": "DrLee", "version": "V2", "images": 1},
        ],
        "category_distribution": [
            {"target": "normal", "cls_pos": 1, "cls_neg": 2, "cls_unknown": 0,
             "det_pos": 0, "det_all": 0},
            {"target": "effusion", "cls_pos": 2, "cls_neg": 0, "cls_unknown": 1,
             "det_pos": 0, "det_all": 1},
        ],
    }
    return {**summary, **overrides}


def test_the_manual_set_template_numbers_categories_by_name():
    md = meta.render_manual_set(_summary(), {})
    # effusion sorts first, so it is 1 — the same id the parquet export gives it
    assert "| effusion | 1 | 2 | 0 | 1 |" in md
    assert "| normal | 2 | 1 | 2 | 0 |" in md
    assert "| effusion | 1 | 1 |" in md


def test_each_source_gets_its_own_selection_rule():
    answers = {
        meta.selection_rule_key("aws_images", "V1"): "every film\nwith a subject",
        "description": "Pneumonia training set",
    }
    md = meta.render_manual_set(_summary(), answers)
    assert "| aws_images | V1 | every film<br>with a subject |" in md
    assert "| DrLee | V2 | N/A |" in md
    assert "- **Description:**\n    Pneumonia training set" in md
    assert "- **Creator:**\n    Ada <ada@example.com>" in md


def test_the_image_template_says_when_the_data_type_was_sampled():
    stats = {
        "images": 10,
        "extensions": {".png": 10},
        "dtypes": {"uint16": 4},
        "dtype_checked": 4,
        "sizes": {(1024, 1024): 10},
    }
    md = meta.render_images(stats, {"additional": meta.resolution_note(stats["sizes"])})
    assert "| **Type** | RAW |" in md
    assert "| **Data Type** | `uint16` (read from 4 of 10 images) |" in md
    assert "## Process Procedure" not in md
    assert "resolution of 1024 by 1024" in md

    processed = meta.render_images(stats, {"process_procedure": "resized to 512"})
    assert "| **Type** | Processed |" in processed
    assert "## Process Procedure (Optional)\nresized to 512" in processed


def test_mixed_values_are_reported_not_hidden_behind_the_majority():
    stats = {"images": 3, "extensions": {".png": 2, ".jpg": 1}, "dtypes": {},
             "dtype_checked": 0, "sizes": {}}
    assert "mixed: `.png` ×2, `.jpg` ×1" in meta.render_images(stats, {})
