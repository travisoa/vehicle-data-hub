"""冻结型号校验必须保留字面值并拒绝不可恢复的切片。"""
import hashlib
import json

import pytest

from miit_gonggao.collection import catalog_main, validate_frozen_models


def test_frozen_selection_is_independent_of_catalog_and_keeps_order(tmp_path, capsys):
    path = tmp_path / "models.txt"
    path.write_text("AA1\nBB2\nCC3\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert catalog_main(["--validate-frozen-models", str(path), "--frozen-sha256", digest,
                         "--offset", "1", "--limit", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"sha256": digest, "total_models": 3, "offset": 1,
                      "limit": 1, "selected_count": 1, "models": ["BB2"]}


@pytest.mark.parametrize("contents", ["", "A\n\nB\n", "A\nA\n", "A\na\n", " A\n"])
def test_invalid_lists_are_rejected(tmp_path, contents):
    path = tmp_path / "models.txt"
    path.write_text(contents, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        validate_frozen_models(path, digest)


def test_hash_and_out_of_range_are_rejected(tmp_path):
    path = tmp_path / "models.txt"
    path.write_text("A\nB\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        validate_frozen_models(path, "0" * 64)
    with pytest.raises(ValueError):
        validate_frozen_models(path, digest, offset=2)
