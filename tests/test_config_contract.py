"""配置合同：严格校验、结构迁移、环境变量覆盖、来源报告、provider capability。"""

from __future__ import annotations

import json

import pytest

from travel_agent.config import (
    CONFIG_VERSION,
    ConfigError,
    Settings,
    available_presets,
    capabilities_for,
    env_variable_reference,
    get_preset,
    load_effective_config,
    check_config_version,
    preset_model_section,
    redact,
)
from travel_agent.config.env import EnvOverrideError, apply_env_overrides
from travel_agent.config.loader import build_settings
from travel_agent.config.schema_export import field_reference_markdown, json_schema_text


# --- 严格校验 ------------------------------------------------------------- #


def test_an_unknown_field_is_rejected_with_its_path_and_value():
    """静默忽略一个拼错的字段，就是「我改了 YAML 为什么没生效」的全部来源。"""

    with pytest.raises(ConfigError) as exc:
        build_settings({"database": {"prot": 5432}})
    message = str(exc.value)
    assert "database.prot" in message
    assert "5432" in message


def test_an_out_of_range_value_names_the_range():
    with pytest.raises(ConfigError) as exc:
        build_settings({"run_budget": {"max_cost_usd": 0}})
    assert "run_budget.max_cost_usd" in str(exc.value)


def test_a_validation_error_never_prints_an_api_key():
    with pytest.raises(ConfigError) as exc:
        build_settings({"primary_model": {"api_key": "sk-real-secret", "timeout": "nope"}})
    assert "sk-real-secret" not in str(exc.value)


def test_deadline_windows_must_increase():
    with pytest.raises(ConfigError) as exc:
        build_settings({"run_deadline": {"target_seconds": 600, "delivery_seconds": 100}})
    assert "run_deadline" in str(exc.value)


def test_mcp_servers_merge_rather_than_replace():
    """用户只写 env 时，command/args/required_env 必须保留内置默认。

    整体替换会让 ``required_env=[]``，于是健康检查漏报「这个 server 缺 Key」。
    """

    from travel_agent.config.mcp_defaults import default_mcp_servers

    settings = build_settings(
        {"mcp": {"servers": {"tavily-search": {"env": {"TAVILY_API_KEY": "x"}}}}}
    )
    server = settings.mcp_servers["tavily-search"]
    # 与内置默认比，而不是与 "npx" 比：装了根 node_modules 之后 command 是本地 bin。
    builtin = default_mcp_servers()["tavily-search"]
    assert server.command == builtin.command
    assert server.args == builtin.args
    assert server.required_env == ["TAVILY_API_KEY"]
    assert server.env == {"TAVILY_API_KEY": "x"}
    assert set(settings.mcp_servers) == set(default_mcp_servers())


def test_top_level_mcp_servers_is_refused():
    """顶层 `mcp_servers` 是替换语义，会把内置的 11 个 server 整体抹掉。

    抹掉之后留下的那个没有 command，MCPManager 标 misconfigured，工具层静默归零 ——
    所以这条路直接拒绝，并指向 `mcp.servers`。
    """

    with pytest.raises(ConfigError) as exc:
        build_settings({"mcp_servers": {"tavily-search": {"env": {"TAVILY_API_KEY": "x"}}}})
    assert "mcp.servers" in str(exc.value) or "servers" in str(exc.value)


# --- 版本判定 ------------------------------------------------------------- #


def test_a_version_that_is_not_the_current_one_is_refused():
    """认不出的版本必须报错，不能当成当前版本读，也不能静默改写。"""

    for version in (CONFIG_VERSION - 1, CONFIG_VERSION + 5):
        with pytest.raises(ConfigError) as exc:
            check_config_version({"config_version": version, "database": {"host": "x"}})
        assert str(CONFIG_VERSION) in str(exc.value)


def test_a_non_empty_file_without_a_version_is_refused():
    with pytest.raises(ConfigError) as exc:
        check_config_version({"database": {"host": "x"}})
    assert "config_version" in str(exc.value)


def test_an_empty_file_needs_no_version():
    """全走默认值的空文件不必声明版本。"""

    assert check_config_version({}) == {}


def test_the_version_key_does_not_reach_the_schema():
    """``config_version`` 不是 Settings 的字段，留着它会撞上 extra=forbid。"""

    payload = check_config_version(
        {"config_version": CONFIG_VERSION, "primary_model": {"model_name": "m"}}
    )
    assert "config_version" not in payload
    assert build_settings(payload).primary_model.model_name == "m"


# --- 环境变量 ------------------------------------------------------------- #


def test_nested_env_override_applies_and_reports_its_path(monkeypatch):
    monkeypatch.setenv("JOURNEYPILOT_DATABASE__PORT", "55433")
    monkeypatch.setenv("JOURNEYPILOT_SERVER__RELOAD", "false")
    monkeypatch.setenv("JOURNEYPILOT_RUN_BUDGET__MAX_COST_USD", "2.5")
    settings = Settings()
    applied = dict(apply_env_overrides(settings))
    assert settings.database.port == 55433
    assert settings.server.reload is False
    assert settings.run_budget.max_cost_usd == 2.5
    assert applied["database.port"] == "JOURNEYPILOT_DATABASE__PORT"


def test_a_misspelled_env_variable_is_an_error_not_a_no_op(monkeypatch):
    """拼错一个字母之后静默什么都不做，正是这套前缀要修掉的东西。"""

    monkeypatch.setenv("JOURNEYPILOT_DATABSE__PORT", "5432")
    with pytest.raises(EnvOverrideError) as exc:
        apply_env_overrides(Settings())
    assert "JOURNEYPILOT_DATABSE__PORT" in str(exc.value)


def test_a_wrong_typed_env_value_is_an_error_not_a_fallback(monkeypatch):
    monkeypatch.setenv("JOURNEYPILOT_DATABASE__PORT", "not-a-number")
    with pytest.raises(EnvOverrideError) as exc:
        apply_env_overrides(Settings())
    assert "database.port" in str(exc.value)


def test_mcp_native_keys_do_not_need_the_prefix(monkeypatch):
    """MCP provider 的原生 Key 名要原样传给子进程，改前缀反而读不到。"""

    monkeypatch.setenv("TAVILY_API_KEY", "native-value")
    effective = load_effective_config()
    server = effective.settings.mcp_servers["tavily-search"]
    assert (server.env or {}).get("TAVILY_API_KEY") == "native-value"


def test_list_and_dict_fields_have_no_env_entry():
    """一个字符串表达不了一张价格表，硬要表达就是又一门小语言。"""

    reference = env_variable_reference()
    paths = set(reference.values())
    assert "database.port" in paths
    assert not any(path.startswith("model_pricing") for path in paths)
    assert not any(path.startswith("mcp_servers") for path in paths)
    assert "server.cors_origins" not in paths


# --- 来源报告 ------------------------------------------------------------- #


def test_effective_config_states_where_each_value_came_from(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "config_version: 2\ndatabase:\n  host: from-yaml\n", encoding="utf-8"
    )
    monkeypatch.setenv("CONFIG_PATH", str(path))
    monkeypatch.setenv("JOURNEYPILOT_DATABASE__PORT", "55433")

    effective = load_effective_config()
    assert effective.source_of("database.host") == "config.yaml"
    assert effective.source_of("database.port") == "environment (JOURNEYPILOT_DATABASE__PORT)"
    # 没被任何一方碰过的字段就是默认值，而这也要说出来。
    assert effective.source_of("database.pool_size") == "config default"


def test_a_missing_config_path_is_an_error_not_a_silent_default(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError):
        load_effective_config()


def test_secrets_never_survive_the_effective_report(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        'config_version: 2\nprimary_model:\n  api_key: "sk-should-not-leak"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(path))
    effective = load_effective_config()
    assert effective.settings.primary_model.api_key == "sk-should-not-leak"
    assert "sk-should-not-leak" not in json.dumps(effective.redacted())
    assert "sk-should-not-leak" not in "\n".join(effective.report_lines())


def test_redaction_reports_set_versus_unset_rather_than_a_prefix():
    """前缀加星号把密钥的一部分交出去了，而这份输出正是用户会贴出来的东西。"""

    redacted = redact({"api_key": "sk-abcdef", "password": "", "host": "localhost"})
    assert redacted == {"api_key": "<set>", "password": "<unset>", "host": "localhost"}


# --- Provider preset 与 capability ---------------------------------------- #


def test_every_shipped_preset_loads():
    ids = {preset.id for preset in available_presets()}
    assert {"deepseek", "openrouter", "minimax", "ollama"} <= ids


def test_direct_and_proxied_deepseek_get_different_capabilities():
    """同一个模型经不同上游的 capability 不同，而原来的判据是猜 base_url。

    JSON Schema 是模型与具体下游共同决定的能力，不能因为 OpenRouter 网关接受这个
    参数就承诺端到端执行；两边都走 json_object + schema prompt。它们的 reasoning
    和 token 字段方言仍不同。
    """

    direct = capabilities_for("https://api.deepseek.com")
    proxied = capabilities_for("https://openrouter.ai/api/v1")
    assert direct.supports_json_schema is False
    assert proxied.supports_json_schema is False
    assert direct.reasoning_control == "deepseek"
    assert proxied.reasoning_control == "openrouter"


def test_an_unknown_endpoint_gets_the_conservative_profile():
    """认不出的上游不假装完全兼容：保守档更啰嗦，但两边都能到。"""

    unknown = capabilities_for("https://mystery.example.com/v1")
    assert unknown.supports_json_schema is False
    assert unknown.reasoning_control == "all_dialects"
    assert unknown.token_limit_field == "both"


def test_a_local_service_on_another_port_is_not_ollama():
    """只按 host 匹配会让本机一个 vLLM 拿到 Ollama 的「不回 usage」那条声明。"""

    assert capabilities_for("http://127.0.0.1:11434/v1").supports_stream_usage is False
    assert capabilities_for("http://127.0.0.1:8000/v1").supports_stream_usage is True


def test_a_preset_section_never_contains_an_api_key():
    section = preset_model_section(get_preset("deepseek"))
    assert "api_key" not in json.dumps(section)
    assert section["primary_model"]["base_url"] == "https://api.deepseek.com"


def test_reasoning_dialects_follow_the_declaration():
    from travel_agent.models.router import _provider_extra_body

    direct = _provider_extra_body(capabilities_for("https://api.deepseek.com"), max_tokens=99)
    proxied = _provider_extra_body(
        capabilities_for("https://openrouter.ai/api/v1"), max_tokens=99
    )
    conservative = _provider_extra_body(
        capabilities_for("https://mystery.example.com/v1"), max_tokens=99
    )
    assert set(direct) == {"thinking", "max_tokens"}
    assert set(proxied) == {"reasoning", "max_tokens"}
    assert proxied["reasoning"] == {"effort": "none"}
    # 保守档全都发：认不出的那种会被对方忽略，少发一种的代价是开关静默失效。
    assert set(conservative) == {"thinking", "reasoning", "max_tokens"}


def test_json_object_downgrade_does_not_duplicate_an_embedded_schema():
    from travel_agent.models.router import _satisfy_json_object_prompt_requirement

    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    compact = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    messages = [
        {
            "role": "system",
            "content": f"只返回 JSON。<json_schema>{compact}</json_schema>",
        }
    ]

    prepared = _satisfy_json_object_prompt_requirement(
        messages,
        {"response_format": {"type": "json_object"}},
        dropped_schema=schema,
    )

    assert prepared == messages


# --- 生成物一致性 --------------------------------------------------------- #


def test_generated_config_docs_are_committed():
    """改了字段却没改文档不能合入。生成器是 `journeypilot config docs`。"""

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for relative, expected in (
        ("docs/configuration.md", field_reference_markdown()),
        ("docs/config.schema.json", json_schema_text()),
    ):
        path = root / relative
        assert path.exists(), f"{relative} 缺失：跑 `journeypilot config docs`"
        assert path.read_text(encoding="utf-8") == expected, (
            f"{relative} 与当前 schema 不一致：跑 `journeypilot config docs` 并提交"
        )


def test_the_example_config_is_valid_and_current():
    """示例配置必须能被当前 schema 读进来，并且带着当前版本号。"""

    from pathlib import Path

    from travel_agent.config import load_yaml

    root = Path(__file__).resolve().parents[1]
    raw = load_yaml(root / "config.example.yaml")
    assert raw["config_version"] == CONFIG_VERSION
    build_settings(check_config_version(raw))


def test_the_example_config_ships_no_api_key():
    from pathlib import Path

    from travel_agent.config import load_yaml

    root = Path(__file__).resolve().parents[1]
    raw = load_yaml(root / "config.example.yaml")
    for section in ("primary_model", "fast_model", "embedding"):
        assert not (raw.get(section) or {}).get("api_key"), section
