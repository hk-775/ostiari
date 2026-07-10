"""Property-based tests for ostiari.config merge algorithm."""

from hypothesis import given
from hypothesis import strategies as st

from ostiari.config import _merge

scalar_values = st.one_of(
    st.integers(),
    st.text(min_size=1, max_size=20),
    st.booleans(),
    st.floats(allow_nan=False, allow_infinity=False),
)

simple_dicts = st.dictionaries(
    keys=st.text(min_size=1, max_size=5, alphabet="abcdefgh"),
    values=scalar_values,
    max_size=5,
)


@given(base=simple_dicts)
def test_merge_with_empty_is_identity(base):
    result = _merge(base, {})
    assert result == base


@given(override=simple_dicts)
def test_merge_empty_base_returns_override(override):
    result = _merge({}, override)
    # None values are skipped, so filter them
    expected = {k: v for k, v in override.items() if v is not None}
    assert result == expected


@given(base=simple_dicts, override=simple_dicts)
def test_merge_override_keys_present(base, override):
    result = _merge(base, override)
    for key, value in override.items():
        if value is not None:
            assert result[key] == value


@given(base=simple_dicts)
def test_merge_does_not_mutate_base(base):
    original = base.copy()
    _merge(base, {"new_key": "new_value"})
    assert base == original


@given(
    base=simple_dicts,
    override1=simple_dicts,
    override2=simple_dicts,
)
def test_merge_associativity_for_scalars(base, override1, override2):
    # (base <- o1) <- o2 == base <- (o1 merged with o2)
    left = _merge(_merge(base, override1), override2)
    combined = _merge(override1, override2)
    right = _merge(base, combined)
    assert left == right


@given(base=simple_dicts)
def test_none_values_never_override(base):
    override = dict.fromkeys(base)
    result = _merge(base, override)
    assert result == base


@given(
    base_inner=simple_dicts,
    override_inner=simple_dicts,
)
def test_deep_merge_preserves_base_keys(base_inner, override_inner):
    base = {"nested": base_inner}
    override = {"nested": override_inner}
    result = _merge(base, override)
    for key in base_inner:
        if key not in override_inner or override_inner[key] is None:
            assert result["nested"][key] == base_inner[key]
