"""Structural validation of the source adapter capability contract.

These tests guard against the metadata declared in each source module
(SUPPORTED_QUERY_KINDS, DEFAULT_DELAY) drifting out of sync with the
functions the module actually implements, or with the derived registry
values everything else (engine.py, runner.py, cli/main.py) relies on.

Pure structural checks — no network, no fixtures.
"""
from __future__ import annotations

import pytest

from ref_checker.sources import base
from ref_checker.sources.base import FN_BY_KIND as _FN_BY_KIND
from ref_checker.sources.registry import (
    DEFAULT_DELAYS,
    LIVENESS_SOURCES,
    SCHOLARLY_SOURCES,
)


class TestScholarlySourceContract:
    @pytest.mark.parametrize("src", SCHOLARLY_SOURCES, ids=lambda s: s.SOURCE_NAME)
    def test_satisfies_protocol(self, src):
        assert isinstance(src, base.ScholarlySource)

    @pytest.mark.parametrize("src", SCHOLARLY_SOURCES, ids=lambda s: s.SOURCE_NAME)
    def test_declared_kinds_have_matching_functions(self, src):
        for kind in src.SUPPORTED_QUERY_KINDS:
            fn_name = _FN_BY_KIND[kind]
            assert hasattr(src, fn_name), (
                f"{src.SOURCE_NAME} declares {kind} in SUPPORTED_QUERY_KINDS "
                f"but has no {fn_name}()"
            )

    @pytest.mark.parametrize("src", SCHOLARLY_SOURCES, ids=lambda s: s.SOURCE_NAME)
    def test_existing_functions_are_declared(self, src):
        for kind, fn_name in _FN_BY_KIND.items():
            if hasattr(src, fn_name):
                assert kind in src.SUPPORTED_QUERY_KINDS, (
                    f"{src.SOURCE_NAME} implements {fn_name}() but does not "
                    f"declare {kind} in SUPPORTED_QUERY_KINDS"
                )


class TestLivenessSourceContract:
    @pytest.mark.parametrize("src", LIVENESS_SOURCES, ids=lambda s: s.SOURCE_NAME)
    def test_satisfies_protocol(self, src):
        assert isinstance(src, base.LivenessSource)


class TestDefaultDelays:
    @pytest.mark.parametrize(
        "src", SCHOLARLY_SOURCES + LIVENESS_SOURCES, ids=lambda s: s.SOURCE_NAME
    )
    def test_registry_matches_module(self, src):
        assert DEFAULT_DELAYS[src.SOURCE_NAME] == src.DEFAULT_DELAY
