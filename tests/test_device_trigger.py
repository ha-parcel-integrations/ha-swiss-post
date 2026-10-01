"""Tests for Swiss Post device triggers."""
from custom_components.swiss_post.const import DOMAIN
from custom_components.swiss_post.device_trigger import (
    TRIGGER_EVENTS,
    async_get_triggers,
)


async def test_get_triggers_returns_the_incoming_set(hass):
    """The four receiver-side triggers exist on every Swiss Post device.

    The two sender-side ones are account-only — see
    ``test_outgoing_triggers_only_offered_on_an_account_hub``.
    """
    triggers = await async_get_triggers(hass, "device123")
    types = {t["type"] for t in triggers}
    assert types == {
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    }
    for trigger in triggers:
        assert trigger["domain"] == DOMAIN
        assert trigger["device_id"] == "device123"


def test_trigger_events_map_to_domain_prefix():
    assert TRIGGER_EVENTS["parcel_registered"] == f"{DOMAIN}_parcel_registered"


async def test_outgoing_triggers_only_offered_on_an_account_hub(hass):
    """A tracking hub has no sender side, so those triggers could never fire."""
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.swiss_post.const import (
        CONF_SOURCE,
        SOURCE_ACCOUNT,
        SOURCE_TRACKING,
    )

    outgoing = {"outgoing_parcel_status_changed", "outgoing_parcel_delivered"}
    registry = dr.async_get(hass)
    seen = {}
    for source, unique_id in ((SOURCE_TRACKING, DOMAIN), (SOURCE_ACCOUNT, "account:u")):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=unique_id, data={CONF_SOURCE: source}
        )
        entry.add_to_hass(hass)
        device = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
        )
        seen[source] = {t["type"] for t in await async_get_triggers(hass, device.id)}

    assert not (seen[SOURCE_TRACKING] & outgoing)
    assert outgoing <= seen[SOURCE_ACCOUNT]
    # The incoming set is identical on both.
    assert "parcel_delivered" in seen[SOURCE_TRACKING]
    assert "parcel_delivered" in seen[SOURCE_ACCOUNT]


async def test_unknown_device_gets_no_outgoing_triggers(hass):
    triggers = await async_get_triggers(hass, "does-not-exist")
    assert "outgoing_parcel_delivered" not in {t["type"] for t in triggers}
