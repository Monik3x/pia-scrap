import threading
import time
from types import SimpleNamespace

import pytest

from src import api


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.cookies = {"USERKEY": "u"}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


def _ticket_block_body(code, errmsg, novel_no, episode_no):
    return {
        "statusCode": 500,
        "code": code,
        "errmsg": errmsg,
        "result": {
            "name": "NOVEL_ERROR",
            "data": {
                "novel_no": novel_no,
                "data": {"episode_no": episode_no, "novel_no": novel_no},
            },
        },
    }


def _client_with_session(session):
    client = make_client_without_init()
    client.s = session
    client.timeout = 30
    client.throttle = 0
    client.tokens = api.Tokens(login_at="login")
    client.refresh = lambda: pytest.fail("refresh should not run")
    client.login = lambda: pytest.fail("login should not run")
    client._on_rate_limit = lambda: None
    return client


def test_request_retries_server_errors(monkeypatch):
    session = FakeSession([FakeResponse(500), FakeResponse(200, {"ok": True})])
    waits = []
    monkeypatch.setattr(api, "_wait_for_retry", lambda seconds, event, reason: waits.append(reason))

    response = api.request_with_retries(session, "GET", "https://example.test/data", max_retries=2)

    assert response.status_code == 200
    assert len(session.calls) == 2
    assert waits == ["server-error backoff"]
    assert "USERKEY=u" in session.calls[0][2]["headers"]["Cookie"]


def test_request_returns_classified_ticket_500_without_backoff(monkeypatch):
    body = _ticket_block_body("0008", "novel.ADVERTISEMENT_EPISODE", 23, 2407)
    session = FakeSession([FakeResponse(500, body)])
    waits = []
    monkeypatch.setattr(api, "_wait_for_retry", lambda seconds, event, reason: waits.append(reason))

    response = api.request_with_retries(
        session, "GET", "https://example.test/v1/novel/episode", max_retries=4
    )

    assert response.status_code == 500
    assert len(session.calls) == 1
    assert waits == []


def test_episode_ticket_classifies_ad_block_without_retrying(monkeypatch):
    monkeypatch.setattr(api, "_wait_for_retry", lambda seconds, event, reason: pytest.fail(reason))
    body = _ticket_block_body("0008", "novel.ADVERTISEMENT_EPISODE", 23, 2407)
    session = FakeSession([FakeResponse(500, body)])
    client = _client_with_session(session)

    with pytest.raises(api.TicketAccessError, match="^ad-gated episode$"):
        client.episode_ticket(2407)

    assert len(session.calls) == 1


def test_episode_ticket_classifies_premium_block_without_retrying(monkeypatch):
    monkeypatch.setattr(api, "_wait_for_retry", lambda seconds, event, reason: pytest.fail(reason))
    body = _ticket_block_body("0009", "novel.PREMIUM_EPISODE", "23", "2408")
    session = FakeSession([FakeResponse(500, body)])
    client = _client_with_session(session)

    with pytest.raises(api.TicketAccessError, match="^premium episode blocked$"):
        client.episode_ticket(2408)

    assert len(session.calls) == 1


def test_episode_ticket_malformed_block_body_retries_as_unknown_500(monkeypatch):
    waits = []
    monkeypatch.setattr(api, "_wait_for_retry", lambda seconds, event, reason: waits.append(reason))
    body = {
        "code": "0008",
        "errmsg": "novel.ADVERTISEMENT_EPISODE",
        "result": {"data": {}},
    }
    session = FakeSession([FakeResponse(500, body) for _ in range(4)])
    client = _client_with_session(session)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.episode_ticket(2407)

    assert len(session.calls) == 4
    assert waits == ["server-error backoff"] * 3


def test_episode_ticket_unknown_500_still_retries(monkeypatch):
    waits = []
    monkeypatch.setattr(api, "_wait_for_retry", lambda seconds, event, reason: waits.append(reason))
    session = FakeSession(
        [
            FakeResponse(500, {"errmsg": "temporary"}),
            FakeResponse(500, {"errmsg": "temporary"}),
            FakeResponse(500, {"errmsg": "temporary"}),
            FakeResponse(200, {"result": {"token": "fixture-token"}}),
        ]
    )
    client = _client_with_session(session)

    ticket = client.episode_ticket(2409)

    assert ticket == {"result": {"token": "fixture-token"}}
    assert len(session.calls) == 4
    assert waits == ["server-error backoff"] * 3


def test_novel_maps_server_error_to_unavailable_novel(monkeypatch):
    client = make_client_without_init()
    client.s = object()
    client.timeout = 30
    client.tokens = api.Tokens(login_at="login")
    client.refresh = lambda: None
    client.login = lambda: None
    client._on_rate_limit = lambda: None
    monkeypatch.setattr(
        api,
        "request_with_retries",
        lambda *args, **kwargs: FakeResponse(
            500,
            {
                "statusCode": 500,
                "errmsg": "The novel does not exist.",
                "errmsgs": ["The novel does not exist."],
                "code": "0001",
                "result": {
                    "name": "NOVEL_ERROR",
                    "message": "The novel does not exist.",
                    "code": "0001",
                },
            },
        ),
    )

    with pytest.raises(api.NovelUnavailableError, match="Novel 123 is unassigned or unavailable"):
        client.novel(123)


def test_api_request_injects_shared_auth_kwargs():
    session = FakeSession([
        FakeResponse(401),
        FakeResponse(200, {"ok": True}),
    ])
    client = make_client_without_init()
    client.s = session
    client.timeout = 30
    client.tokens = api.Tokens(login_at="old-login")
    refreshes = []

    def refresh():
        refreshes.append("fresh-login-at")
        return "fresh-login-at"

    client.refresh = refresh
    client.login = lambda: pytest.fail("login should not run")
    client._on_rate_limit = lambda: None
    client.cancel_event = None

    response = client._api_request("GET", "https://example.test/v1/login/me")

    assert response.status_code == 200
    assert refreshes == ["fresh-login-at"]
    assert len(session.calls) == 2
    assert session.calls[0][2]["headers"]["login-at"] == "old-login"
    assert session.calls[1][2]["headers"]["login-at"] == "fresh-login-at"


def test_episode_list_maps_missing_episodes_to_no_episodes_error(monkeypatch):
    payload = {
        "statusCode": 500,
        "errmsg": "The episode does not exist.",
        "errmsgs": ["The episode does not exist."],
        "code": "0002",
        "result": {
            "name": "NOVEL_ERROR",
            "message": "The episode does not exist.",
            "code": "0002",
            "path": "/v1/novel/episode/list?novel_no=647&rows=1000&sort=ASC",
        },
    }
    response = FakeResponse(500, payload)
    assert api._response_indicates_missing_episodes(response)
    assert not api._response_indicates_missing_novel(response)

    client = make_client_without_init()
    client.s = object()
    client.timeout = 30
    client.tokens = api.Tokens(login_at="login")
    client.refresh = lambda: None
    client.login = lambda: None
    client._on_rate_limit = lambda: None
    monkeypatch.setattr(
        api,
        "request_with_retries",
        lambda *args, **kwargs: response,
    )

    with pytest.raises(api.NoEpisodesError, match="Novel 647 has no downloadable episodes"):
        client.episode_list(647, rows=1000)


@pytest.mark.parametrize(
    ("retry_after", "expected_wait"),
    [
        ("7", 7.0),
        (None, 5.0),
        ("not-a-number", 5.0),
        ("-10", 5.0),
    ],
)
def test_request_rate_limit_honors_retry_after_with_five_second_minimum(
    monkeypatch, retry_after, expected_wait
):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    session = FakeSession([FakeResponse(429, headers=headers), FakeResponse(200)])
    waits = []
    monkeypatch.setattr(
        api,
        "_wait_for_retry",
        lambda seconds, event, reason: waits.append(seconds),
    )

    response = api.request_with_retries(
        session,
        "GET",
        "https://example.test/data",
        max_retries=2,
    )

    assert response.status_code == 200
    assert waits == [expected_wait]


def test_request_returns_final_429_and_callbacks_on_exhausted_retries(monkeypatch):
    final_response = FakeResponse(429)
    session = FakeSession([FakeResponse(429), FakeResponse(429), final_response])
    waits = []
    callbacks = []
    monkeypatch.setattr(
        api,
        "_wait_for_retry",
        lambda seconds, event, reason: waits.append(seconds),
    )

    response = api.request_with_retries(
        session,
        "GET",
        "https://example.test/data",
        max_retries=3,
        on_rate_limit=lambda: callbacks.append(True),
    )

    assert response is final_response
    assert response.status_code == 429
    assert len(session.calls) == 3
    assert callbacks == [True, True, True]
    assert waits == [5.0, 5.0]


def test_request_stops_retrying_at_max_retries(monkeypatch):
    final_response = FakeResponse(500)
    session = FakeSession([FakeResponse(500), FakeResponse(500), final_response])
    waits = []
    monkeypatch.setattr(
        api,
        "_wait_for_retry",
        lambda seconds, event, reason: waits.append((seconds, reason)),
    )

    response = api.request_with_retries(
        session,
        "GET",
        "https://example.test/data",
        max_retries=3,
    )

    assert response is final_response
    assert len(session.calls) == 3
    assert waits == [
        (1.25, "server-error backoff"),
        (1.5625, "server-error backoff"),
    ]


def test_cancellation_interrupts_rate_limit_wait():
    event = threading.Event()
    session = FakeSession([FakeResponse(429), FakeResponse(200)])
    cancel_timer = threading.Timer(0.02, event.set)
    cancel_timer.start()
    started_at = time.monotonic()

    try:
        with pytest.raises(api.DownloadCancelled, match="cancelled during rate-limit wait"):
            api.request_with_retries(
                session,
                "GET",
                "https://example.test/data",
                max_retries=2,
                cancel_event=event,
            )
    finally:
        cancel_timer.join()

    assert time.monotonic() - started_at < 1.0
    assert len(session.calls) == 1


def test_request_recovers_expired_auth():
    session = FakeSession([
        FakeResponse(401),
        FakeResponse(200),
    ])

    response = api.request_with_retries(
        session, "GET", "https://example.test/data", allow_refresh=True,
        refresh_fn=lambda: "fresh-login-at",
    )

    assert response.status_code == 200
    assert session.calls[1][2]["headers"]["login-at"] == "fresh-login-at"


def test_refresh_keeps_in_memory_token_when_persistence_fails(monkeypatch, caplog):
    client = make_client_without_init()
    client._auth_lock = threading.RLock()
    client.s = object()
    client.timeout = 30
    client.tokens = api.Tokens(login_at="old-token")
    monkeypatch.setattr(
        api,
        "request_with_retries",
        lambda *args, **kwargs: FakeResponse(200, {"result": {"LOGINAT": "fresh-token"}}),
    )
    monkeypatch.setattr(api, "load_config", lambda: {})
    monkeypatch.setattr(api, "save_config", lambda config: False)

    with caplog.at_level("WARNING", logger="pia_scrap"):
        assert client.refresh() == "fresh-token"

    assert client.tokens.login_at == "fresh-token"
    assert "refreshed in memory, but could not be stored" in caplog.text


@pytest.mark.parametrize("method_name", ["login", "refresh"])
@pytest.mark.parametrize(
    "body",
    [
        {"result": []},
        {"result": {}},
        {"result": {"LOGINAT": None}},
        {"result": {"LOGINAT": ""}},
        {"result": "fresh-token"},
        ["unexpected"],
        {"LOGINAT": "fresh-token"},
    ],
)
def test_login_and_refresh_reject_malformed_authentication_payloads(
    monkeypatch, method_name, body
):
    client = make_client_without_init()
    client._auth_lock = threading.RLock()
    client.s = object()
    client.timeout = 30
    client.tokens = api.Tokens(login_at="old-token")
    client.email = "user@example.test"
    client.password = "pw"
    saves = []
    monkeypatch.setattr(
        api,
        "request_with_retries",
        lambda *args, **kwargs: FakeResponse(200, body),
    )
    monkeypatch.setattr(api, "load_config", lambda: {})
    monkeypatch.setattr(api, "save_config", lambda config: saves.append(config) or True)

    with pytest.raises(RuntimeError, match="did not contain a login token"):
        getattr(client, method_name)()

    assert client.tokens.login_at == "old-token"
    assert saves == []


def test_request_does_not_refresh_for_forbidden_access():
    session = FakeSession([
        FakeResponse(403, {"error": {"code": "SUBSCRIPTION_REQUIRED", "message": "Premium access required"}}),
    ])
    refreshes = []

    response = api.request_with_retries(
        session, "GET", "https://example.test/data", allow_refresh=True,
        refresh_fn=lambda: refreshes.append(True),
    )

    assert response.status_code == 403
    assert refreshes == []
    assert len(session.calls) == 1


def test_request_recovers_auth_specific_forbidden_response():
    session = FakeSession([
        FakeResponse(403, {"error": {"code": "TOKEN_EXPIRED", "message": "Login token expired"}}),
        FakeResponse(200),
    ])

    response = api.request_with_retries(
        session, "GET", "https://example.test/data", allow_refresh=True,
        refresh_fn=lambda: "fresh-login-at",
    )

    assert response.status_code == 200
    assert len(session.calls) == 2


def test_request_honors_pre_cancelled_event():
    event = threading.Event()
    event.set()
    with pytest.raises(api.DownloadCancelled, match="cancelled"):
        api.request_with_retries(FakeSession([]), "GET", "https://example.test", cancel_event=event)


def test_sleep_cooperative_raises_when_cancelled():
    event = threading.Event()
    event.set()
    client = object.__new__(api.NovelpiaClient)
    client.cancel_event = event

    with pytest.raises(api.DownloadCancelled, match="cancelled during wait"):
        client.sleep_cooperative(0)
    with pytest.raises(api.DownloadCancelled, match="cancelled during wait"):
        client.sleep_cooperative(5)


def make_client_without_init():
    client = object.__new__(api.NovelpiaClient)
    client.cancel_event = None
    client.sleep_cooperative = lambda seconds: None
    return client


def test_recent_novels_filters_k_premium_and_preserves_api_order(monkeypatch):
    client = make_client_without_init()
    client.s = object()
    client.timeout = 30
    client.tokens = api.Tokens(login_at="login")
    client.refresh = lambda: None
    client.login = lambda: None
    client._on_rate_limit = lambda: None
    response = FakeResponse(200, {
        "result": {"list": [
            {"novel": {"novel_no": 40, "novel_locale": "ko"}},
            {"novel": {"novel_no": 41, "novel_locale": "EN"}},
            {"novel": {"novel_no": "42", "novel_locale": "KO"}},
            {"novel": {"novel_no": 40, "novel_locale": "ko"}},
            {"novel": {"novel_no": None, "novel_locale": "ko"}},
            {"unexpected": True},
        ]}
    })
    calls = []

    def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    monkeypatch.setattr(api, "request_with_retries", fake_request)

    assert client.recent_novels() == [40, 42]
    assert calls[0][0][2].endswith("/v1/novel/list")
    assert calls[0][1]["params"] == {"rows": 30}


def test_recent_novels_rejects_invalid_row_count():
    client = make_client_without_init()
    with pytest.raises(ValueError, match="at least 1"):
        client.recent_novels(0)


def test_recent_novels_handles_unexpected_response_shape(monkeypatch):
    client = make_client_without_init()
    client.s = object()
    client.timeout = 30
    client.tokens = api.Tokens()
    client.refresh = lambda: None
    client.login = lambda: None
    client._on_rate_limit = lambda: None
    monkeypatch.setattr(
        api,
        "request_with_retries",
        lambda *args, **kwargs: FakeResponse(200, ["unexpected"]),
    )

    assert client.recent_novels() == []


def test_my_library_raises_when_cancelled(monkeypatch):
    event = threading.Event()
    event.set()
    client = make_client_without_init()
    client.cancel_event = event
    monkeypatch.setattr(
        api,
        "request_with_retries",
        lambda *args, **kwargs: pytest.fail("cancelled library fetch should not request"),
    )

    with pytest.raises(api.DownloadCancelled, match="Library fetch cancelled"):
        client.my_library()


def test_my_library_does_not_return_partial_ids_on_cancel(monkeypatch):
    event = threading.Event()
    client = object.__new__(api.NovelpiaClient)
    client.cancel_event = event
    client.s = object()
    client.timeout = 30
    client.tokens = api.Tokens(login_at="login")
    client.refresh = lambda: None
    client.login = lambda: None
    client._on_rate_limit = lambda: None
    full_page = {
        "result": {"list": [{"novel": {"novel_no": idx}} for idx in range(1, 101)]}
    }

    def fake_request(*args, **kwargs):
        event.set()
        return FakeResponse(200, full_page)

    monkeypatch.setattr(api, "request_with_retries", fake_request)

    with pytest.raises(api.DownloadCancelled, match="cancelled during wait"):
        client.my_library()


def test_my_library_skips_malformed_rows_and_preserves_order(monkeypatch):
    client = make_client_without_init()
    client.s = object()
    client.timeout = 30
    client.tokens = api.Tokens(login_at="login")
    client.refresh = lambda: None
    client.login = lambda: None
    client._on_rate_limit = lambda: None
    monkeypatch.setattr(
        api,
        "request_with_retries",
        lambda *args, **kwargs: FakeResponse(200, {
            "result": {"list": [
                {"novel": {"novel_no": 10}},
                {"novel": None},
                "not-a-dict",
                {"unexpected": True},
                {"novel": {"novel_no": "11"}},
                {"novel": {"novel_no": 0}},
                {"novel": {"novel_no": -3}},
                {"novel": {"novel_no": None}},
                {"novel": {"novel_no": 10}},
                {"novel": {"novel_no": 12, "novel_locale": "EN"}},
                {"novel": {"novel_no": 13, "novel_locale": "ko"}},
            ]}
        }),
    )

    assert client.my_library() == [10, 11, 12, 13]


def test_my_library_returns_empty_on_unexpected_response_shape(monkeypatch):
    client = make_client_without_init()
    client.s = object()
    client.timeout = 30
    client.tokens = api.Tokens()
    client.refresh = lambda: None
    client.login = lambda: None
    client._on_rate_limit = lambda: None
    monkeypatch.setattr(
        api,
        "request_with_retries",
        lambda *args, **kwargs: FakeResponse(200, ["unexpected"]),
    )

    assert client.my_library() == []


def test_fetch_episode_matches_captured_ticket_and_split_content_shapes(captured_api_samples):
    episode = captured_api_samples["episode"]
    client = make_client_without_init()
    client.episode_ticket = lambda epi_no: episode["ticket_payload"]
    client.episode_content = lambda token: episode["content_payload"]

    result = client.fetch_episode(
        episode["list_payload"]["result"]["list"][0],
        idx=1,
    )

    assert result["html"] == "<p>first</p><p>second</p><p>third</p>"
    assert result["epi_no"] == 21443
    assert result["epi_title"] == "Prologue"
    assert result["signed_key"] == episode["ticket_payload"]["result"]["signed_key"]
    assert "writer_comment" not in result["html"]


def test_fetch_episode_reports_missing_number_and_empty_content():
    client = make_client_without_init()
    assert client.fetch_episode({"epi_num": 1})["error"] == "missing episode_no"
    client.episode_ticket = lambda epi_no: {"result": {"token": "token"}}
    client.episode_content = lambda token: {"result": {"data": {}}}
    assert client.fetch_episode({"episode_no": 1})["error"] == "episode content was empty"


def test_fetch_episode_keeps_non_cancel_errors_chapter_scoped():
    client = make_client_without_init()
    client.episode_ticket = lambda epi_no: (_ for _ in ()).throw(ValueError("bad ticket"))
    assert client.fetch_episode({"episode_no": 12, "epi_title": "Prologue"}) == {
        "error": "bad ticket",
        "epi_no": 12,
        "epi_title": "Prologue",
    }

    client.episode_ticket = lambda epi_no: (_ for _ in ()).throw(
        api.DownloadCancelled("Request cancelled during wait.")
    )
    with pytest.raises(api.DownloadCancelled):
        client.fetch_episode({"episode_no": 12, "epi_title": "Prologue"})


def test_fetch_episode_maps_ad_block_to_chapter_error_dict():
    client = make_client_without_init()
    content_calls = []
    client.episode_ticket = lambda epi_no: (_ for _ in ()).throw(
        api.TicketAccessError("ad-gated episode")
    )
    client.episode_content = lambda token: content_calls.append(token) or {}

    result = client.fetch_episode({"episode_no": 2407, "epi_title": "Prologue"})

    assert result == {
        "error": "ad-gated episode",
        "epi_no": 2407,
        "epi_title": "Prologue",
    }
    assert content_calls == []


def test_episode_content_omits_login_at_and_skips_auth_recovery():
    session = FakeSession([
        FakeResponse(401, {"errmsg": "unauthorized"}),
        FakeResponse(200, {"result": {"data": {"epi_content": "<p>ok</p>"}}}),
    ])
    client = _client_with_session(session)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        client.episode_content("fixture-token")

    assert len(session.calls) == 1
    kwargs = session.calls[0][2]
    assert "login-at" not in kwargs["headers"]
    assert kwargs["params"]["_t"] == "fixture-token"
    assert "USERKEY=u" in kwargs["headers"]["Cookie"]


def test_fetch_episode_remints_ticket_on_content_403():
    ticket = {"result": {"token": "fixture-token", "signed_key": {}}}
    content_ok = {"result": {"data": {"epi_content": "<p>ok</p>"}}}
    session = FakeSession([
        FakeResponse(200, ticket),
        FakeResponse(403),
        FakeResponse(200, ticket),
        FakeResponse(200, content_ok),
    ])
    client = _client_with_session(session)
    sleeps = []
    client.sleep_cooperative = lambda seconds: sleeps.append(seconds)

    result = client.fetch_episode({"episode_no": 2407, "epi_title": "Prologue"})

    assert result["html"] == "<p>ok</p>"
    assert result["epi_no"] == 2407
    assert [call[0] for call in session.calls] == ["GET", "GET", "GET", "GET"]
    assert session.calls[0][1].endswith("/v1/novel/episode")
    assert session.calls[1][1].endswith("/v1/novel/episode/content")
    assert session.calls[2][1].endswith("/v1/novel/episode")
    assert session.calls[3][1].endswith("/v1/novel/episode/content")
    assert 1.0 in sleeps


def test_fetch_episode_gives_up_after_three_content_403s():
    ticket = {"result": {"token": "fixture-token"}}
    session = FakeSession([
        FakeResponse(200, ticket), FakeResponse(403),
        FakeResponse(200, ticket), FakeResponse(403),
        FakeResponse(200, ticket), FakeResponse(403),
        FakeResponse(200, ticket), FakeResponse(200, {"result": {"data": {"epi_content": "<p>late</p>"}}}),
    ])
    client = _client_with_session(session)

    result = client.fetch_episode({"episode_no": 2407, "epi_title": "Prologue"})

    assert result == {
        "error": "content ticket rejected",
        "epi_no": 2407,
        "epi_title": "Prologue",
    }
    assert len(session.calls) == 6


def test_fetch_episode_cancel_during_remint_wait_raises():
    ticket = {"result": {"token": "fixture-token"}}
    session = FakeSession([
        FakeResponse(200, ticket),
        FakeResponse(403),
    ])
    client = _client_with_session(session)

    def sleep(seconds):
        if seconds == api.CONTENT_REMINT_WAIT_SECONDS:
            raise api.DownloadCancelled("cancelled during remint")

    client.sleep_cooperative = sleep

    with pytest.raises(api.DownloadCancelled):
        client.fetch_episode({"episode_no": 2407, "epi_title": "Prologue"})


def test_parallel_fetch_preserves_input_order_and_reports_progress():
    client = make_client_without_init()
    client.fetch_episode = lambda episode, idx: {"epi_title": episode["epi_title"], "idx": idx}
    progress = []
    completed = []
    episodes = [{"epi_title": "one"}, {"epi_title": "two"}]

    results = client.fetch_episodes_parallel(
        episodes, max_workers=2,
        progress_cb=lambda current, total, label: progress.append((current, total, label)),
        on_complete_cb=completed.append,
    )

    assert [result["epi_title"] for result in results] == ["one", "two"]
    assert len(progress) == 2
    assert sorted(item[0] for item in progress) == [1, 2]
    assert len(completed) == 2


def test_one_worker_rate_limit_wait_does_not_block_another_worker(monkeypatch):
    rate_limit_wait_started = threading.Event()
    other_worker_finished = threading.Event()
    release_rate_limited_worker = threading.Event()

    class ConcurrentSession:
        def __init__(self):
            self.cookies = {"USERKEY": "u"}
            self.first_worker_calls = 0

        def request(self, method, url, **kwargs):
            if url.endswith("/rate-limited"):
                self.first_worker_calls += 1
                if self.first_worker_calls == 1:
                    return FakeResponse(429)
                return FakeResponse(200)

            assert rate_limit_wait_started.wait(1.0)
            other_worker_finished.set()
            release_rate_limited_worker.set()
            return FakeResponse(200)

    def wait_for_retry(seconds, event, reason):
        assert seconds == 5.0
        assert reason == "rate-limit wait"
        rate_limit_wait_started.set()
        assert release_rate_limited_worker.wait(1.0)

    session = ConcurrentSession()
    client = make_client_without_init()

    def fetch_episode(episode, idx):
        response = api.request_with_retries(
            session,
            "GET",
            f"https://example.test/{episode['path']}",
            max_retries=2,
        )
        return {
            "epi_title": episode["epi_title"],
            "status_code": response.status_code,
            "idx": idx,
        }

    client.fetch_episode = fetch_episode
    monkeypatch.setattr(api, "_wait_for_retry", wait_for_retry)

    results = client.fetch_episodes_parallel(
        [
            {"path": "rate-limited", "epi_title": "limited"},
            {"path": "unlimited", "epi_title": "unlimited"},
        ],
        max_workers=2,
    )

    assert other_worker_finished.is_set()
    assert [result["status_code"] for result in results] == [200, 200]
    assert session.first_worker_calls == 2


def test_parallel_fetch_rejects_invalid_worker_count():
    client = make_client_without_init()
    with pytest.raises(ValueError, match="at least 1"):
        client.fetch_episodes_parallel([], max_workers=0)


def test_parallel_fetch_does_not_submit_when_already_cancelled():
    event = threading.Event()
    event.set()
    client = make_client_without_init()
    client.cancel_event = event
    client.fetch_episode = lambda *args, **kwargs: pytest.fail(
        "fetch_episode should not run when cancel is already set"
    )

    with pytest.raises(
        api.DownloadCancelled, match="Download stopped by cancellation request"
    ):
        client.fetch_episodes_parallel(
            [{"epi_title": "one"}, {"epi_title": "two"}],
            max_workers=2,
            on_complete_cb=lambda result: pytest.fail(
                "on_complete_cb should not run when cancel is already set"
            ),
        )


def test_parallel_fetch_stores_completed_chapter_before_honoring_cancel():
    event = threading.Event()
    client = make_client_without_init()
    client.cancel_event = event
    completed = []

    def fetch_episode(episode, idx):
        if episode["epi_title"] == "one":
            event.set()
            return {"epi_title": "one", "html": "ok", "epi_no": 1}
        time.sleep(0.2)
        return {"epi_title": episode["epi_title"], "html": "other", "epi_no": idx}

    client.fetch_episode = fetch_episode

    with pytest.raises(api.DownloadCancelled, match="Download pool stopped"):
        client.fetch_episodes_parallel(
            [{"epi_title": "one"}, {"epi_title": "two"}],
            max_workers=2,
            on_complete_cb=completed.append,
        )

    stored_one = [item for item in completed if item.get("epi_title") == "one"]
    assert stored_one
    assert stored_one[0]["html"] == "ok"


def test_parallel_fetch_stores_finished_chapter_when_sibling_raises_cancel():
    client = make_client_without_init()
    completed = []
    html_done = threading.Event()

    def fetch_episode(episode, idx):
        if episode["epi_title"] == "ok":
            result = {"epi_title": "ok", "html": "ok", "epi_no": 1}
            html_done.set()
            return result
        html_done.wait(1.0)
        raise api.DownloadCancelled("Request cancelled during wait.")

    client.fetch_episode = fetch_episode

    with pytest.raises(api.DownloadCancelled, match="Request cancelled during wait"):
        client.fetch_episodes_parallel(
            [{"epi_title": "ok"}, {"epi_title": "raise"}],
            max_workers=2,
            on_complete_cb=completed.append,
        )

    stored_ok = [item for item in completed if item.get("epi_title") == "ok"]
    assert stored_ok
    assert stored_ok[0]["html"] == "ok"


class FakeImageResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeImageSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cookies = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _image_client(session):
    client = make_client_without_init()
    client.timeout = 30
    client.s = session
    return client


def test_fetch_image_reraises_cancellation_during_signed_key_refresh():
    client = _image_client(FakeImageSession([FakeImageResponse(403)]))
    client.cancel_event = threading.Event()

    def cancelled_signed_key(episode_no):
        raise api.DownloadCancelled("Request cancelled by user request.")

    client.episode_signed_key = cancelled_signed_key

    with pytest.raises(api.DownloadCancelled, match="cancelled"):
        client.fetch_image(
            "https://gn.novelpia.com/img.jpg",
            "https://global.novelpia.com/viewer/1",
            episode_cookies={"CloudFront-Policy": "stale"},
            episode_no=99,
        )


def test_fetch_image_keeps_renewal_failure_when_signed_key_refresh_fails(caplog):
    client = _image_client(FakeImageSession([FakeImageResponse(403)]))

    def failing_signed_key(episode_no):
        raise RuntimeError("ticket failed")

    client.episode_signed_key = failing_signed_key

    with caplog.at_level("WARNING", logger="pia_scrap"):
        result = client.fetch_image(
            "https://gn.novelpia.com/img.jpg",
            "https://global.novelpia.com/viewer/1",
            episode_cookies={"CloudFront-Policy": "stale"},
            episode_no=99,
        )

    assert result is None
    assert "could not renew image authorization" in caplog.text
    assert "ticket failed" in caplog.text
    assert "HTTP Error or Timeout" not in caplog.text
