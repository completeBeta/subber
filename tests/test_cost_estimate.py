"""Cost estimation must follow the model that actually ran.

Before v0.11.2 the estimator used one global rate, so after switching to Hy-MT2 the
UI kept reporting Llama's price (about a quarter of the real cost).
"""
import pytest

from subber import config
from subber import library_pipeline as lp


@pytest.fixture()
def priced(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "cost:\n"
        "  input_cost_per_million: 0.10\n"
        "  output_cost_per_million: 0.20\n"
        "  chars_per_token: 2.5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    config.reload()
    sub = tmp_path / "ep.srt"
    sub.write_bytes(b"x" * 1_000_000)  # 400k tokens at 2.5 chars/token
    yield sub
    config.reload()


def test_builtin_model_prices_win_over_global_rate(priced):
    hy = lp._estimate_cost(priced, "tencent/hy-mt2-30b-a3b")
    llama = lp._estimate_cost(priced, "meta-llama/llama-3.1-8b-instruct")
    # 400k tokens × (in+out)/1M
    assert hy == pytest.approx(400_000 * (0.074 + 0.295) / 1_000_000, rel=1e-3)
    assert llama == pytest.approx(400_000 * (0.05 + 0.08) / 1_000_000, rel=1e-3)
    assert hy > llama * 2


def test_local_ollama_model_is_free(priced):
    assert lp._estimate_cost(priced, "translategemma:4b") == 0.0


def test_unknown_model_falls_back_to_global_rate(priced):
    unknown = lp._estimate_cost(priced, "some-vendor/some-model")
    assert unknown == pytest.approx(400_000 * (0.10 + 0.20) / 1_000_000, rel=1e-3)


def test_user_override_beats_builtin(priced, monkeypatch):
    cfg = priced.parent / "config.yaml"
    cfg.write_text(
        "cost:\n"
        "  chars_per_token: 2.5\n"
        "  models:\n"
        "    tencent/hy-mt2-30b-a3b:\n"
        "      input_cost_per_million: 0.5\n"
        "      output_cost_per_million: 0.5\n",
        encoding="utf-8",
    )
    config.reload()
    assert lp._estimate_cost(priced, "tencent/hy-mt2-30b-a3b") == pytest.approx(
        400_000 * 1.0 / 1_000_000, rel=1e-3
    )
