"""Structural validation of the source adapter capability contract.

These tests guard against the metadata declared in each source module
(SUPPORTED_QUERY_KINDS, DEFAULT_DELAY) drifting out of sync with the
functions the module actually implements, or with the derived registry
values everything else (engine.py, runner.py, cli/main.py) relies on.

Pure structural checks — no network, no fixtures.

``@runtime_checkable`` Protocols (base.ScholarlySource/LivenessSource)
only let isinstance() verify that an attribute/method of the right *name*
exists — not its signature. TestSourceFunctionSignatures below fills that
gap with inspect.signature() checks for the one convention every source
function (build_context, the three scholarly lookup functions, check_url)
must follow: a mandatory, positional, trailing ``ctx: SourceContext``
parameter (build_context excepted, which takes none).
"""
from __future__ import annotations

import inspect

import pytest

from ref_checker.sources import base, primo
from ref_checker.sources.base import FN_BY_KIND as _FN_BY_KIND
from ref_checker.sources.registry import (
    default_delays,
    liveness_sources,
    scholarly_sources,
)

_ALL_SOURCES = scholarly_sources() + liveness_sources()


def _assert_takes_no_params(fn, label: str) -> None:
    params = list(inspect.signature(fn).parameters.values())
    assert params == [], f"{label} should take no parameters, got {params!r}"


def _assert_trailing_ctx_param(fn, label: str) -> None:
    params = list(inspect.signature(fn).parameters.values())
    assert params, f"{label} has no parameters at all (expected a trailing ctx)"
    last = params[-1]
    assert last.name == "ctx", (
        f"{label}'s last parameter is {last.name!r}, expected a trailing "
        f"'ctx' parameter"
    )
    assert last.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    ), (
        f"{label}'s ctx parameter must be positional (found {last.kind}); "
        f"engine.py always calls it positionally (fn(*args, ctx))"
    )
    # Annotations are stringized (from __future__ import annotations), so
    # this is a string-equality check on the source, not a real type
    # check — best-effort given no static type checker runs in CI (see
    # docs/source-adapter-contract.md's "Explicitly out of scope").
    assert last.annotation in ("SourceContext", "'SourceContext'"), (
        f"{label}'s ctx parameter is annotated {last.annotation!r}, "
        f"expected 'SourceContext'"
    )


class TestScholarlySourceContract:
    @pytest.mark.parametrize("src", scholarly_sources(), ids=lambda s: s.SOURCE_NAME)
    def test_satisfies_protocol(self, src):
        assert isinstance(src, base.ScholarlySource)

    @pytest.mark.parametrize("src", scholarly_sources(), ids=lambda s: s.SOURCE_NAME)
    def test_declared_kinds_have_matching_functions(self, src):
        for kind in src.SUPPORTED_QUERY_KINDS:
            fn_name = _FN_BY_KIND[kind]
            assert hasattr(src, fn_name), (
                f"{src.SOURCE_NAME} declares {kind} in SUPPORTED_QUERY_KINDS "
                f"but has no {fn_name}()"
            )

    @pytest.mark.parametrize("src", scholarly_sources(), ids=lambda s: s.SOURCE_NAME)
    def test_existing_functions_are_declared(self, src):
        for kind, fn_name in _FN_BY_KIND.items():
            if hasattr(src, fn_name):
                assert kind in src.SUPPORTED_QUERY_KINDS, (
                    f"{src.SOURCE_NAME} implements {fn_name}() but does not "
                    f"declare {kind} in SUPPORTED_QUERY_KINDS"
                )


class TestLivenessSourceContract:
    @pytest.mark.parametrize("src", liveness_sources(), ids=lambda s: s.SOURCE_NAME)
    def test_satisfies_protocol(self, src):
        assert isinstance(src, base.LivenessSource)


class TestDefaultDelays:
    @pytest.mark.parametrize(
        "src", scholarly_sources() + liveness_sources(), ids=lambda s: s.SOURCE_NAME
    )
    def test_registry_matches_module(self, src):
        assert default_delays()[src.SOURCE_NAME] == src.DEFAULT_DELAY


class TestSourceFunctionSignatures:
    """isinstance() against a runtime_checkable Protocol only checks that
    an attribute/method of the right name exists, never its signature.
    These tests use inspect.signature() to verify the one calling
    convention every source function actually relies on: engine.py calls
    every lookup/check_url function as ``fn(*args, ctx)`` (ctx positional,
    trailing) and every build_context() with no arguments at all
    (registry.py:build_all_contexts, ThreadLocalSourceContexts.get,
    engine.py:_ctx_for).
    """

    @pytest.mark.parametrize("src", _ALL_SOURCES, ids=lambda s: s.SOURCE_NAME)
    def test_build_context_takes_no_parameters(self, src):
        _assert_takes_no_params(
            src.build_context, f"{src.SOURCE_NAME}.build_context"
        )

    @pytest.mark.parametrize("src", scholarly_sources(), ids=lambda s: s.SOURCE_NAME)
    def test_lookup_functions_have_trailing_ctx(self, src):
        for kind in src.SUPPORTED_QUERY_KINDS:
            fn_name = _FN_BY_KIND[kind]
            fn = getattr(src, fn_name)
            _assert_trailing_ctx_param(fn, f"{src.SOURCE_NAME}.{fn_name}")

    @pytest.mark.parametrize("src", liveness_sources(), ids=lambda s: s.SOURCE_NAME)
    def test_check_url_has_trailing_ctx(self, src):
        _assert_trailing_ctx_param(src.check_url, f"{src.SOURCE_NAME}.check_url")


class TestPrimoModuleContract:
    """Verify the primo module satisfies the ScholarlySource Protocol and
    has the correct function signatures unconditionally — regardless of
    whether it is enabled (i.e. whether it appears in scholarly_sources()).
    """

    def test_satisfies_scholarly_source_protocol(self):
        assert isinstance(primo, base.ScholarlySource)

    def test_declared_kinds_have_matching_functions(self):
        for kind in primo.SUPPORTED_QUERY_KINDS:
            fn_name = _FN_BY_KIND[kind]
            assert hasattr(primo, fn_name), (
                f"primo declares {kind} in SUPPORTED_QUERY_KINDS but has no {fn_name}()"
            )

    def test_build_context_takes_no_parameters(self):
        _assert_takes_no_params(primo.build_context, "primo.build_context")

    def test_lookup_functions_have_trailing_ctx(self):
        for kind in primo.SUPPORTED_QUERY_KINDS:
            fn_name = _FN_BY_KIND[kind]
            fn = getattr(primo, fn_name)
            _assert_trailing_ctx_param(fn, f"primo.{fn_name}")

    def test_is_enabled_is_callable(self):
        assert callable(primo.is_enabled)
