import pytest

from src.api import NovelpiaClient


class FakeResponse:
    def __init__(self, status_code=200, content=b"image", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


AUTH_COOKIES = {"USERKEY": "user", "TKEY": "token", "other": "secret"}
SIGNED_COOKIES = {
    "CloudFront-Policy": "fixture-policy",
    "CloudFront-Signature": "fixture-signature",
    "CloudFront-Key-Pair-Id": "fixture-key-id",
}


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


@pytest.mark.parametrize(
    "host",
    ["global.novelpia.com", "d.novelpia.com", "images.novelpia.com"],
)
def test_session_image_fetch_sends_only_session_cookies(host):
    # Contaminate the image jar; curl_cffi would attach these if clear/filter failed.
    session = FakeSession([FakeResponse()], cookies=dict(AUTH_COOKIES))
    client = make_client(session)
    result = client.fetch_image(
        f"https://{host}/image.jpg",
        "https://global.novelpia.com/viewer/1",
        dict(SIGNED_COOKIES),
    )

    assert result == b"image"
    sent = cookies_curl_would_send(session, session.calls[0][1]["headers"])
    assert sent.get("USERKEY") == "user"
    assert sent.get("TKEY") == "token"
    assert "CloudFront-Policy" not in sent
    assert "other" not in sent


@pytest.mark.parametrize(
    "host",
    ["gn.novelpia.com", "image.novelpia.com", "img.novelpia.com", "pv-gn.novelpia.com"],
)
def test_cdn_image_fetch_sends_only_signed_cloudfront_cookies(host):
    # Contaminate the image jar; curl_cffi would attach these if clear/filter failed.
    session = FakeSession([FakeResponse()], cookies=dict(AUTH_COOKIES))
    client = make_client(session)
    result = client.fetch_image(
        f"https://{host}/image.jpg",
        "https://global.novelpia.com/viewer/1",
        {**SIGNED_COOKIES, "unexpected": "secret"},
    )

    assert result == b"image"
    sent = cookies_curl_would_send(session, session.calls[0][1]["headers"])
    assert "USERKEY" not in sent
    assert "TKEY" not in sent
    assert "unexpected" not in sent
    assert "other" not in sent
    assert sent.get("CloudFront-Policy") == "fixture-policy"
    assert sent.get("CloudFront-Signature") == "fixture-signature"
    assert sent.get("CloudFront-Key-Pair-Id") == "fixture-key-id"


def test_image_fetch_does_not_follow_redirect_to_unapproved_host():
    session = FakeSession([
        FakeResponse(302, headers={"Location": "https://evil.test/steal"}),
    ])
    result = make_client(session).fetch_image(
        "https://image.novelpia.com/image.jpg",
        "https://global.novelpia.com/viewer/1",
        dict(SIGNED_COOKIES),
    )

    assert result is None
    assert len(session.calls) == 1
