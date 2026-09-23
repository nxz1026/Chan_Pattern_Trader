"""Smoke test: ensure all CPT sub-packages import cleanly.

This is the only test in M0. It exists to verify that the package skeleton
defined in docs/architecture.md (application/engine/domain/adapters/storage/llm)
is structurally sound before any business logic lands in M1+.
"""

from __future__ import annotations


def test_top_level_package_imports() -> None:
    import cpt

    assert cpt.__name__ == "cpt"


def test_all_subpackages_import() -> None:
    """Each architectural layer must be importable as a sub-package."""
    for layer in (
        "cpt.application",
        "cpt.engine",
        "cpt.domain",
        "cpt.adapters",
        "cpt.storage",
    ):
        module = __import__(layer, fromlist=["__name__"])
        assert module.__name__ == layer


def test_tests_package_is_a_package() -> None:
    import tests

    assert tests.__name__ == "tests"
