import httpx
import pytest

from hcs_ai.cloud_provider import ProviderFailure, classify_http_failure


def _response(status, body=None, headers=None):
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    return httpx.Response(status, json=body or {}, headers=headers or {}, request=request)


@pytest.mark.parametrize(
    "status,expected",
    [(429, "rate_limit"), (500, "capacity"), (502, "capacity"), (503, "capacity"), (401, "auth"), (403, "auth")],
)
def test_classify_http_failure(status, expected):
    failure = classify_http_failure(_response(status, {"error": {"message": "boom"}}))
    assert isinstance(failure, ProviderFailure)
    assert failure.kind == expected


def test_classify_http_failure_reads_retry_after():
    failure = classify_http_failure(_response(429, headers={"Retry-After": "12"}))
    assert failure.retry_after_seconds == 12.0
