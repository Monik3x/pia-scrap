import pytest

from src.api import NovelpiaClient
from src.const import SESSION_HEADERS


class FakeResponse:
    def __init__(self, status_code=200, content=b"image", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


AUTH_COOKIES = {"USERKEY": "user", "TKEY": "token", "other": "secret"}


class FakeSession:
    def __init__(self, responses, cookies=None):
        self.cookies = {} if cookies is None else cookies
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def cookies_curl_would_send(session, headers):
    """Merge the session jar with the Cookie header the way curl_cffi does."""
    sent = {}
    jar = getattr(session, "cookies", None) or {}
    if hasattr(jar, "items"):
        sent.update({str(key): str(value) for key, value in jar.items() if value})
    cookie_header = (headers or {}).get("Cookie") or ""
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        sent[key.strip()] = value
    return sent


def make_client(session):
    client = object.__new__(NovelpiaClient)
    client.s = FakeSession([], cookies=dict(AUTH_COOKIES))
    client._image_s = session
    client.timeout = 30
    client.cancel_event = None
    client.sleep_cooperative = lambda seconds: None
    return client


def test_image_session_jar_does_not_hold_api_auth_cookies():
    client = NovelpiaClient(userkey="user", tkey="token")

    assert client.s is not client._image_s
    assert client.s.cookies.get("USERKEY") == "user"
    assert client.s.cookies.get("TKEY") == "token"
    assert not client._image_s.cookies.get("USERKEY")
    assert not client._image_s.cookies.get("TKEY")


def test_image_fetch_blocks_unapproved_hosts_without_request():
    session = FakeSession([])
    result = make_client(session).fetch_image(
        "https://evil.test/image.jpg", "https://global.novelpia.com/"
    )

    assert result is None
    assert session.calls == []


def test_image_fetch_uses_image_headers_not_json_session_headers():
    session = FakeSession([FakeResponse()])
    make_client(session).fetch_image(
        "https://image.novelpia.com/image.jpg",
        "https://global.novelpia.com/viewer/1",
        {"CloudFront-Policy": "policy"},
    )

    headers = session.calls[0][1]["headers"]
    assert headers["User-Agent"] == SESSION_HEADERS["user-agent"]
    assert headers["Accept"].startswith("image/")
    assert "application/json" not in headers["Accept"]
    assert headers["Sec-Fetch-Dest"] == "image"


@pytest.mark.parametrize(
    "host",
    ["global.novelpia.com", "d.novelpia.com", "images.novelpia.com"],
)
def test_session_image_fetch_sends_only_session_cookies(host):
    session = FakeSession([FakeResponse()])
    client = make_client(session)
    result = client.fetch_image(
        f"https://{host}/image.jpg",
        "https://global.novelpia.com/viewer/1",
        {
            "CloudFront-Policy": "policy",
            "CloudFront-Signature": "signature",
            "CloudFront-Key-Pair-Id": "key-id",
        },
    )

    assert result == b"image"
    assert client.s.calls == []
    headers = session.calls[0][1]["headers"]
    sent = cookies_curl_would_send(session, headers)
    assert sent.get("USERKEY") == "user"
    assert sent.get("TKEY") == "token"
    assert "CloudFront-Policy" not in sent
    assert "other" not in sent
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    "host",
    ["gn.novelpia.com", "image.novelpia.com", "img.novelpia.com", "pv-gn.novelpia.com"],
)
def test_cdn_image_fetch_sends_only_signed_cloudfront_cookies(host):
    session = FakeSession([FakeResponse()])
    client = make_client(session)
    result = client.fetch_image(
        f"https://{host}/image.jpg",
        "https://global.novelpia.com/viewer/1",
        {
            "CloudFront-Policy": "policy",
            "CloudFront-Signature": "signature",
            "CloudFront-Key-Pair-Id": "key-id",
            "unexpected": "secret",
        },
    )

    assert result == b"image"
    assert client.s.calls == []
    headers = session.calls[0][1]["headers"]
    sent = cookies_curl_would_send(session, headers)
    assert "USERKEY" not in sent
    assert "TKEY" not in sent
    assert "unexpected" not in sent
    assert sent.get("CloudFront-Policy") == "policy"
    assert session.calls[0][1]["allow_redirects"] is False


def test_image_fetch_does_not_follow_redirect_to_unapproved_host():
    session = FakeSession([
        FakeResponse(302, headers={"Location": "https://evil.test/steal"}),
    ])
    result = make_client(session).fetch_image(
        "https://image.novelpia.com/image.jpg",
        "https://global.novelpia.com/viewer/1",
        {"CloudFront-Policy": "policy"},
    )

    assert result is None
    assert len(session.calls) == 1
