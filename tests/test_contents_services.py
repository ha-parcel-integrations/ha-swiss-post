"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.swiss_post.account.coordinator import SwissPostAccountCoordinator
from custom_components.swiss_post.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DEVICE_ID,
    CONF_ID_TOKEN,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_ACCOUNT,
)
from custom_components.swiss_post.services import async_setup_services
from custom_components.swiss_post.tracking import coordinator as module

from .payloads import (
    ACCOUNT_IN_ID as ACCOUNT_ACTIVE_CODE,
    ACCOUNT_OUT_ID,
    ACTIVE_CODE as HUB_CODE,
    DELIVERED_CODE,
    account_element,
    active_sample,
    delivered_sample,
    overview_response,
)

HUB_GET = (
    "custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel"
)
ACCOUNT_GET = (
    "custom_components.swiss_post.account.client.SwissPostAccountClient"
    ".async_get_overview"
)
ACCOUNT_DELIVERED_CODE = "990011112222333399"
ACCOUNT_OUT_DELIVERED = "990055556666777799"
UNTRACKED = "EXAMPLE999999"
STORE_KEY = f"{DOMAIN}.contents.%s"


def _hub_payload(code, **_):
    return active_sample(code)


def _overview():
    return overview_response(
        in_progress=[account_element(ACCOUNT_ACTIVE_CODE)],
        completed=[
            account_element(ACCOUNT_DELIVERED_CODE, status="DELIVERED", complete=True)
        ],
    )


async def _setup(hass, codes=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes or []]},
    )
    entry.add_to_hass(hass)
    with patch(HUB_GET, new=AsyncMock(side_effect=_hub_payload)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_account(hass, email="someone@example.com") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"account:{email}",
        title=email,
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ID_TOKEN: "idt",
            CONF_REFRESH_TOKEN: "rt",
            CONF_DEVICE_ID: "dev-1",
            "email": email,
        },
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 5,
        },
    )
    entry.add_to_hass(hass)
    with patch(ACCOUNT_GET, new=AsyncMock(return_value=_overview())):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@contextmanager
def _polls():
    with (
        patch(HUB_GET, new=AsyncMock(side_effect=_hub_payload)),
        patch(ACCOUNT_GET, new=AsyncMock(return_value=_overview())),
    ):
        yield


async def _call(hass, service: str, data: dict) -> None:
    with _polls():
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


def _contents(entry, code):
    data = entry.runtime_data.coordinator.data
    if isinstance(data, dict):
        data = [p for bucket in data.values() for p in bucket]
    return {p["barcode"]: p for p in data}[code]["contents"]


def _store(entry):
    return entry.runtime_data.coordinator.contents_store


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass, [HUB_CODE])
    assert _contents(entry, HUB_CODE) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass, [HUB_CODE])

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": " Running  shoes "},
    )
    assert _contents(entry, HUB_CODE) == "Running shoes"

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Winter socks"},
    )
    assert _contents(entry, HUB_CODE) == "Winter socks"

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: HUB_CODE, "contents": "  "}
    )
    assert _contents(entry, HUB_CODE) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "update_contents", {CONF_TRACKING_CODE: HUB_CODE})

    assert _contents(entry, HUB_CODE) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass, ["EXAMPLE111111"])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: " example111111 ", "contents": "Running shoes"},
    )
    assert _store(entry).get("EXAMPLE111111") == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert _store(entry).get(UNTRACKED) == "Running shoes"
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: HUB_CODE}]


async def test_update_contents_updates_without_polling_only_on_a_change(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"}
    with (
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh,
        patch(HUB_GET, new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        assert _contents(entry, HUB_CODE) == "Running shoes"
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: UNTRACKED}, blocking=True
        )
        assert listeners.call_count == 1
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: HUB_CODE}, blocking=True
        )
        assert listeners.call_count == 2
        assert _contents(entry, HUB_CODE) is None
    refresh.assert_not_called()
    fetch.assert_not_called()


async def test_an_unchanged_value_writes_nothing(hass):
    entry = await _setup(hass, [HUB_CODE])
    store = _store(entry)
    with patch.object(store._store, "async_delay_save") as save:
        assert store.async_set(HUB_CODE, "Running shoes") is True
        assert save.call_count == 1
        assert store.async_set(HUB_CODE, " Running shoes ") is False
        assert store.async_set(UNTRACKED, None) is False
        assert save.call_count == 1


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: " "})


async def test_update_contents_needs_a_loaded_entry(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: HUB_CODE}, blocking=True
        )


async def test_update_contents_ignores_an_entry_that_is_not_loaded(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
        )


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: HUB_CODE}]
    assert _contents(entry, HUB_CODE) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: HUB_CODE})

    assert _store(entry).get(HUB_CODE) is None
    assert _contents(entry, HUB_CODE) is None


async def test_track_parcel_replaces_and_clears_contents_of_a_tracked_code(hass):
    entry = await _setup(hass, [HUB_CODE])
    track = {CONF_TRACKING_CODE: HUB_CODE}

    await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
    assert _contents(entry, HUB_CODE) == "Running shoes"

    await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
    assert _contents(entry, HUB_CODE) == "Winter socks"

    await _call(hass, "track_parcel", {**track, "contents": ""})
    assert _contents(entry, HUB_CODE) is None
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: HUB_CODE}]


async def test_track_parcel_contents_change_of_a_tracked_code_does_not_poll(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"}
    with (
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh,
        patch(HUB_GET, new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "track_parcel", data, blocking=True)
        await hass.services.async_call(DOMAIN, "track_parcel", data, blocking=True)
        assert listeners.call_count == 1
        assert _contents(entry, HUB_CODE) == "Running shoes"
    refresh.assert_not_called()
    fetch.assert_not_called()


async def test_track_parcel_with_contents_needs_a_loaded_hub(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
        )


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: HUB_CODE})

    assert _store(entry).get(HUB_CODE) is None
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_parcel_skips_a_hub_that_is_not_loaded(hass):
    entry = await _setup(hass, [HUB_CODE])
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: HUB_CODE})

    assert entry.options[CONF_PARCELS] == []


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )
    await _store(entry)._store.async_save(_store(entry)._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry, HUB_CODE) == "Running shoes"


async def test_an_entry_without_a_store_file_sets_up_and_loads_empty(
    hass, hass_storage
):
    assert not [key for key in hass_storage if key.startswith(f"{DOMAIN}.contents.")]
    entry = await _setup(hass, [HUB_CODE])
    assert entry.state is ConfigEntryState.LOADED
    assert _contents(entry, HUB_CODE) is None


@pytest.mark.parametrize("kind", ["hub", "account"])
async def test_removing_the_entry_removes_its_store(hass, hass_storage, kind):
    if kind == "hub":
        entry, code = await _setup(hass, [HUB_CODE]), HUB_CODE
    else:
        entry, code = await _setup_account(hass), ACCOUNT_ACTIVE_CODE
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: code, "contents": "Running shoes"},
    )
    store = _store(entry)
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass, [HUB_CODE])
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", events.append)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    assert events == []


# ---------------------------------------------------------------------------
# overlay on the coordinators
# ---------------------------------------------------------------------------


async def test_user_contents_is_keyed_on_the_requested_code(hass):
    """A payload carrying another number still finds the value stored for the code."""
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    coordinator.contents_store.async_set(HUB_CODE, "Running shoes")

    with patch(HUB_GET, new=AsyncMock(return_value=active_sample(DELIVERED_CODE))):
        data = await coordinator._async_update_data()

    assert data[0]["barcode"] == DELIVERED_CODE
    assert data[0]["contents"] == "Running shoes"


async def test_user_contents_overlays_the_carrier_value(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    real = module.normalize_parcel

    def carrier_text(raw, **kwargs):
        parcel = real(raw, **kwargs)
        parcel["contents"] = "Carrier text"
        return parcel

    with (
        patch.object(module, "normalize_parcel", side_effect=carrier_text),
        patch(HUB_GET, new=AsyncMock(return_value=active_sample(HUB_CODE))),
    ):
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )
        coordinator.contents_store.async_set(HUB_CODE, "Running shoes")
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Running shoes"
        )
        coordinator.contents_store.async_set(HUB_CODE, None)
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )


async def test_account_overlay_covers_active_and_delivered(hass):
    account = await _setup_account(hass)
    coordinator = account.runtime_data.coordinator
    assert isinstance(coordinator, SwissPostAccountCoordinator)
    coordinator.contents_store.async_set(ACCOUNT_ACTIVE_CODE, "Running shoes")
    coordinator.contents_store.async_set(ACCOUNT_DELIVERED_CODE, "Winter socks")

    with patch(ACCOUNT_GET, new=AsyncMock(return_value=_overview())):
        data = await coordinator._async_update_data()

    assert data["incoming_active"][0]["contents"] == "Running shoes"
    assert data["incoming_delivered"][0]["contents"] == "Winter socks"


async def test_known_codes_list_every_code_the_entry_holds(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)

    assert hub.runtime_data.coordinator.known_codes == {HUB_CODE}
    assert account.runtime_data.coordinator.known_codes == {
        ACCOUNT_ACTIVE_CODE,
        ACCOUNT_DELIVERED_CODE,
    }


async def test_account_entries_take_contents_too(hass):
    account = await _setup_account(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACCOUNT_ACTIVE_CODE, "contents": "Running shoes"},
    )

    assert _contents(account, ACCOUNT_ACTIVE_CODE) == "Running shoes"


# ---------------------------------------------------------------------------
# several entries
# ---------------------------------------------------------------------------


async def test_update_contents_goes_to_the_entry_that_lists_the_code(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )

    assert _store(hub).get(HUB_CODE) == "Running shoes"
    assert _store(account).get(HUB_CODE) is None


async def test_update_contents_goes_to_every_entry_that_lists_the_code(hass):
    home = await _setup_account(hass, "home@example.com")
    work = await _setup_account(hass, "work@example.com")

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACCOUNT_ACTIVE_CODE, "contents": "Running shoes"},
    )

    assert _store(home).get(ACCOUNT_ACTIVE_CODE) == "Running shoes"
    assert _store(work).get(ACCOUNT_ACTIVE_CODE) == "Running shoes"


async def test_update_contents_with_several_entries_and_no_match_needs_an_entry(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)

    with pytest.raises(ServiceValidationError) as error:
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
        )

    assert error.value.translation_key == "update_contents_entry_required"
    assert _store(hub).get(UNTRACKED) is None
    assert _store(account).get(UNTRACKED) is None


async def test_config_entry_id_restricts_and_stores_an_unknown_code(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)

    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: UNTRACKED,
            "contents": "Running shoes",
            "config_entry_id": account.entry_id,
        },
    )
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: HUB_CODE,
            "contents": "Winter socks",
            "config_entry_id": hub.entry_id,
        },
    )

    assert _store(account).get(UNTRACKED) == "Running shoes"
    assert _store(hub).get(UNTRACKED) is None
    assert _store(hub).get(HUB_CODE) == "Winter socks"
    assert _store(account).get(HUB_CODE) is None


async def test_config_entry_id_must_name_a_loaded_entry(hass):
    await _setup(hass, [HUB_CODE])
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "config_entry_id": "missing"},
        )


# ---------------------------------------------------------------------------
# service registration
# ---------------------------------------------------------------------------


async def test_an_account_alone_registers_only_update_contents(hass):
    account = await _setup_account(hass)

    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")

    assert await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_unloading_one_entry_keeps_the_services_the_others_need(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(hub.entry_id)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_a_hub_after_an_account_still_registers_the_hub_services(hass):
    await _setup_account(hass)
    await _setup(hass, [HUB_CODE])

    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "untrack_parcel")


# ---------------------------------------------------------------------------
# re-applying contents without a poll
# ---------------------------------------------------------------------------


async def _setup_account_with_outgoing(hass):
    overview = overview_response(
        in_progress=[
            account_element(ACCOUNT_ACTIVE_CODE),
            account_element(ACCOUNT_OUT_ID, outgoing=True),
        ],
        completed=[
            account_element(ACCOUNT_DELIVERED_CODE, status="DELIVERED", complete=True),
            account_element(
                ACCOUNT_OUT_DELIVERED, status="DELIVERED", complete=True, outgoing=True
            ),
        ],
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:someone@example.com",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ID_TOKEN: "idt",
            CONF_REFRESH_TOKEN: "rt",
            CONF_DEVICE_ID: "dev-1",
            "email": "someone@example.com",
        },
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 5,
        },
    )
    entry.add_to_hass(hass)
    with patch(ACCOUNT_GET, new=AsyncMock(return_value=overview)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_reapply_reaches_every_account_list_without_a_poll(hass):
    account = await _setup_account_with_outgoing(hass)
    coordinator = account.runtime_data.coordinator
    codes = [
        ACCOUNT_ACTIVE_CODE,
        ACCOUNT_DELIVERED_CODE,
        ACCOUNT_OUT_ID,
        ACCOUNT_OUT_DELIVERED,
    ]
    with (
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh,
        patch(ACCOUNT_GET, new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        for code in codes:
            await hass.services.async_call(
                DOMAIN,
                "update_contents",
                {CONF_TRACKING_CODE: code, "contents": f"Text {code}"},
                blocking=True,
            )
        assert listeners.call_count == 4
        for code in codes:
            assert _contents(account, code) == f"Text {code}"
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: ACCOUNT_OUT_ID},
            blocking=True,
        )
        assert _contents(account, ACCOUNT_OUT_ID) is None
        assert (
            _contents(account, ACCOUNT_OUT_DELIVERED) == f"Text {ACCOUNT_OUT_DELIVERED}"
        )
    refresh.assert_not_called()
    fetch.assert_not_called()


async def test_reapply_reaches_delivered_hub_parcels(hass):
    entry = await _setup(hass, [DELIVERED_CODE])
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 5,
        },
    )
    coordinator = entry.runtime_data.coordinator
    with patch(HUB_GET, new=AsyncMock(return_value=delivered_sample())):
        await coordinator.async_refresh()
    assert coordinator.delivered
    with patch(HUB_GET, new=AsyncMock()) as fetch:
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: DELIVERED_CODE, "contents": "Winter socks"},
            blocking=True,
        )
    fetch.assert_not_called()
    assert coordinator.delivered[0]["contents"] == "Winter socks"


async def test_a_parcel_without_a_barcode_keeps_its_contents(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    real = module.normalize_parcel

    def no_barcode(raw, **kwargs):
        parcel = real(raw, **kwargs)
        parcel["barcode"] = ""
        return parcel

    with (
        patch.object(module, "normalize_parcel", side_effect=no_barcode),
        patch(HUB_GET, new=AsyncMock(return_value=active_sample(HUB_CODE))),
    ):
        await coordinator.async_refresh()
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
            blocking=True,
        )
        assert coordinator.data[0]["contents"] == "Running shoes"
        await coordinator.async_refresh()
        assert coordinator.data[0]["contents"] == "Running shoes"


async def test_a_differing_response_barcode_keeps_the_text_after_reapply(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    with patch(HUB_GET, new=AsyncMock(return_value=active_sample(DELIVERED_CODE))):
        await coordinator.async_refresh()
    assert coordinator.data[0]["barcode"] == DELIVERED_CODE
    await hass.services.async_call(
        DOMAIN,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
        blocking=True,
    )
    assert coordinator.data[0]["contents"] == "Running shoes"


async def test_an_account_parcel_without_a_barcode_stays_without_contents(hass):
    account = await _setup_account(hass)
    coordinator = account.runtime_data.coordinator
    parcel = coordinator.data["incoming_active"][0]
    parcel["barcode"] = ""
    coordinator._parcel_codes.pop(id(parcel))
    parcel["contents"] = "Stale"
    coordinator.async_reapply_contents()
    assert parcel["contents"] is None


async def test_two_entries_listing_one_code_are_both_reapplied_without_a_poll(hass):
    home = await _setup_account(hass, "home@example.com")
    work = await _setup_account(hass, "work@example.com")
    counts = {}
    with (
        patch(ACCOUNT_GET, new=AsyncMock()) as fetch,
        patch.object(
            home.runtime_data.coordinator,
            "async_update_listeners",
            side_effect=lambda: counts.__setitem__("home", counts.get("home", 0) + 1),
        ),
        patch.object(
            work.runtime_data.coordinator,
            "async_update_listeners",
            side_effect=lambda: counts.__setitem__("work", counts.get("work", 0) + 1),
        ),
    ):
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: ACCOUNT_ACTIVE_CODE, "contents": "Running shoes"},
            blocking=True,
        )
    fetch.assert_not_called()
    assert counts == {"home": 1, "work": 1}
    assert _contents(home, ACCOUNT_ACTIVE_CODE) == "Running shoes"
    assert _contents(work, ACCOUNT_ACTIVE_CODE) == "Running shoes"


async def test_reapply_before_the_first_poll_publishes_nothing(hass):
    entry = await _setup(hass, [])
    coordinator = entry.runtime_data.coordinator
    coordinator.data = None
    coordinator.async_reapply_contents()
    account = await _setup_account(hass)
    account.runtime_data.coordinator.data = None
    account.runtime_data.coordinator.async_reapply_contents()
