"""Spec 語法層：錯誤要在解析當下就擋下來，不要等到跑一半才炸。"""

import pytest
from pydantic import ValidationError

from cxr_dataset_manager.core.schema import BuildSpec

BASE = """
steps:
  - id: a
    op: source
    original_set: aws_images
    image_batch: V1
final: a
"""


def test_minimal_spec_parses():
    spec = BuildSpec.from_yaml(BASE)
    assert spec.final == "a"
    assert spec.sha256() == BuildSpec.from_yaml(spec.to_yaml()).sha256()


def test_source_needs_exactly_one_batch_kind():
    with pytest.raises(ValidationError, match="exactly one of"):
        BuildSpec.from_yaml(
            """
            steps:
              - {id: a, op: source, original_set: x, image_batch: V1, annotation_batch: V1}
            final: a
            """
        )
    with pytest.raises(ValidationError, match="exactly one of"):
        BuildSpec.from_yaml("steps: [{id: a, op: source, original_set: x}]\nfinal: a")


def test_forward_reference_is_rejected():
    with pytest.raises(ValidationError, match="before it is defined"):
        BuildSpec.from_yaml(
            """
            steps:
              - {id: a, op: dedup, input: b}
              - {id: b, op: source, original_set: x, image_batch: V1}
            final: a
            """
        )


def test_duplicate_step_id_is_rejected():
    with pytest.raises(ValidationError, match="duplicate step id"):
        BuildSpec.from_yaml(
            """
            steps:
              - {id: a, op: source, original_set: x, image_batch: V1}
              - {id: a, op: source, original_set: y, image_batch: V1}
            final: a
            """
        )


def test_final_must_exist():
    with pytest.raises(ValidationError, match="final"):
        BuildSpec.from_yaml(BASE.replace("final: a", "final: nope"))


def test_sample_filter_requires_its_params():
    with pytest.raises(ValidationError, match="mod / keep_remainder / seed"):
        BuildSpec.from_yaml(
            """
            steps:
              - {id: a, op: source, original_set: x, image_batch: V1}
              - {id: b, op: filter, input: a, criterion: sample}
            final: b
            """
        )


def test_keep_remainder_covering_everything_is_rejected():
    """留下全部餘數的 filter 不會篩掉任何東西——那是打錯字，不是有效設定。"""
    with pytest.raises(ValidationError, match="would drop nothing"):
        BuildSpec.from_yaml(
            """
            steps:
              - {id: a, op: source, original_set: x, image_batch: V1}
              - {id: b, op: filter, input: a, criterion: sample, mod: 2,
                 keep_remainder: [0, 1], seed: s}
            final: b
            """
        )


def test_import_list_needs_names_or_ref():
    with pytest.raises(ValidationError, match="file_names"):
        BuildSpec.from_yaml(
            """
            steps:
              - {id: a, op: import_list, original_set: x, image_batch: V1}
            final: a
            """
        )


def test_yaml_roundtrip_is_stable():
    spec = BuildSpec.from_yaml(
        """
        steps:
          - {id: a, op: source, original_set: aws_images, annotation_batch: V1}
          - {id: b, op: dedup, input: a, source_priority: [DrLee, aws_images]}
        final: b
        """
    )
    again = BuildSpec.from_yaml(spec.to_yaml())
    assert again.canonical_json() == spec.canonical_json()
