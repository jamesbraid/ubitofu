"""The 0.3 dependency floor is present and importable."""


def test_deepdiff_is_a_runtime_dependency():
    import deepdiff  # noqa: F401

    from deepdiff import DeepDiff  # runtime API used by reconcile

    assert DeepDiff({"a": 1}, {"a": 2})  # non-empty diff proves it works


def test_hypothesis_available_for_property_tests():
    import hypothesis  # noqa: F401
    from hypothesis import given, strategies as st  # noqa: F401


def test_pytest_cov_plugin_installed():
    import pytest_cov  # noqa: F401
