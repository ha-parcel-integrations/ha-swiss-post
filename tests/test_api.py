"""Tests for the Swiss Post API client and its anonymous session.

The session is the only stateful code in the integration, so the handshake, its
403 recovery and the "empty list is not a delivery" trap all get explicit
coverage here.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from multidict import CIMultiDict

from custom_components.swiss_post import api as api_module
from custom_components.swiss_post.api import (
    SwissPostApiClient,
    SwissPostApiError,
    SwissPostSession,
)

from .payloads import DELIVERED_CODE, EVENTS, delivered_sample, history_response

USER_ID = "<[anonymous]>b31beee6-0000-0000-0000-000000000000"
CSRF = "csrf-token-value"
DIGEST = "a" * 64


def _response(status: int, body: object = None, headers: dict | None = None):
    """Build a mock aiohttp response context manager."""
    response = AsyncMock()
    response.status = status
    response.headers = CIMultiDict(headers or {})
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(*, gets: list, posts: list) -> MagicMock:
    """A mock aiohttp session replaying the given GET/POST responses in order."""
    session = MagicMock()
    session.get = MagicMock(side_effect=list(gets))
    session.post = MagicMock(side_effect=list(posts))
    return session


def _user_response(user_id: str = USER_ID, csrf: str | None = CSRF):
    """The handshake response: user id in the body, CSRF on a header."""
    return _response(
        200,
        {"userIdentifier": user_id},
        # Upper-case, as Swiss Post sends it.
        {"X-CSRF-TOKEN": csrf} if csrf else {},
    )


def _lookup_session(shipments: list, *, digest: str = DIGEST) -> MagicMock:
    """A session that completes a handshake and one two-call lookup."""
    return _session(
        gets=[_user_response(), _response(200, shipments)],
        posts=[_response(200, {"hash": digest})],
    )


@pytest.fixture(autouse=True)
def _reset_one_shot_logs():
    """Clear the module's one-shot log sets between tests."""
    api_module._empty_result_logged.clear()
    api_module._history_204_logged.clear()
    yield


# ---------------------------------------------------------------------------
# the handshake
# ---------------------------------------------------------------------------


async def test_establish_keeps_user_id_and_token():
    session = _session(gets=[_user_response()], posts=[])
    tracking_session = SwissPostSession(session)

    assert tracking_session.established is False
    await tracking_session.async_establish()

    assert tracking_session.established is True
    assert session.get.call_args[0][0].endswith("/api/user")
    # A browser-shaped request: the endpoint is a consumer web API.
    headers = session.get.call_args.kwargs["headers"]
    assert "Mozilla" in headers["User-Agent"]
    assert headers["Referer"].startswith("https://service.post.ch/")


async def test_establish_reads_the_csrf_header_case_insensitively():
    """Swiss Post sends X-CSRF-TOKEN upper-case; a dict lookup would miss it."""
    session = _session(
        gets=[_response(200, {"userIdentifier": USER_ID}, {"x-csrf-token": CSRF})],
        posts=[],
    )
    tracking_session = SwissPostSession(session)
    await tracking_session.async_establish()
    assert tracking_session.established


@pytest.mark.parametrize(
    "response,expected",
    [
        (_response(500), "HTTP 500"),
        (_response(200, "not json"), "unparseable"),
        (_response(200, {"userIdentifier": None}, {"X-CSRF-TOKEN": CSRF}), "user identifier"),
        (_response(200, {"userIdentifier": USER_ID}), "CSRF"),
    ],
)
async def test_establish_rejects_a_broken_handshake(response, expected):
    tracking_session = SwissPostSession(_session(gets=[response], posts=[]))
    with pytest.raises(SwissPostApiError) as err:
        await tracking_session.async_establish()
    assert expected in str(err.value)


# ---------------------------------------------------------------------------
# the two-call lookup
# ---------------------------------------------------------------------------


async def test_lookup_registers_the_code_before_reading_it():
    """The POST is what populates the hash — never skip it (see api.py)."""
    session = _lookup_session([delivered_sample()])
    parcel = await SwissPostApiClient(session).async_get_parcel(DELIVERED_CODE)

    assert parcel["shipmentNumber"] == DELIVERED_CODE
    # One handshake GET, then the registration POST, then the read GET.
    assert session.post.call_args.kwargs["json"] == {"searchQuery": DELIVERED_CODE}
    assert session.post.call_args.kwargs["headers"]["X-CSRF-TOKEN"] == CSRF
    read_url = session.get.call_args_list[1][0][0]
    assert read_url.endswith(f"/history/not-included/{DIGEST}")
    # The user id goes through params so aiohttp encodes the "<[anonymous]>".
    assert session.get.call_args.kwargs["params"] == {"userId": USER_ID}
    # The read is authorised by the cookie alone.
    assert "X-CSRF-TOKEN" not in session.get.call_args.kwargs["headers"]


async def test_lookup_reuses_an_established_session():
    """One handshake serves every parcel — six lookups, one /api/user call."""
    session = _session(
        gets=[_user_response(), _response(200, [delivered_sample()]),
              _response(200, [delivered_sample()])],
        posts=[_response(200, {"hash": DIGEST}), _response(200, {"hash": DIGEST})],
    )
    client = SwissPostApiClient(session)

    await client.async_get_parcel(DELIVERED_CODE)
    await client.async_get_parcel(DELIVERED_CODE)

    user_calls = [c for c in session.get.call_args_list if c[0][0].endswith("/api/user")]
    assert len(user_calls) == 1


async def test_empty_result_is_not_found_not_an_error(caplog):
    """A warm session returning [] means Swiss Post does not know the code."""
    client = SwissPostApiClient(_lookup_session([]))

    assert await client.async_get_parcel(DELIVERED_CODE) is None
    assert "has no data for tracking code" in caplog.text


async def test_empty_result_warns_only_once(caplog):
    session = _session(
        gets=[_user_response(), _response(200, []), _response(200, [])],
        posts=[_response(200, {"hash": DIGEST}), _response(200, {"hash": DIGEST})],
    )
    client = SwissPostApiClient(session)

    await client.async_get_parcel(DELIVERED_CODE)
    await client.async_get_parcel(DELIVERED_CODE)

    assert caplog.text.count("has no data for tracking code") == 1


@pytest.mark.parametrize(
    "gets,posts,expected",
    [
        # registration failures
        ([_user_response()], [_response(500)], "registering"),
        ([_user_response()], [_response(200, "not json")], "unparseable registration"),
        ([_user_response()], [_response(200, {})], "no hash"),
        ([_user_response()], [_response(200, ["nope"])], "no hash"),
        # read failures
        (
            [_user_response(), _response(500)],
            [_response(200, {"hash": DIGEST})],
            "reading the shipment",
        ),
        (
            [_user_response(), _response(200, "not json")],
            [_response(200, {"hash": DIGEST})],
            "unparseable shipment",
        ),
        (
            [_user_response(), _response(200, {"not": "a list"})],
            [_response(200, {"hash": DIGEST})],
            "not a JSON list",
        ),
        (
            [_user_response(), _response(200, ["not a dict"])],
            [_response(200, {"hash": DIGEST})],
            "not a JSON object",
        ),
    ],
)
async def test_lookup_raises_on_unexpected_responses(gets, posts, expected):
    client = SwissPostApiClient(_session(gets=gets, posts=posts))
    with pytest.raises(SwissPostApiError) as err:
        await client.async_get_parcel(DELIVERED_CODE)
    assert expected in str(err.value)


async def test_network_error_propagates():
    """ClientError is left alone — DataUpdateCoordinator already wraps it."""
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await SwissPostApiClient(session).async_get_parcel(DELIVERED_CODE)


# ---------------------------------------------------------------------------
# 403 recovery — the session is the only stateful part of the integration
# ---------------------------------------------------------------------------


async def test_expired_session_is_re_established_and_the_lookup_retried():
    session = _session(
        gets=[
            _user_response(),
            # second handshake, after the 403
            _user_response(user_id="<[anonymous]>second", csrf="second-token"),
            _response(200, [delivered_sample()]),
        ],
        posts=[_response(403), _response(200, {"hash": DIGEST})],
    )
    client = SwissPostApiClient(session)

    parcel = await client.async_get_parcel(DELIVERED_CODE)

    assert parcel["shipmentNumber"] == DELIVERED_CODE
    user_calls = [c for c in session.get.call_args_list if c[0][0].endswith("/api/user")]
    assert len(user_calls) == 2
    # The retry uses the *new* token, not the dead one.
    assert session.post.call_args.kwargs["headers"]["X-CSRF-TOKEN"] == "second-token"


async def test_a_403_on_the_read_call_also_re_establishes():
    session = _session(
        gets=[
            _user_response(),
            _response(403),
            _user_response(),
            _response(200, [delivered_sample()]),
        ],
        posts=[_response(200, {"hash": DIGEST}), _response(200, {"hash": DIGEST})],
    )
    assert await SwissPostApiClient(session).async_get_parcel(DELIVERED_CODE)


async def test_a_second_403_gives_up_rather_than_looping():
    session = _session(
        gets=[_user_response(), _user_response()],
        posts=[_response(403), _response(403)],
    )
    with pytest.raises(SwissPostApiError) as err:
        await SwissPostApiClient(session).async_get_parcel(DELIVERED_CODE)
    assert "403" in str(err.value)


# ---------------------------------------------------------------------------
# surface B — the event timeline
# ---------------------------------------------------------------------------


def _history_client(history_ctx) -> tuple[SwissPostApiClient, MagicMock]:
    session = _session(
        gets=[_user_response(), _response(200, [delivered_sample()])],
        posts=[_response(200, {"hash": DIGEST}), history_ctx],
    )
    return SwissPostApiClient(session), session


async def test_history_is_only_fetched_when_asked_for():
    client, session = _history_client(_response(200, history_response()))

    parcel = await client.async_get_parcel(DELIVERED_CODE)

    assert parcel["events"] == []  # surface A's own, always-empty array
    assert len(session.post.call_args_list) == 1  # registration only


async def test_history_is_merged_into_the_shipment():
    client, session = _history_client(_response(200, history_response()))

    parcel = await client.async_get_parcel(DELIVERED_CODE, include_history=True)

    assert parcel["events"] == EVENTS
    # Singular, capitalised "Identifier" — the plural spelling is ignored with
    # a 204 rather than rejected.
    assert session.post.call_args.kwargs["json"] == {"Identifier": DELIVERED_CODE}
    assert session.post.call_args.kwargs["params"] == {"culture": "de-DE"}


async def test_history_204_is_a_malformed_request_not_an_unknown_parcel(caplog):
    client, _ = _history_client(_response(204))

    parcel = await client.async_get_parcel(DELIVERED_CODE, include_history=True)

    assert parcel["events"] == []
    assert "did not understand our request" in caplog.text
    assert "issues/new" in caplog.text


async def test_history_204_warns_only_once(caplog):
    session = _session(gets=[], posts=[_response(204), _response(204)])
    client = SwissPostApiClient(session)

    await client.async_get_history(DELIVERED_CODE)
    await client.async_get_history(DELIVERED_CODE)

    assert caplog.text.count("did not understand our request") == 1


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"Type": 3, "Data": []}, []),
        ({"Type": 3, "Data": None}, []),
        ({"Type": 3, "Data": [{"Identifier": DELIVERED_CODE}]}, []),
        ({"Type": 3, "Data": [{"History": None}]}, []),
    ],
)
async def test_history_tolerates_thin_envelopes(body, expected):
    client = SwissPostApiClient(_session(gets=[], posts=[_response(200, body)]))
    assert await client.async_get_history(DELIVERED_CODE) == expected


@pytest.mark.parametrize(
    "response,expected",
    [
        (_response(500), "HTTP 500"),
        (_response(200, "not json"), "unparseable history"),
        (_response(200, ["not", "a", "dict"]), "not a JSON object"),
        (_response(200, {"Data": ["not a dict"]}), "not a JSON object"),
    ],
)
async def test_history_raises_on_unexpected_responses(response, expected):
    client = SwissPostApiClient(_session(gets=[], posts=[response]))
    with pytest.raises(SwissPostApiError) as err:
        await client.async_get_history(DELIVERED_CODE)
    assert expected in str(err.value)


async def test_a_broken_history_never_fails_the_parcel(caplog):
    """The timeline is a nice-to-have; the status is not."""
    client, _ = _history_client(_response(500))

    parcel = await client.async_get_parcel(DELIVERED_CODE, include_history=True)

    assert parcel["shipmentNumber"] == DELIVERED_CODE
    assert parcel["events"] == []
    assert "event history unavailable" in caplog.text
