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


class FakeSession:
    def __init__(self, responses):
        self.cookies = {"USERKEY": "user", "TKEY": "token", "other": "secret"}
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def make_client(session):
    client = object.__new__(NovelpiaClient)
    client.s = session
    client.timeout = 30
    client.cancel_event = None
    client.sleep_cooperative = lambda seconds: None
    return client


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
    result = make_client(session).fetch_image(
        f"https://{host}/image.jpg",
        "https://global.novelpia.com/viewer/1",
        {
            "CloudFront-Policy": "policy",
            "CloudFront-Signature": "signature",
            "CloudFront-Key-Pair-Id": "key-id",
        },
    )

    assert result == b"image"
    headers = session.calls[0][1]["headers"]
    assert "USERKEY=user" in headers["Cookie"]
    assert "TKEY=token" in headers["Cookie"]
    assert "CloudFront-Policy" not in headers["Cookie"]
    assert "other=secret" not in headers["Cookie"]
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    "host",
    ["gn.novelpia.com", "image.novelpia.com", "img.novelpia.com", "pv-gn.novelpia.com"],
)
def test_cdn_image_fetch_sends_only_signed_cloudfront_cookies(host):
    session = FakeSession([FakeResponse()])
    result = make_client(session).fetch_image(
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
    headers = session.calls[0][1]["headers"]
    assert "USERKEY" not in headers["Cookie"]
    assert "TKEY" not in headers["Cookie"]
    assert "unexpected" not in headers["Cookie"]
    assert "CloudFront-Policy=policy" in headers["Cookie"]
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
