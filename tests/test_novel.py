import logging
from types import SimpleNamespace

import pytest

from src.novel import (
    NoEpisodesError,
    NoMetadataError,
    fetch_novel_and_episodes,
    html_from_episode_text,
    parse_novel_metadata,
    user_subscription_status,
)


def test_html_from_episode_text_repairs_lazy_images():
    raw = (
        '<p>Hello</p><img data-src="//cdn.test/a.jpg" style="width: 1px" '
        'srcset="a 1x" data-srcset="b 2x"><img data-original="/b.png">'
    )
    result = html_from_episode_text(raw)

    assert "https://cdn.test/a.jpg" in result
    assert "https://global.novelpia.com/b.png" in result
    assert "style=" not in result
    assert "srcset=" not in result
    assert not result.startswith("<html")


def test_html_from_episode_text_removes_active_content_and_unsafe_attributes():
    raw = (
        '<script>alert(1)</script><iframe src="https://evil.test"></iframe>'
        '<p onclick="alert(2)" style="background:url(https://evil.test)">Text '
        '<strong>bold</strong></p><a href="javascript:alert(3)" onmouseover="x">bad</a>'
        '<a href="https://example.test/page" title="safe">safe</a>'
    )
    result = html_from_episode_text(raw)

    assert "script" not in result
    assert "iframe" not in result
    assert "onclick" not in result
    assert "style=" not in result
    assert "javascript:" not in result
    assert "<strong>bold</strong>" in result
    assert 'href="https://example.test/page"' in result


def test_fetch_novel_and_episodes_filters_webtoon_before_limit(novel_data):
    episode_payload = {
        "result": {
            "list": [
                {
                    "episode_no": 1,
                    "epi_title": "Webtoon prologue",
                    "flag_content": 1,
                    "flag_type": 0,
                },
                *[
                    {
                        "episode_no": n,
                        "epi_title": f"Chapter {n}",
                        "flag_content": 0,
                        "flag_type": 1,
                    }
                    for n in range(2, 8)
                ],
            ]
        }
    }
    client = SimpleNamespace(
        novel=lambda novel_id: novel_data,
        episode_list=lambda novel_id, rows: episode_payload,
    )

    _, selected, _ = fetch_novel_and_episodes(client, 42, max_chapters=5)

    assert len(selected) == 5
    assert selected == episode_payload["result"]["list"][1:6]
    assert all(
        not (episode["flag_content"] == 1 and episode["flag_type"] == 0)
        for episode in selected
    )


@pytest.mark.parametrize(
    "episode",
    [
        {"flag_content": 1, "flag_type": 1},
        {"flag_content": 0, "flag_type": 0},
        {"flag_content": 1},
        {"flag_type": 0},
    ],
)
def test_fetch_novel_and_episodes_requires_both_webtoon_flags(
    novel_data, episode
):
    episode = {"episode_no": 101, "epi_title": "Chapter", **episode}
    client = SimpleNamespace(
        novel=lambda novel_id: novel_data,
        episode_list=lambda novel_id, rows: {"result": {"list": [episode]}},
    )

    _, selected, _ = fetch_novel_and_episodes(client, 42)

    assert selected == [episode]


def test_parse_novel_metadata_matches_captured_api_shapes(captured_api_samples):
    for case in captured_api_samples["novels"]:
        expected = case["expected"]
        metadata = parse_novel_metadata(case["payload"])

        assert metadata.novel_id == expected["novel_id"]
        assert metadata.title == expected["title"]
        assert metadata.author == expected["author"]
        assert metadata.episode_count == expected["episode_count"]
        assert metadata.status == expected["status"], expected["novel_id"]
        assert metadata.tags[0] == expected["first_tag"]


def test_parse_novel_metadata_reads_live_info_counts():
    metadata = parse_novel_metadata(
        {
            "result": {
                "novel": {
                    "novel_no": 1624,
                    "novel_name": "Little Raccoon Spirit",
                    "count_epi": 450,
                    "flag_complete": 1,
                },
                "info": {
                    "epi_cnt": 450,
                    "free_epi_cnt": 31,
                    "ad_epi_cnt": 70,
                    "premium_epi_cnt": 349,
                },
                "writer_list": [{"writer_name": "geomeunhakja"}],
            }
        }
    )

    assert metadata.episode_count == 450
    assert metadata.free_episode_count == 31
    assert metadata.ad_episode_count == 70
    assert metadata.premium_episode_count == 349


def test_fetch_novel_and_episodes_accepts_captured_prologue_shape(captured_api_samples):
    novel_case = captured_api_samples["novels"][0]
    episode_payload = captured_api_samples["episode"]["list_payload"]
    requested_rows = []
    client = SimpleNamespace(
        novel=lambda novel_id: novel_case["payload"],
        episode_list=lambda novel_id, rows: requested_rows.append(rows) or episode_payload,
    )

    _, episodes, _ = fetch_novel_and_episodes(client, 86, max_chapters=1)

    assert requested_rows == [1577]
    assert episodes == [
        {
            "episode_no": 21443,
            "novel_no": 86,
            "epi_num": 0,
            "epi_title": "Prologue",
            "flag_open": 1,
        }
    ]


@pytest.mark.parametrize("payload", [None, {}, {"result": {}}, {"result": {"novel": []}}])
def test_parse_novel_metadata_rejects_invalid_payload(payload):
    with pytest.raises(NoMetadataError, match="no metadata"):
        parse_novel_metadata(payload, 42)


def test_fetch_novel_and_episodes_keeps_invalid_id_as_value_error(novel_data):
    client = SimpleNamespace(
        novel=lambda novel_id: novel_data,
        episode_list=lambda novel_id, rows: {"result": {"list": []}},
    )
    with pytest.raises(ValueError, match="invalid novel ID") as caught:
        fetch_novel_and_episodes(client, "nope")
    assert not isinstance(caught.value, NoMetadataError)


@pytest.mark.parametrize(
    ("novel_response", "episode_response", "message"),
    [
        ({}, {}, "returned no metadata"),
        (
            {"result": {"novel": {"count_epi": 1}, "info": {"epi_cnt": 1}}},
            [],
            "invalid episode response",
        ),
        (
            {"result": {"novel": {"count_epi": 1}, "info": {"epi_cnt": 1}}},
            {"result": {"list": {}}},
            "invalid episode list",
        ),
    ],
)
def test_fetch_novel_and_episodes_validates_api_shapes(novel_response, episode_response, message):
    client = SimpleNamespace(
        novel=lambda novel_id: novel_response,
        episode_list=lambda novel_id, rows: episode_response,
    )
    expected = NoMetadataError if "no metadata" in message else ValueError
    with pytest.raises(expected, match=message):
        fetch_novel_and_episodes(client, 42)


def test_fetch_novel_and_episodes_skips_list_call_when_epi_cnt_is_zero():
    novel_response = {
        "result": {
            "novel": {
                "novel_no": 647,
                "novel_name": "I Became an Idol Who Tears Through the Entertainment Industry",
                "novel_story": "I Got TS-ed, But It's Actually Better.",
                "flag_complete": 1,
                "count_epi": 186,
            },
            "info": {"epi_cnt": 0, "free_epi_cnt": 0, "ad_epi_cnt": 0, "premium_epi_cnt": 0},
            "writer_list": [{"writer_name": "palmiho"}],
            "tag_list": [{"tag_name": "Gender Bender"}],
        }
    }
    calls = []
    client = SimpleNamespace(
        novel=lambda novel_id: novel_response,
        episode_list=lambda novel_id, rows: calls.append((novel_id, rows)) or {},
    )

    with pytest.raises(NoEpisodesError, match="has no downloadable episodes"):
        fetch_novel_and_episodes(client, 647)

    assert calls == []


def test_fetch_novel_and_episodes_rejects_empty_episode_list(novel_data):
    client = SimpleNamespace(
        novel=lambda novel_id: novel_data,
        episode_list=lambda novel_id, rows: {"result": {"list": []}},
    )

    with pytest.raises(NoEpisodesError, match="has no downloadable episodes"):
        fetch_novel_and_episodes(client, 42)


def test_fetch_novel_and_episodes_rejects_all_webtoon_episode_list(novel_data):
    client = SimpleNamespace(
        novel=lambda novel_id: novel_data,
        episode_list=lambda novel_id, rows: {
            "result": {
                "list": [
                    {
                        "episode_no": 1,
                        "epi_title": "Webtoon only",
                        "flag_content": 1,
                        "flag_type": 0,
                    }
                ]
            }
        },
    )

    with pytest.raises(NoEpisodesError, match="has no downloadable episodes"):
        fetch_novel_and_episodes(client, 42)


def test_user_subscription_status_paid_free_unknown():
    assert user_subscription_status({
        "result": {"subscription": {}, "login": {"mem_plus_type": 0}},
    }) == "paid"
    assert user_subscription_status({
        "result": {"login": {"mem_plus_type": 0}},
    }) == "free"
    assert user_subscription_status({
        "result": {"login": {"mem_plus_type": 1}},
    }) == "paid"
    assert user_subscription_status({
        "result": {"login": {"mem_plus_type": 2}},
    }) == "paid"
    assert user_subscription_status({
        "result": {"login": {"mem_plus_type": "0"}},
    }) == "free"
    assert user_subscription_status({}) == "unknown"
    assert user_subscription_status(None) == "unknown"
    assert user_subscription_status({"result": {}}) == "unknown"


def test_fetch_novel_and_episodes_logs_account_and_counts(novel_data, caplog):
    novel_payload = {
        "result": {
            "novel": novel_data["result"]["novel"],
            "info": {
                "epi_cnt": 2,
                "free_epi_cnt": 1,
                "ad_epi_cnt": 4,
                "premium_epi_cnt": 10,
            },
            "writer_list": novel_data["result"]["writer_list"],
            "tag_list": novel_data["result"]["tag_list"],
        }
    }
    episode_payload = {
        "result": {
            "list": [
                {
                    "episode_no": 1,
                    "epi_title": "One",
                    "flag_content": 0,
                    "flag_type": 1,
                }
            ]
        }
    }
    client = SimpleNamespace(
        me=lambda: {
            "result": {"login": {"mem_nick": "tester", "mem_plus_type": 0}},
        },
        novel=lambda novel_id: novel_payload,
        episode_list=lambda novel_id, rows: episode_payload,
    )

    with caplog.at_level(logging.INFO, logger="pia_scrap"):
        fetch_novel_and_episodes(client, 42)

    assert "account=free nick='tester'" in caplog.text
    assert "free=1 ad=4 premium=10" in caplog.text
