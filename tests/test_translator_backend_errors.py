"""Backend-response edge cases that used to abort the translation failover.

Production evidence (2026-09-13 02:48):
    [WARNING] Translation backend Llama 3.1 8B (OpenRouter) failed:
              'NoneType' object has no attribute 'strip'
    [WARNING] Translation backend DeepSeek V4 Flash failed:
              Translation API call failed after 1 attempts
    [ERROR]   Pipeline error for .../Rosario to Vampire - 03.mkv:
              All translation backends failed: Translation API call failed after 1 attempts

An OpenAI-compatible server answers ``{"message": {"content": null}}`` for an
empty/filtered completion; ``.get("content", "")`` does not cover an explicit
null, so ``content.strip()`` raised AttributeError — which was not in the caught
tuple, so it skipped the retry loop entirely and took the backend (and, with only
one backend left, the whole file) down with it.
"""
from unittest import mock

import httpx
import pytest

from subber.translator import Translator


def _resp(payload, status_code=200):
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"),
    )


def test_null_content_returns_empty_instead_of_raising():
    t = Translator(api_key="x", model="m", max_retries=1)
    payload = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
    with mock.patch.object(httpx, "post", return_value=_resp(payload)):
        assert t._call_api([{"role": "user", "content": "hi"}]) == ""


def test_completion_style_null_text_returns_empty():
    t = Translator(api_key="x", model="m", max_retries=1)
    payload = {"choices": [{"text": None}]}
    with mock.patch.object(httpx, "post", return_value=_resp(payload)):
        assert t._call_api([{"role": "user", "content": "hi"}]) == ""


def test_missing_message_key_returns_empty():
    t = Translator(api_key="x", model="m", max_retries=1)
    payload = {"choices": [{"finish_reason": "content_filter"}]}
    with mock.patch.object(httpx, "post", return_value=_resp(payload)):
        assert t._call_api([{"role": "user", "content": "hi"}]) == ""


def test_retries_then_reports_the_underlying_cause():
    t = Translator(api_key="x", model="m", max_retries=2)
    with mock.patch.object(httpx, "post",
                           return_value=_resp({"error": "rate limited"}, status_code=429)):
        with pytest.raises(RuntimeError) as err:
            t._call_api([{"role": "user", "content": "hi"}])
    msg = str(err.value)
    assert "after 2 attempts" in msg
    # the cause must survive into the message — the old code dropped it, which
    # is why production only ever said "failed after 1 attempts"
    assert "400" in msg or "429" in msg or "rate" in msg.lower()
