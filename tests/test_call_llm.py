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

from ref_checker.extract import Reference, _call_llm


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
