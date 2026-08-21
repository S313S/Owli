from __future__ import annotations

import pytest


def test_韧性配置默认值环境无关() -> None:
    from app.config import load_resilience_config

    config = load_resilience_config({})

    assert config.transport_failure_threshold == 3
    assert config.plan_segment_retries == 3
    assert config.backoff_initial_seconds == 60
    assert config.backoff_max_seconds == 900
    assert config.engine_probe_interval_seconds == 300
    assert [config.backoff_seconds(index) for index in range(6)] == [
        60, 120, 240, 480, 900, 900,
    ]


def test_韧性配置部署环境覆盖全部生效() -> None:
    from app.config import load_resilience_config

    config = load_resilience_config({
        "OWLI_TRANSPORT_FAILURE_THRESHOLD": "2",
        "OWLI_PLAN_SEGMENT_RETRIES": "5",
        "OWLI_BACKOFF_INITIAL_SECONDS": "7",
        "OWLI_BACKOFF_MAX_SECONDS": "21",
        "OWLI_ENGINE_PROBE_INTERVAL_SECONDS": "11",
    })

    assert config.transport_failure_threshold == 2
    assert config.plan_segment_retries == 5
    assert config.backoff_seconds(0) == 7
    assert config.backoff_seconds(4) == 21
    assert config.engine_probe_interval_seconds == 11


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OWLI_TRANSPORT_FAILURE_THRESHOLD", "0"),
        ("OWLI_PLAN_SEGMENT_RETRIES", "-1"),
        ("OWLI_BACKOFF_INITIAL_SECONDS", "abc"),
        ("OWLI_BACKOFF_MAX_SECONDS", "0"),
        ("OWLI_ENGINE_PROBE_INTERVAL_SECONDS", "-3"),
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
