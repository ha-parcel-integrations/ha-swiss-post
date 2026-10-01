"""Tests for the SwissID account-inbox source.

Covers the three pieces the account model adds: the OAuth/PKCE client (token
rotation and the id_token-as-bearer trap), the overview normalisation, and the
coordinator's four-way incoming/outgoing split.
"""
import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from multidict import CIMultiDict
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_post.account.client import (
    SwissPostAccountApiError,
    SwissPostAccountClient,
    SwissPostAccountInvalidAuth,
    SwissPostAccountReauthRequired,
    decode_jwt_claims,
)
from custom_components.swiss_post.account.coordinator import (
    SwissPostAccountCoordinator,
)
from custom_components.swiss_post.account.parcels import (
    is_letter,
    is_outgoing,
    map_account_status,
    normalize_account_parcel,
)
from custom_components.swiss_post.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_SOURCE,
    DOMAIN,
    OIDC_CLIENT_ID,
    SOURCE_ACCOUNT,
    ParcelStatus,
)

from .payloads import (
    ACCOUNT_IN_ID,
    ACCOUNT_LETTER_ID,
    ACCOUNT_OUT_ID,
    account_element,
    overview_response,
)


def _jwt(claims: dict) -> str:
    """Build an unsigned JWT carrying ``claims`` (the client never verifies)."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return f"header.{payload.decode()}.signature"


def _fresh_id_token() -> str:
    return _jwt({"sub": "user-1", "exp": int(time.time()) + 3600})


def _response(status: int, body: object = None):
    """Build a mock aiohttp response context manager."""
    response = AsyncMock()
    response.status = status
    response.headers = CIMultiDict()
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(*, gets: list | None = None, posts: list | None = None) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(side_effect=list(gets or []))
    session.post = MagicMock(side_effect=list(posts or []))
    return session


def _token_body(*, id_token: str | None = None, refresh: str = "rt-new") -> dict:
    return {
        "id_token": id_token or _fresh_id_token(),
        "refresh_token": refresh,
        "access_token": "opaque-at",
    }


# ---------------------------------------------------------------------------
# parcels — status map and normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("NOT_YET_SENT", ParcelStatus.REGISTERED),
        ("ON_GOING_DELIVERY", ParcelStatus.IN_TRANSIT),
        ("WAITING_FOR_PICKUP", ParcelStatus.AT_PICKUP_POINT),
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("NOT_DELIVERED", ParcelStatus.PROBLEM),
        ("RETURNED", ParcelStatus.RETURNING),
        ("UNKNOWN", ParcelStatus.UNKNOWN),
    ],
)
def test_map_account_status_covers_the_app_enum(code, expected):
    """Every MailpieceStatusType value from the APK model is mapped."""
    assert map_account_status(code) is expected


def test_map_account_status_warns_once_on_an_unmapped_value(caplog):
    assert map_account_status("SOMETHING_NEW") is ParcelStatus.UNKNOWN
    assert map_account_status("SOMETHING_NEW") is ParcelStatus.UNKNOWN
    assert len([r for r in caplog.records if "SOMETHING_NEW" in r.getMessage()]) == 1


def test_map_account_status_is_silent_on_a_missing_value(caplog):
    assert map_account_status(None) is ParcelStatus.UNKNOWN
    assert not caplog.records


def test_is_letter_only_matches_an_explicit_letter():
    assert is_letter(account_element(mailpiece_type="LETTER"))
    assert not is_letter(account_element(mailpiece_type="PARCEL"))
    # An unexpected type is kept, so a parcel is never silently dropped.
    assert not is_letter(account_element(mailpiece_type="UNKNOWN"))
    assert not is_letter({})


def test_is_outgoing_reads_the_sender_flag():
    assert is_outgoing(account_element(outgoing=True))
    assert not is_outgoing(account_element(outgoing=False))
    assert not is_outgoing({})


def test_normalize_account_parcel_maps_an_in_transit_parcel():
    parcel = normalize_account_parcel(account_element())
    assert parcel["carrier"] == "Swiss Post"
    assert parcel["barcode"] == ACCOUNT_IN_ID
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "ON_GOING_DELIVERY"
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    # The town is all the surface gives — the name* fields are blank.
    assert parcel["receiver"] == "3011 Bern"
    assert parcel["url"].endswith(ACCOUNT_IN_ID)
    # The summary carries none of these; a detail call would.
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_account_parcel_marks_a_completed_parcel_delivered():
    parcel = normalize_account_parcel(
        account_element(status="DELIVERED", complete=True)
    )
    assert parcel["delivered"] is True
    # statusTimestamp is epoch milliseconds (10:28 local = 08:28 UTC).
    assert parcel["delivered_at"] == "2026-04-16T08:28:29+00:00"
    assert parcel["pickup"] is False


def test_normalize_account_parcel_uses_is_complete_over_the_status():
    """``isComplete`` is explicit, so it survives an unmapped status token."""
    parcel = normalize_account_parcel(
        account_element(status="SOMETHING_NEW", complete=True)
    )
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["delivered"] is True


def test_normalize_account_parcel_exposes_the_pickup_office():
    """The account surface names the pickup point the tracking surface lacks."""
    parcel = normalize_account_parcel(
        account_element(status="WAITING_FOR_PICKUP", pickup_office="Bern 1")
    )
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Bern 1"


def test_normalize_account_parcel_survives_a_missing_address():
    parcel = normalize_account_parcel({"mailpieceId": "X", "mailpieceStatusType": "DELIVERED"})
    assert parcel["receiver"] is None
    assert parcel["pickup_point"] is None


def test_normalize_account_parcel_publishes_the_canonical_keys():
    """The account source must publish the same contract as the tracking one."""
    from custom_components.swiss_post.tracking.parcels import normalize_parcel

    from .payloads import delivered_sample

    assert set(normalize_account_parcel(account_element())) == set(
        normalize_parcel(delivered_sample())
    )


# ---------------------------------------------------------------------------
# client — PKCE, token rotation, the id_token bearer
# ---------------------------------------------------------------------------


def test_build_authorize_url_carries_pkce_and_a_fresh_state():
    url, verifier, state = SwissPostAccountClient.build_authorize_url()
    assert url.startswith("https://login.swissid.ch/idp/oauth2/authorize?")
    assert f"client_id={OIDC_CLIENT_ID}" in url
    assert "code_challenge_method=S256" in url
    assert "code_challenge=" in url
    assert f"state={state}" in url
    assert verifier and verifier not in url  # the verifier is never sent here
    # Each call is a distinct login attempt.
    assert SwissPostAccountClient.build_authorize_url()[2] != state


def test_decode_jwt_claims_reads_claims_and_tolerates_rubbish():
    assert decode_jwt_claims(_jwt({"sub": "abc"}))["sub"] == "abc"
    assert decode_jwt_claims("not-a-jwt") == {}
    assert decode_jwt_claims(None) == {}
    assert decode_jwt_claims("a.!!!.c") == {}


async def test_exchange_code_returns_the_token_triple():
    session = _session(posts=[_response(200, _token_body(refresh="rt-1"))])
    client = SwissPostAccountClient(session)
    tokens = await client.async_exchange_code("CODE", "VERIFIER")
    assert tokens["refresh_token"] == "rt-1"
    sent = session.post.call_args.kwargs["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code_verifier"] == "VERIFIER"


async def test_exchange_code_rejects_a_bad_code():
    client = SwissPostAccountClient(_session(posts=[_response(400)]))
    with pytest.raises(SwissPostAccountInvalidAuth):
        await client.async_exchange_code("CODE", "VERIFIER")


async def test_token_response_without_tokens_is_an_error():
    client = SwissPostAccountClient(_session(posts=[_response(200, {"access_token": "x"})]))
    with pytest.raises(SwissPostAccountApiError):
        await client.async_exchange_code("CODE", "VERIFIER")


async def test_token_endpoint_server_error_is_an_api_error():
    client = SwissPostAccountClient(_session(posts=[_response(500)]))
    with pytest.raises(SwissPostAccountApiError):
        await client.async_exchange_code("CODE", "VERIFIER")


async def test_refresh_rotates_the_pair_and_persists_it():
    """The refresh token rotates every time, so the new one must be stored."""
    stored: list[dict] = []
    client = SwissPostAccountClient(
        _session(posts=[_response(200, _token_body(refresh="rt-2"))]),
        refresh_token="rt-1",
        token_callback=AsyncMock(side_effect=lambda t: stored.append(t)),
    )
    tokens = await client.async_refresh()
    assert tokens["refresh_token"] == "rt-2"
    assert stored and stored[0]["refresh_token"] == "rt-2"


async def test_refresh_without_a_stored_token_needs_reauth():
    client = SwissPostAccountClient(_session())
    with pytest.raises(SwissPostAccountReauthRequired):
        await client.async_refresh()


async def test_a_rejected_refresh_chain_needs_reauth():
    client = SwissPostAccountClient(
        _session(posts=[_response(400)]), refresh_token="dead"
    )
    with pytest.raises(SwissPostAccountReauthRequired):
        await client.async_refresh()


async def test_overview_sends_the_id_token_as_bearer():
    """The inbox validates the JWT id_token; the opaque access_token is refused."""
    id_token = _fresh_id_token()
    session = _session(gets=[_response(200, overview_response())])
    client = SwissPostAccountClient(
        session, id_token=id_token, refresh_token="rt", device_id="dev-1"
    )
    await client.async_get_overview()
    headers = session.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {id_token}"
    assert headers["x-device-id"] == "dev-1"


async def test_overview_refreshes_a_stale_id_token_first():
    expired = _jwt({"sub": "user-1", "exp": int(time.time()) - 10})
    session = _session(
        gets=[_response(200, overview_response())],
        posts=[_response(200, _token_body())],
    )
    client = SwissPostAccountClient(session, id_token=expired, refresh_token="rt-1")
    await client.async_get_overview()
    assert session.post.called  # the refresh happened before the read


async def test_overview_retries_once_after_a_surprise_401():
    """A token can be rejected between the freshness check and the call."""
    session = _session(
        gets=[_response(401), _response(200, overview_response())],
        posts=[_response(200, _token_body())],
    )
    client = SwissPostAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt-1"
    )
    assert await client.async_get_overview() == overview_response()
    assert session.get.call_count == 2


async def test_overview_gives_up_after_a_second_rejection():
    session = _session(
        gets=[_response(401), _response(401)],
        posts=[_response(200, _token_body())],
    )
    client = SwissPostAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt-1"
    )
    with pytest.raises(SwissPostAccountReauthRequired):
        await client.async_get_overview()


async def test_overview_server_error_is_an_api_error():
    client = SwissPostAccountClient(
        _session(gets=[_response(503)]),
        id_token=_fresh_id_token(),
        refresh_token="rt",
    )
    with pytest.raises(SwissPostAccountApiError):
        await client.async_get_overview()


async def test_overview_non_object_payload_is_an_api_error():
    client = SwissPostAccountClient(
        _session(gets=[_response(200, [])]),
        id_token=_fresh_id_token(),
        refresh_token="rt",
    )
    with pytest.raises(SwissPostAccountApiError):
        await client.async_get_overview()


async def test_overview_invalid_json_is_an_api_error():
    client = SwissPostAccountClient(
        _session(gets=[_response(200, "not json")]),
        id_token=_fresh_id_token(),
        refresh_token="rt",
    )
    with pytest.raises(SwissPostAccountApiError):
        await client.async_get_overview()


# ---------------------------------------------------------------------------
# coordinator — the four-way split
# ---------------------------------------------------------------------------


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user-1",
        data={CONF_SOURCE: SOURCE_ACCOUNT},
        # Keep-most-recent-100 so the retention filter never trims the fixed
        # sample dates these tests assert on.
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )


async def _refresh(hass, payload: dict) -> tuple[SwissPostAccountCoordinator, dict]:
    entry = _account_entry()
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_get_overview = AsyncMock(return_value=payload)
    coordinator = SwissPostAccountCoordinator(hass, client, entry)
    await coordinator.async_refresh()
    return coordinator, coordinator.data


async def test_coordinator_splits_incoming_outgoing_and_delivered(hass):
    coordinator, data = await _refresh(
        hass,
        overview_response(
            in_progress=[
                account_element(ACCOUNT_IN_ID),
                account_element(ACCOUNT_OUT_ID, outgoing=True),
            ],
            completed=[
                account_element("111", status="DELIVERED", complete=True),
                account_element(
                    "222", status="DELIVERED", complete=True, outgoing=True
                ),
            ],
        ),
    )
    assert [p["barcode"] for p in data["incoming_active"]] == [ACCOUNT_IN_ID]
    assert [p["barcode"] for p in data["outgoing_active"]] == [ACCOUNT_OUT_ID]
    assert [p["barcode"] for p in data["incoming_delivered"]] == ["111"]
    assert [p["barcode"] for p in data["outgoing_delivered"]] == ["222"]
    # Account polls are batched, so nothing is ever skipped from a fetch.
    assert coordinator.delivered_codes == set()


async def test_coordinator_excludes_letters(hass):
    """Letters are out of scope — Swiss Post has no scan-preview feature."""
    _, data = await _refresh(
        hass,
        overview_response(
            in_progress=[
                account_element(ACCOUNT_IN_ID),
                account_element(ACCOUNT_LETTER_ID, mailpiece_type="LETTER"),
            ]
        ),
    )
    assert [p["barcode"] for p in data["incoming_active"]] == [ACCOUNT_IN_ID]


async def test_coordinator_handles_an_empty_inbox(hass):
    _, data = await _refresh(hass, overview_response())
    assert data == {
        "incoming_active": [],
        "incoming_delivered": [],
        "outgoing_active": [],
        "outgoing_delivered": [],
    }


async def test_coordinator_tolerates_missing_keys_and_junk_entries(hass):
    _, data = await _refresh(hass, {"inProgress": [account_element(), "junk", None]})
    assert len(data["incoming_active"]) == 1


async def test_coordinator_fires_no_events_on_the_first_refresh(hass):
    """A restart must not replay history as fresh notifications."""
    events: list = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    await _refresh(hass, overview_response(in_progress=[account_element()]))
    await hass.async_block_till_done()
    assert events == []


async def test_coordinator_fires_events_on_a_later_change(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_get_overview = AsyncMock(
        return_value=overview_response(in_progress=[account_element()])
    )
    coordinator = SwissPostAccountCoordinator(hass, client, entry)
    await coordinator.async_refresh()

    delivered: list = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", delivered.append)
    client.async_get_overview.return_value = overview_response(
        completed=[account_element(status="DELIVERED", complete=True)]
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [e.data["barcode"] for e in delivered] == [ACCOUNT_IN_ID]


async def test_coordinator_fires_outgoing_events_on_a_later_change(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_get_overview = AsyncMock(
        return_value=overview_response(
            in_progress=[account_element(ACCOUNT_OUT_ID, outgoing=True)]
        )
    )
    coordinator = SwissPostAccountCoordinator(hass, client, entry)
    await coordinator.async_refresh()

    fired: list = []
    hass.bus.async_listen(f"{DOMAIN}_outgoing_parcel_delivered", fired.append)
    client.async_get_overview.return_value = overview_response(
        completed=[
            account_element(
                ACCOUNT_OUT_ID, status="DELIVERED", complete=True, outgoing=True
            )
        ]
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [e.data["barcode"] for e in fired] == [ACCOUNT_OUT_ID]


async def test_coordinator_surfaces_reauth(hass):
    """A dead refresh chain must reach HA as a reauth, not a failed update.

    ConfigEntryAuthFailed is the only exception DataUpdateCoordinator turns into
    the reauth flow; anything else would retry a dead chain forever.
    """
    from homeassistant.exceptions import ConfigEntryAuthFailed

    entry = _account_entry()
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_get_overview = AsyncMock(
        side_effect=SwissPostAccountReauthRequired("dead")
    )
    coordinator = SwissPostAccountCoordinator(hass, client, entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_wraps_an_api_error_as_update_failed(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _account_entry()
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_get_overview = AsyncMock(side_effect=SwissPostAccountApiError("boom"))
    coordinator = SwissPostAccountCoordinator(hass, client, entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_account_coordinator_polls_at_the_steady_cadence(hass):
    from custom_components.swiss_post.const import MID_INTERVAL_MINUTES

    coordinator, _ = await _refresh(hass, overview_response())
    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES
    assert coordinator.last_success_time is not None
