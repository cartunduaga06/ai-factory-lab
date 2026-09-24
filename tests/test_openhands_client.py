"""Tests for the OpenHands HTTP client and its security boundary.

Everything runs against an injected in-memory transport: no network, no live
server. The security assertions are the point — no token, header or raw body may
survive into an error message, a ``repr`` or a formatted traceback.
"""

from __future__ import annotations

import json
import traceback

import pytest

from factory.integrations.openhands.client import (
    OpenHandsClient,
    OpenHandsConfigurationError,
    OpenHandsConnectionError,
    OpenHandsError,
    OpenHandsRequestError,
    OpenHandsResponseError,
    ServerResponse,
    bound,
    mask_url,
    redact,
)
from tests.fake_openhands import FakeTransport

BASE_URL = "http://localhost:60000"
SESSION_KEY = "session-key-must-not-leak"
CONVERSATION_ID = "1f4b915f-c720-4b05-bd9b-093f7abdbc85"


def _client(transport: FakeTransport) -> OpenHandsClient:
    return OpenHandsClient(BASE_URL, session_api_key=SESSION_KEY, transport=transport)


# -- transport-independent helpers ----------------------------------------


def test_mask_url_hides_embedded_userinfo() -> None:
    assert mask_url("https://user:pass@host:1234/x") == "https://***@host:1234/x"
    assert mask_url("http://localhost:60000") == "http://localhost:60000"


def test_redact_removes_known_values_and_token_shapes() -> None:
    text = f"token {SESSION_KEY} and sk-abc123XYZ and gAAAAAencrypted and Bearer abc.def"
    cleaned = redact(text, [SESSION_KEY])
    assert SESSION_KEY not in cleaned
    assert "sk-abc123XYZ" not in cleaned
    assert "gAAAAAencrypted" not in cleaned
    assert "Bearer abc.def" not in cleaned


def test_bound_collapses_and_truncates() -> None:
    assert bound("a\n\n  b", 100) == "a b"
    assert bound("x" * 500, 10) == "x" * 10 + "..."


# -- repr / configuration --------------------------------------------------


def test_repr_never_exposes_the_session_key() -> None:
    client = _client(FakeTransport())
    assert SESSION_KEY not in repr(client)
    assert "localhost:60000" in repr(client)


def test_empty_base_url_is_rejected() -> None:
    with pytest.raises(OpenHandsConfigurationError):
        OpenHandsClient("   ")


def test_requests_carry_the_session_key_header() -> None:
    transport = FakeTransport([ServerResponse(200, {"id": CONVERSATION_ID})])
    _client(transport).create_conversation({"workspace": {}})
    assert transport.last.headers.get("X-Session-API-Key") == SESSION_KEY


def test_requests_are_sent_to_the_configured_base_url() -> None:
    transport = FakeTransport([ServerResponse(200, {"id": CONVERSATION_ID})])
    _client(transport).create_conversation({"workspace": {}})
    assert transport.last.url == f"{BASE_URL}/api/conversations"
    assert transport.last.method == "POST"


# -- status handling -------------------------------------------------------


def test_http_error_is_sanitized() -> None:
    transport = FakeTransport([ServerResponse(500, {"detail": f"boom with {SESSION_KEY}"})])
    with pytest.raises(OpenHandsRequestError) as caught:
        _client(transport).create_conversation({"workspace": {}})
    message = str(caught.value)
    assert SESSION_KEY not in message
    assert "500" in message
    assert caught.value.status == 500


def test_response_without_a_json_object_is_rejected() -> None:
    transport = FakeTransport([ServerResponse(200, body="not-an-object")])
    with pytest.raises(OpenHandsResponseError):
        _client(transport).create_conversation({"workspace": {}})


def test_get_conversation_returns_none_on_404() -> None:
    transport = FakeTransport([ServerResponse(404, {"detail": "gone"})])
    assert _client(transport).get_conversation(CONVERSATION_ID) is None


def test_get_conversation_returns_the_descriptor() -> None:
    transport = FakeTransport(
        [ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "running"})]
    )
    descriptor = _client(transport).get_conversation(CONVERSATION_ID)
    assert descriptor is not None
    assert descriptor["execution_status"] == "running"


def test_interrupt_conversation_is_true_when_delivered() -> None:
    transport = FakeTransport([ServerResponse(200, {"success": True})])
    assert _client(transport).interrupt_conversation(CONVERSATION_ID) is True


def test_interrupt_conversation_is_false_when_already_gone() -> None:
    transport = FakeTransport([ServerResponse(404, {"detail": "gone"})])
    assert _client(transport).interrupt_conversation(CONVERSATION_ID) is False


def test_agent_final_response_returns_text() -> None:
    transport = FakeTransport([ServerResponse(200, {"response": "OK"})])
    assert _client(transport).agent_final_response(CONVERSATION_ID) == "OK"


def test_agent_final_response_is_none_when_empty() -> None:
    transport = FakeTransport([ServerResponse(200, {"response": "   "})])
    assert _client(transport).agent_final_response(CONVERSATION_ID) is None


# -- failure normalization -------------------------------------------------


def test_transport_failure_is_normalized() -> None:
    with pytest.raises(OpenHandsConnectionError) as caught:
        _client(FakeTransport(raise_on_send=True)).create_conversation({"workspace": {}})
    assert "server" in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_network_failure_never_reveals_the_url_or_key() -> None:
    try:
        _client(FakeTransport(raise_on_send=True)).create_conversation({"workspace": {}})
    except OpenHandsError as exc:
        formatted = "".join(traceback.format_exception(exc))
        assert SESSION_KEY not in formatted
        assert BASE_URL not in formatted


ARBITRARY_SECRET = "MY_PRIVATE_LLM_PASSWORD_93726"


def test_arbitrary_error_body_secret_is_discarded() -> None:
    # This value matches no known token shape and is not the session key, so
    # partial redaction could never have been safe. It must be discarded whole.
    transport = FakeTransport(
        [
            ServerResponse(
                500,
                {"detail": f"provider rejected credential {ARBITRARY_SECRET}"},
            )
        ]
    )
    with pytest.raises(OpenHandsRequestError) as caught:
        _client(transport).create_conversation({"workspace": {}})

    error = caught.value
    assert error.status == 500
    assert ARBITRARY_SECRET not in str(error)
    assert ARBITRARY_SECRET not in repr(error)
    assert all(ARBITRARY_SECRET not in repr(value) for value in vars(error).values())
    assert error.__cause__ is None
    assert error.__context__ is None
    assert ARBITRARY_SECRET not in "".join(traceback.format_exception(error))
    # No remote body text is retained under any attribute.
    assert not any("provider rejected" in repr(value) for value in vars(error).values())


def test_remote_error_body_text_is_not_retained() -> None:
    transport = FakeTransport(
        [ServerResponse(422, {"detail": "some remote diagnostic text", "message": "more"})]
    )
    with pytest.raises(OpenHandsRequestError) as caught:
        _client(transport).create_conversation({"workspace": {}})
    message = str(caught.value)
    assert "some remote diagnostic text" not in message
    assert "more" not in message
    assert message == "OpenHands agent server returned status 422"


def test_non_string_error_detail_is_discarded() -> None:
    # A structured payload must not be echoed into a message.
    transport = FakeTransport([ServerResponse(422, {"detail": [{"msg": "x", "loc": ["body"]}]})])
    with pytest.raises(OpenHandsRequestError) as caught:
        _client(transport).create_conversation({"workspace": {}})
    assert "loc" not in str(caught.value)


def test_verbose_error_body_is_discarded() -> None:
    transport = FakeTransport([ServerResponse(500, {"detail": "y" * 5000})])
    with pytest.raises(OpenHandsRequestError) as caught:
        _client(transport).create_conversation({"workspace": {}})
    assert len(str(caught.value)) < 400
    assert "yyy" not in str(caught.value)


def test_payload_is_json_encoded() -> None:
    transport = FakeTransport([ServerResponse(200, {"id": CONVERSATION_ID})])
    _client(transport).create_conversation({"workspace": {"working_dir": "/tmp/x"}})
    assert json.loads(transport.last.body.decode()) == {"workspace": {"working_dir": "/tmp/x"}}
    assert transport.last.headers.get("Content-Type") == "application/json"
