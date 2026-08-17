"""增强依赖缺口：配置要求了什么、没装什么、装它的命令是什么。"""

from __future__ import annotations

import pytest

from travel_agent.capabilities import capability_gaps, capability_report
from travel_agent.config import Settings


def _settings(**overrides) -> Settings:
    settings = Settings()
    for dotted, value in overrides.items():
        section, field = dotted.split(".", 1)
        setattr(getattr(settings, section), field, value)
    return settings


def test_a_configuration_matching_the_install_has_no_gaps():
    """这个环境装了 local-embedding，所以默认配置应当没有缺口。"""

    assert capability_gaps(_settings()) == []


def test_a_missing_enhancement_names_its_config_item_and_install_command(monkeypatch):
    """报「缺了什么」不够：要报**哪个配置项要求的**和**敲哪一条命令**。

    一条没有可执行下一步的错误信息，读的人只能去搜索。
    """

    monkeypatch.setattr(
        "travel_agent.capabilities._missing",
        lambda *modules: list(modules),
    )
    gaps = capability_gaps(_settings(**{"rerank.provider": "cross_encoder"}))
    by_item = {gap.requested_by: gap for gap in gaps}
    gap = by_item["rerank.provider=cross_encoder"]
    assert gap.install_command == "uv sync --group cross-encoder"
    assert "sentence_transformers" in gap.missing_modules
    # 还要给出另一条路：改配置也能解决，不是只能装依赖。
    assert "rerank.provider" in gap.consequence


def test_a_disabled_enhancement_is_not_a_gap(monkeypatch):
    monkeypatch.setattr(
        "travel_agent.capabilities._missing",
        lambda *modules: list(modules),
    )
    gaps = capability_gaps(
        _settings(**{"rerank.enabled": False, "rerank.provider": "cross_encoder"})
    )
    assert all(gap.requested_by != "rerank.provider=cross_encoder" for gap in gaps)


def test_a_remote_embedding_provider_needs_no_local_stack(monkeypatch):
    monkeypatch.setattr(
        "travel_agent.capabilities._missing",
        lambda *modules: list(modules),
    )
    gaps = capability_gaps(_settings(**{"embedding.provider": "openai"}))
    assert all("embedding.provider" not in gap.requested_by for gap in gaps)


def test_the_report_blocks_readiness_when_a_gap_exists(monkeypatch):
    """这一项与空语料那一类不同：它会在第一次真正调用时抛异常，所以拦门禁。"""

    monkeypatch.setattr(
        "travel_agent.capabilities._missing",
        lambda *modules: list(modules),
    )
    report = capability_report(_settings(**{"embedding.provider": "qwen3"}))
    assert report["ready"] is False
    assert report["gaps"]
    assert "uv sync --group local-embedding" in report["message"]


def test_the_report_is_ready_when_nothing_is_missing():
    report = capability_report(_settings())
    assert report["ready"] is True
    assert report["gaps"] == []
    assert report["message"] == "ok"


@pytest.mark.parametrize("module", ["onnxruntime", "tokenizers", "numpy"])
def test_probing_never_imports_the_module(module, monkeypatch):
    """探测用 find_spec：import 一个 400 MB 的推理栈只为了知道它在不在，太贵了。"""

    import sys

    from travel_agent.capabilities import _missing

    before = set(sys.modules)
    _missing(module)
    assert module not in (set(sys.modules) - before)
