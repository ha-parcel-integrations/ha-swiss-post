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
    account_detail,
    account_element,
    detail_response,
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


def test_normalize_account_parcel_does_not_call_a_returned_parcel_delivered():
    """``isComplete`` means "finished", and a returned parcel is finished too."""
    parcel = normalize_account_parcel(account_element(status="RETURNED", complete=True))
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_normalize_account_parcel_leaves_an_unmapped_complete_parcel_undelivered():
    """Only the status enum separates delivered from returned, so it decides."""
    parcel = normalize_account_parcel(
        account_element(status="SOMETHING_NEW", complete=True)
    )
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False


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


def test_normalize_account_parcel_warns_once_about_an_end_timestamp(caplog):
    """The only ETA candidate in the model, and it has never been populated."""
    element = account_element()
    element["statusEndTimestamp"] = 1776328109000
    normalize_account_parcel(element)
    normalize_account_parcel(element)
    assert len([r for r in caplog.records if "end timestamp" in r.getMessage()]) == 1


def test_normalize_account_parcel_publishes_the_canonical_keys():
    """The account source must publish the same contract as the tracking one."""
    from custom_components.swiss_post.tracking.parcels import normalize_parcel

    from .payloads import delivered_sample

    expected = set(normalize_parcel(delivered_sample()))
    assert set(normalize_account_parcel(account_element())) == expected
    # The enrichment fills fields; it must never add or drop one.
    assert (
        set(normalize_account_parcel(account_element(), detail=account_detail()))
        == expected
    )


# ---------------------------------------------------------------------------
# parcels — the per-parcel enrichment record
# ---------------------------------------------------------------------------


def test_enrichment_parses_the_formatted_weight_and_dimensions():
    """The app backend states both as display strings, not numbers."""
    parcel = normalize_account_parcel(account_element(), detail=account_detail())
    assert parcel["weight"] == 1.14
    assert parcel["dimensions"] == {
        "length": 40.0,
        "width": 25.0,
        "height": 15.5,
        "text": "40 x 25 x 15 cm",
    }
    # The whole enrichment record stays available under raw.
    assert parcel["raw"]["detail"]["mailpieceStatus"]["status"] == "DELIVERED"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.14 kg", 1.14),
        # Localisation is unprobed, so a decimal comma is accepted up front.
        ("1,14 kg", 1.14),
        ("850 g", 0.85),
        ("2 KG", 2.0),
        (None, None),
        ("", None),
        (1.14, None),
    ],
)
def test_enrichment_weight_parsing(value, expected):
    parcel = normalize_account_parcel(
        account_element(), detail=account_detail(weight=value)
    )
    assert parcel["weight"] == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("40.0 x 25.0 x 15.5 cm", (40.0, 25.0, 15.5)),
        ("40,0 x 25,0 x 15,5 cm", (40.0, 25.0, 15.5)),
        ("400 x 250 x 155 mm", (40.0, 25.0, 15.5)),
        # The three numbers carry no axis meaning, exactly as on the public
        # surface, so they are sorted before they are labelled.
        ("15.5 x 40.0 x 25.0 cm", (40.0, 25.0, 15.5)),
        ("40 × 25 × 15 cm", (40.0, 25.0, 15.0)),
        (None, None),
        ("roughly shoebox-sized", None),
    ],
)
def test_enrichment_dimension_parsing(value, expected):
    parcel = normalize_account_parcel(
        account_element(), detail=account_detail(dimensions=value)
    )
    if expected is None:
        assert parcel["dimensions"] is None
    else:
        length, width, height = expected
        assert (
            parcel["dimensions"]["length"],
            parcel["dimensions"]["width"],
            parcel["dimensions"]["height"],
        ) == (length, width, height)


def test_enrichment_reports_an_unreadable_measurement_once_without_the_value(caplog):
    detail = account_detail(weight="three and a bit stone")
    assert normalize_account_parcel(account_element(), detail=detail)["weight"] is None
    assert normalize_account_parcel(account_element(), detail=detail)["weight"] is None
    reports = [r for r in caplog.records if "cannot read yet" in r.getMessage()]
    assert len(reports) == 1
    assert "stone" not in reports[0].getMessage()


def test_enrichment_builds_the_history_oldest_first():
    parcel = normalize_account_parcel(account_element(), detail=account_detail())
    assert [entry["raw_status"] for entry in parcel["history"]] == [
        "Loading into delivery vehicle",
        "Delivered",
    ]
    # eventType was OTHER on every event ever seen, so it is not a status
    # vocabulary — the same call the public timeline makes.
    assert all(entry["status"] is None for entry in parcel["history"])
    # Coordinates say where a parcel physically was; they stay out of an
    # attribute and remain in raw only.
    assert all("location" not in entry for entry in parcel["history"])


def test_enrichment_history_skips_undatable_and_malformed_events():
    detail = account_detail(
        events=[
            {"title": "No timestamp at all"},
            "not even a dict",
            {"timestamp": 1776328109000, "subtitle": "Only a subtitle"},
        ]
    )
    parcel = normalize_account_parcel(account_element(), detail=detail)
    assert [entry["raw_status"] for entry in parcel["history"]] == ["Only a subtitle"]


def test_enrichment_history_is_capped():
    from custom_components.swiss_post.const import HISTORY_MAX_EVENTS

    events = [
        {"timestamp": 1776328109000 + index * 1000, "title": f"Event {index}"}
        for index in range(HISTORY_MAX_EVENTS + 5)
    ]
    parcel = normalize_account_parcel(
        account_element(), detail=account_detail(events=events)
    )
    assert len(parcel["history"]) == HISTORY_MAX_EVENTS
    # The cap keeps the most recent events, not the first ones.
    assert parcel["history"][-1]["raw_status"] == f"Event {HISTORY_MAX_EVENTS + 4}"


def test_enrichment_reports_an_unexpected_event_type_once(caplog):
    detail = account_detail(
        events=[
            {"timestamp": 1776328109000, "title": "Delivered", "eventType": "DELIVERY"}
        ]
    )
    normalize_account_parcel(account_element(), detail=detail)
    normalize_account_parcel(account_element(), detail=detail)
    assert len([r for r in caplog.records if "eventType" in r.getMessage()]) == 1


def test_enrichment_tolerates_a_record_without_physical_dimensions():
    detail = account_detail()
    del detail["physicalDimensions"]
    parcel = normalize_account_parcel(account_element(), detail=detail)
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] != []


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


async def test_get_detail_is_an_anonymous_lookup_that_never_follows():
    """No bearer: an enrichment failure can never cost the entry its tokens."""
    session = _session(gets=[_response(200, detail_response())])
    client = SwissPostAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", device_id="dev-1"
    )
    detail = await client.async_get_detail(ACCOUNT_IN_ID)
    assert detail["physicalDimensions"]["weight"] == "1.14 kg"
    kwargs = session.get.call_args.kwargs
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["headers"]["x-device-id"] == "dev-1"
    assert kwargs["params"]["mailpieceId"] == ACCOUNT_IN_ID
    # Without this the parcel would be attached to the device on every poll.
    assert kwargs["params"]["dontFollow"] == "true"


async def test_get_detail_returns_none_when_there_is_nothing_to_add():
    """A parcel the public surface does not know yet is not an error."""
    client = SwissPostAccountClient(_session(gets=[_response(404)]), device_id="dev-1")
    assert await client.async_get_detail(ACCOUNT_IN_ID) is None

    client = SwissPostAccountClient(
        _session(gets=[_response(200, {"detail": None, "singleResultNoInfo": True})]),
        device_id="dev-1",
    )
    assert await client.async_get_detail(ACCOUNT_IN_ID) is None


async def test_get_detail_without_a_code_makes_no_request():
    session = _session()
    client = SwissPostAccountClient(session, device_id="dev-1")
    assert await client.async_get_detail("") is None
    session.get.assert_not_called()


@pytest.mark.parametrize("body", [_response(503), _response(200, "not json"), _response(200, [])])
async def test_get_detail_surfaces_a_broken_response(body):
    client = SwissPostAccountClient(_session(gets=[body]), device_id="dev-1")
    with pytest.raises(SwissPostAccountApiError):
        await client.async_get_detail(ACCOUNT_IN_ID)


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


def _account_entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user-1",
        data={CONF_SOURCE: SOURCE_ACCOUNT},
        # Keep-most-recent-100 so the retention filter never trims the fixed
        # sample dates these tests assert on.
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            **options,
        },
    )


async def _refresh(
    hass, payload: dict, *, detail=None, **options
) -> tuple[SwissPostAccountCoordinator, dict]:
    entry = _account_entry(**options)
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_get_overview = AsyncMock(return_value=payload)
    client.async_get_detail = (
        detail if isinstance(detail, AsyncMock) else AsyncMock(return_value=detail)
    )
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


async def test_coordinator_skips_the_enrichment_by_default(hass):
    """It costs a request per parcel, so it stays off until asked for."""
    coordinator, data = await _refresh(
        hass, overview_response(in_progress=[account_element(ACCOUNT_IN_ID)])
    )
    coordinator._client.async_get_detail.assert_not_called()
    parcel = data["incoming_active"][0]
    assert (parcel["weight"], parcel["dimensions"], parcel["history"]) == (
        None,
        None,
        None,
    )


async def test_coordinator_enriches_every_parcel_when_asked(hass):
    from custom_components.swiss_post.const import CONF_ACCOUNT_DETAILS

    coordinator, data = await _refresh(
        hass,
        overview_response(
            in_progress=[
                account_element(ACCOUNT_IN_ID),
                account_element(ACCOUNT_OUT_ID, outgoing=True),
            ]
        ),
        detail=account_detail(),
        **{CONF_ACCOUNT_DETAILS: True},
    )
    assert coordinator._client.async_get_detail.await_count == 2
    assert [call.args[0] for call in coordinator._client.async_get_detail.await_args_list] == [
        ACCOUNT_IN_ID,
        ACCOUNT_OUT_ID,
    ]
    assert data["incoming_active"][0]["weight"] == 1.14
    assert data["outgoing_active"][0]["dimensions"]["length"] == 40.0


async def test_coordinator_keeps_a_parcel_whose_enrichment_fails(hass):
    """Losing a weight must not cost the user every parcel in the inbox."""
    from custom_components.swiss_post.const import CONF_ACCOUNT_DETAILS

    _, data = await _refresh(
        hass,
        overview_response(
            in_progress=[
                account_element(ACCOUNT_IN_ID),
                account_element(ACCOUNT_OUT_ID, outgoing=True),
            ]
        ),
        detail=AsyncMock(
            side_effect=[SwissPostAccountApiError("boom"), account_detail()]
        ),
        **{CONF_ACCOUNT_DETAILS: True},
    )
    assert data["incoming_active"][0]["weight"] is None
    assert data["incoming_active"][0]["status"] is ParcelStatus.IN_TRANSIT
    assert data["outgoing_active"][0]["weight"] == 1.14


async def test_coordinator_reraises_an_unexpected_enrichment_error(hass):
    """A programming error must fail the poll, not pass as a missing weight."""
    from custom_components.swiss_post.const import CONF_ACCOUNT_DETAILS

    coordinator, _ = await _refresh(
        hass,
        overview_response(in_progress=[account_element(ACCOUNT_IN_ID)]),
        detail=AsyncMock(side_effect=RuntimeError("bug")),
        **{CONF_ACCOUNT_DETAILS: True},
    )
    assert coordinator.last_update_success is False


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
