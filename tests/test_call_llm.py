"""Tests for extract._call_llm's streaming Chat Completions request.

Argo rejects long-running non-streaming Chat Completions requests (HTTP 500,
or a "streaming required for operations that may take longer than 10
minutes" error), regardless of prompt size or actual output length. _call_llm
must request stream=True and reassemble the streamed delta chunks into the
same JSON string a non-streaming call would have returned.

All tests mock openai.OpenAI — no network calls are made.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ref_checker.extract import (
    Reference,
    _call_llm,
    _extract_json_object,
    _parse_llm_json,
    _strip_markdown_fence,
    resolve_model,
)


def _make_chunk(content: str | None):
    """Build a fake streaming chunk shaped like the OpenAI SDK's."""
    delta = SimpleNamespace(content=content)
    choice = SimpleNamespace(delta=delta)
    return SimpleNamespace(choices=[choice])


def _make_empty_chunk():
    """A chunk with no choices, as OpenAI sometimes sends (e.g. the final
    usage-only chunk when stream_options include_usage is set)."""
    return SimpleNamespace(choices=[])


@pytest.fixture(autouse=True)
def _openai_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_MODEL", raising=False)


def test_call_llm_requests_streaming_and_reassembles_chunks():
    raw_json = (
        '{"references": [{"index": 1, "raw": "Smith, J. (2020). '
        'A Paper. Journal."}]}'
    )
    chunks = [_make_chunk(piece) for piece in [raw_json[:20], raw_json[20:]]]

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = iter(chunks)

    with patch("openai.OpenAI", return_value=mock_client):
        refs = _call_llm("some narrowed reference text")

    assert isinstance(refs, list)
    assert len(refs) == 1
    assert isinstance(refs[0], Reference)
    assert refs[0].index == 1

    _, kwargs = mock_client.chat.completions.create.call_args
    assert kwargs["stream"] is True


def test_call_llm_skips_chunks_with_no_content():
    raw_json = '{"references": []}'
    chunks = [
        _make_empty_chunk(),
        _make_chunk(None),
        _make_chunk(raw_json),
        _make_chunk(None),
    ]

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = iter(chunks)

    with patch("openai.OpenAI", return_value=mock_client):
        refs = _call_llm("some narrowed reference text")

    assert refs == []


def test_call_llm_raises_on_missing_references_key():
    chunks = [_make_chunk('{"not_references": []}')]

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = iter(chunks)

    with patch("openai.OpenAI", return_value=mock_client):
        with pytest.raises(ValueError, match="missing 'references' list"):
            _call_llm("some narrowed reference text")


def test_call_llm_recovers_from_markdown_fenced_json():
    """Regression test: observed live against Argo with GPT-4.1/4o and
    Claude models -- despite response_format={"type": "json_object"},
    some models wrap their JSON output in a ```json ... ``` fence.
    _call_llm must recover the JSON rather than failing outright."""
    raw_json = '{"references": [{"index": 1, "raw": "Smith, J. (2020)."}]}'
    fenced = f"```json\n{raw_json}\n```"
    chunks = [_make_chunk(fenced)]

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = iter(chunks)

    with patch("openai.OpenAI", return_value=mock_client):
        refs = _call_llm("some narrowed reference text")

    assert len(refs) == 1
    assert refs[0].index == 1


# --- resolve_model ---------------------------------------------------------


def test_resolve_model_defaults_when_neither_env_var_set():
    assert resolve_model() == "GPT-5.4"


def test_resolve_model_uses_openai_model_when_set(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
    assert resolve_model() == "gpt-4.1"


def test_resolve_model_falls_back_to_openai_api_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_MODEL", "GPT-5.4")
    assert resolve_model() == "GPT-5.4"


def test_resolve_model_prefers_openai_model_over_openai_api_model(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
    monkeypatch.setenv("OPENAI_API_MODEL", "GPT-5.4")
    assert resolve_model() == "gpt-4.1"


# --- _strip_markdown_fence ---------------------------------------------


def test_strip_markdown_fence_removes_json_fence():
    text = '```json\n{"a": 1}\n```'
    assert _strip_markdown_fence(text) == '{"a": 1}'


def test_strip_markdown_fence_removes_bare_fence():
    text = '```\n{"a": 1}\n```'
    assert _strip_markdown_fence(text) == '{"a": 1}'


def test_strip_markdown_fence_noop_without_fence():
    text = '{"a": 1}'
    assert _strip_markdown_fence(text) == text


# --- _extract_json_object -----------------------------------------------


def test_extract_json_object_finds_balanced_object():
    raw = 'Some preamble text.\n\n{"a": 1, "b": {"c": 2}}'
    extracted = _extract_json_object(raw)
    assert extracted == '{"a": 1, "b": {"c": 2}}'


def test_extract_json_object_handles_braces_in_strings():
    raw = 'Preamble {"text": "a sentence with a { brace } inside it", "n": 1}'
    extracted = _extract_json_object(raw)
    assert extracted == '{"text": "a sentence with a { brace } inside it", "n": 1}'


def test_extract_json_object_no_object_returns_unchanged():
    raw = "no json here at all"
    assert _extract_json_object(raw) == raw


# --- _parse_llm_json ------------------------------------------------------


def test_parse_llm_json_handles_clean_json():
    assert _parse_llm_json('{"a": 1}') == {"a": 1}


def test_parse_llm_json_recovers_fenced_json():
    assert _parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_llm_json_recovers_prefixed_prose():
    raw = 'Here is the JSON you requested:\n\n{"a": 1}'
    assert _parse_llm_json(raw) == {"a": 1}


def test_parse_llm_json_raises_value_error_on_unparseable_response():
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_llm_json("not json at all")
