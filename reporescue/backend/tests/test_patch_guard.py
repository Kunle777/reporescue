import pytest

from app.patch_guard import PatchProposal, PatchRejected, validate_and_apply

PRICING_SOURCE = (
    "def final_price(price: float, discount: float) -> float:\n"
    '    """Return the amount charged after applying a discount."""\n'
    "    return price - discount\n"
)

TEST_SOURCE = "from src.pricing import final_price\n"


@pytest.fixture
def workspace(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "pricing.py").write_text(PRICING_SOURCE, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_pricing.py").write_text(TEST_SOURCE, encoding="utf-8")
    return tmp_path


def valid_patch(**overrides):
    fields = {
        "file": "src/pricing.py",
        "before": "    return price - discount",
        "after": "    return max(0, price - discount)",
        "reason": "Clamp the final price at zero.",
    }
    fields.update(overrides)
    return PatchProposal(**fields)


def test_valid_patch_is_applied(workspace):
    validate_and_apply(workspace, valid_patch())
    updated = (workspace / "src" / "pricing.py").read_text(encoding="utf-8")
    assert "    return max(0, price - discount)" in updated
    assert "    return price - discount\n" not in updated


def test_rejects_missing_expected_line(workspace):
    with pytest.raises(PatchRejected) as exc_info:
        validate_and_apply(workspace, valid_patch(before="    return price + discount"))
    assert str(exc_info.value) == (
        "Expected vulnerable line was not found; refusing to patch an unexpected fixture."
    )
    assert (workspace / "src" / "pricing.py").read_text(encoding="utf-8") == PRICING_SOURCE


def test_rejects_protected_test_suite(workspace):
    with pytest.raises(PatchRejected, match="protected test suite"):
        validate_and_apply(
            workspace,
            valid_patch(file="tests/test_pricing.py", before="final_price", after="patched"),
        )
    assert (workspace / "tests" / "test_pricing.py").read_text(encoding="utf-8") == TEST_SOURCE


def test_rejects_path_traversal(workspace):
    with pytest.raises(PatchRejected, match="outside the repository"):
        validate_and_apply(workspace, valid_patch(file="src/../../evil.py"))


def test_rejects_absolute_path(workspace):
    with pytest.raises(PatchRejected, match="outside the repository"):
        validate_and_apply(workspace, valid_patch(file="/tmp/evil.py"))


def test_rejects_out_of_scope_file(workspace):
    with pytest.raises(PatchRejected, match="outside the allowed demo scope"):
        validate_and_apply(workspace, valid_patch(file="README.md"))


def test_rejects_missing_target_file(workspace):
    with pytest.raises(PatchRejected, match="does not exist"):
        validate_and_apply(workspace, valid_patch(file="src/does_not_exist.py"))


def test_rejects_oversize_patch(workspace):
    with pytest.raises(PatchRejected, match="maximum demo patch size"):
        validate_and_apply(workspace, valid_patch(after="x" * 5000))
    assert (workspace / "src" / "pricing.py").read_text(encoding="utf-8") == PRICING_SOURCE
