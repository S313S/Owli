from __future__ import annotations

import pytest


def test_韧性配置默认值环境无关() -> None:
    from app.config import load_resilience_config

    config = load_resilience_config({})

    assert config.plan_segment_retries == 3
    assert config.plan_chapter_lint_retries == 2
    assert config.plan_transport_retries == 3
    assert config.backoff_initial_seconds == 60
    assert config.backoff_max_seconds == 900
    assert [config.backoff_seconds(index) for index in range(6)] == [
        60, 120, 240, 480, 900, 900,
    ]


def test_韧性配置部署环境覆盖全部生效() -> None:
    from app.config import load_resilience_config

    config = load_resilience_config({
        "OWLI_PLAN_SEGMENT_RETRIES": "5",
        "OWLI_PLAN_CHAPTER_LINT_RETRIES": "6",
        "OWLI_PLAN_TRANSPORT_RETRIES": "4",
        "OWLI_BACKOFF_INITIAL_SECONDS": "7",
        "OWLI_BACKOFF_MAX_SECONDS": "21",
    })

    assert config.plan_segment_retries == 5
    assert config.plan_chapter_lint_retries == 6
    assert config.plan_transport_retries == 4
    assert config.backoff_seconds(0) == 7
    assert config.backoff_seconds(4) == 21


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OWLI_PLAN_SEGMENT_RETRIES", "-1"),
        ("OWLI_PLAN_CHAPTER_LINT_RETRIES", "0"),
        ("OWLI_PLAN_TRANSPORT_RETRIES", "0"),
        ("OWLI_BACKOFF_INITIAL_SECONDS", "abc"),
        ("OWLI_BACKOFF_MAX_SECONDS", "0"),
    ],
)
def test_韧性配置拒绝非正整数(name: str, value: str) -> None:
    from app.config import load_resilience_config

    with pytest.raises(ValueError, match=name):
        load_resilience_config({name: value})


def test_退避起点不得超过上限() -> None:
    from app.config import load_resilience_config

    with pytest.raises(ValueError, match="OWLI_BACKOFF_INITIAL_SECONDS"):
        load_resilience_config({
            "OWLI_BACKOFF_INITIAL_SECONDS": "10",
            "OWLI_BACKOFF_MAX_SECONDS": "9",
        })


def test_调研规模配置默认值环境无关() -> None:
    from app.config import load_research_scale_config

    config = load_research_scale_config()

    assert config.standard.max_goals == 7
    assert config.standard.max_sources_per_goal is None
    assert config.fast.max_goals == 3
    assert config.fast.max_sources_per_goal == 2
    assert config.fast.source_item_limits == {
        "hacker_news": 100,
        "product_hunt": 10,
        "web_search": 5,
        "x": 10,
        "xhs": 25,
        "douyin": 25,
        "reddit": 25,
    }


def test_调研规模配置可由产品配置覆盖() -> None:
    from app.config import load_research_scale_config

    config = load_research_scale_config({
        "fast": {
            "max_goals": 4,
            "max_sources_per_goal": 1,
            "source_item_limits": {"hacker_news": 42},
        }
    })

    assert config.fast.max_goals == 4
    assert config.fast.max_sources_per_goal == 1
    assert config.fast.source_item_limits["hacker_news"] == 42
    assert config.fast.source_item_limits["web_search"] == 5


def test_采集响应字节上限有部署默认值且可覆盖() -> None:
    from app.config import load_source_response_config

    default = load_source_response_config({})
    overridden = load_source_response_config({
        "OWLI_SOURCE_PAYLOAD_BYTE_LIMIT": "4096",
    })

    assert default.payload_byte_limit == 262_144
    assert overridden.payload_byte_limit == 4096


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1023"])
def test_采集响应字节上限拒绝不可用配置(value: str) -> None:
    from app.config import load_source_response_config

    with pytest.raises(ValueError, match="OWLI_SOURCE_PAYLOAD_BYTE_LIMIT"):
        load_source_response_config({"OWLI_SOURCE_PAYLOAD_BYTE_LIMIT": value})
