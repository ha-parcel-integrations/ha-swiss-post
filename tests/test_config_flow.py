"""Tests for the Swiss Post config and options flow."""
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_post.account.client import (
    SwissPostAccountApiError,
    SwissPostAccountInvalidAuth,
)
from custom_components.swiss_post.config_flow import (
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.swiss_post.const import (
    CONF_ACCOUNT_SUB,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_ID_TOKEN,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)

_AUTHORIZE = (
    "https://login.swissid.ch/idp/oauth2/authorize?x=1",
    "VERIFIER",
    "STATE",
)
_REDIRECT_OK = (
    "https://app.post.ch/mainapp/auth/callback?code=THECODE&state=STATE"
)


def test_normalize_tracking_code_strips_and_uppercases():
    """Swiss Post prints the 18 digits in groups, so pasted codes carry separators."""
    assert normalize_tracking_code("99.00 1234.5678 9012 34") == "990012345678901234"
    assert normalize_tracking_code("rr123456789ch") == "RR123456789CH"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


def test_valid_tracking_code_accepts_any_non_empty_code():
    assert valid_tracking_code("990012345678901234")  # domestic, 18 digits
    assert valid_tracking_code("RR123456789CH")  # international, S10
    assert valid_tracking_code("ORDER12345")  # format unconfirmed, still accepted
    assert not valid_tracking_code("")


async def _pick_source(hass, source: str):
    """Open the config flow and choose one of the two sources."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "menu"
    assert result["menu_options"] == [SOURCE_ACCOUNT, SOURCE_TRACKING]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": source}
    )


async def test_tracking_flow_creates_hub_without_input(hass):
    """No account, no postcode — the tracking hub is created straight away."""
    result = await _pick_source(hass, SOURCE_TRACKING)
    assert result["type"] == "create_entry"
    assert result["title"] == "Swiss Post"
    assert result["data"][CONF_SOURCE] == SOURCE_TRACKING
    assert result["options"][CONF_PARCELS] == []


async def test_second_tracking_hub_rejected(hass):
    """Tracking is a single hub — the codes are not scoped to anything.

    The unique_id must stay ``DOMAIN``: hubs created before the account source
    existed carry that value, and an install upgrading into this version must
    not be able to add a second hub polling the same surface.
    """
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    result = await _pick_source(hass, SOURCE_TRACKING)
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_account_hub_allowed_alongside_a_tracking_hub(hass):
    """The two sources are separate hubs and must coexist."""
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    with (
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".build_authorize_url",
            return_value=_AUTHORIZE,
        ),
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".async_exchange_code",
            new=AsyncMock(
                return_value={
                    "id_token": "idt",
                    "refresh_token": "rt",
                    "access_token": "at",
                }
            ),
        ),
        patch(
            "custom_components.swiss_post.config_flow.decode_jwt_claims",
            return_value={"sub": "user-1", "email": "me@example.ch"},
        ),
    ):
        result = await _pick_source(hass, SOURCE_ACCOUNT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _REDIRECT_OK}
        )
    assert result["type"] == "create_entry"


async def test_account_flow_keeps_the_pkce_pair_across_a_retry(hass):
    """A transient failure must not invalidate an unspent authorization code.

    Minting a new challenge on the error re-render would silently break the link
    the user already signed in with, forcing them to redo the whole browser
    login and network-log capture.
    """
    exchange = AsyncMock(
        side_effect=[
            SwissPostAccountApiError("down"),
            {"id_token": "idt", "refresh_token": "rt", "access_token": "at"},
        ]
    )
    with (
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".build_authorize_url",
            return_value=_AUTHORIZE,
        ) as build,
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".async_exchange_code",
            new=exchange,
        ),
        patch(
            "custom_components.swiss_post.config_flow.decode_jwt_claims",
            return_value={"sub": "user-1", "email": "me@example.ch"},
        ),
    ):
        result = await _pick_source(hass, SOURCE_ACCOUNT)
        first_url = result["description_placeholders"]["authorize_url"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _REDIRECT_OK}
        )
        assert result["errors"] == {"base": "cannot_connect"}
        # Same link, same challenge -- the code the user already holds is valid.
        assert result["description_placeholders"]["authorize_url"] == first_url
        assert build.call_count == 1

        # Pasting the same address again now succeeds.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _REDIRECT_OK}
        )
    assert result["type"] == "create_entry"


async def test_account_flow_stores_only_the_tokens(hass):
    """The pasted redirect is exchanged for tokens; no password is ever held."""
    with (
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".build_authorize_url",
            return_value=_AUTHORIZE,
        ),
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".async_exchange_code",
            new=AsyncMock(
                return_value={
                    "id_token": "idt",
                    "refresh_token": "rt",
                    "access_token": "at",
                }
            ),
        ),
        patch(
            "custom_components.swiss_post.config_flow.decode_jwt_claims",
            return_value={"sub": "user-1", "email": "me@example.ch"},
        ),
    ):
        result = await _pick_source(hass, SOURCE_ACCOUNT)
        assert result["type"] == "form"
        assert result["step_id"] == SOURCE_ACCOUNT
        # The user needs the authorize link to sign in with.
        assert result["description_placeholders"]["authorize_url"] == _AUTHORIZE[0]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _REDIRECT_OK}
        )

    assert result["type"] == "create_entry"
    assert result["title"] == "Swiss Post (me@example.ch)"
    assert result["data"][CONF_SOURCE] == SOURCE_ACCOUNT
    assert result["data"][CONF_ID_TOKEN] == "idt"
    assert result["data"][CONF_REFRESH_TOKEN] == "rt"
    # A per-entry device id is generated for the inbox's x-device-id header.
    assert result["data"][CONF_DEVICE_ID]


async def test_account_flow_rejects_a_redirect_without_a_code(hass):
    """A pasted URL that carries no code must not be silently accepted."""
    with patch(
        "custom_components.swiss_post.config_flow.SwissPostAccountClient"
        ".build_authorize_url",
        return_value=_AUTHORIZE,
    ):
        result = await _pick_source(hass, SOURCE_ACCOUNT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"redirect_url": "https://app.post.ch/mainapp/auth/callback?state=STATE"},
        )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_redirect"}


async def test_account_flow_rejects_a_mismatched_state(hass):
    """A redirect from a different flow (wrong state) is refused."""
    with patch(
        "custom_components.swiss_post.config_flow.SwissPostAccountClient"
        ".build_authorize_url",
        return_value=_AUTHORIZE,
    ):
        result = await _pick_source(hass, SOURCE_ACCOUNT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "redirect_url": "https://app.post.ch/mainapp/auth/callback"
                "?code=THECODE&state=SOMETHINGELSE"
            },
        )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_redirect"}


async def test_account_options_menu_hides_the_parcel_page(hass):
    """An account inbox discovers its own parcels, so there is nothing to edit."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user-1",
        data={CONF_SOURCE: SOURCE_ACCOUNT},
        options={},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["settings"]


async def test_account_settings_offer_the_details_toggle_not_history(hass):
    """On the inbox the timeline arrives bundled with the weight and the size,
    so one toggle buys all three and a separate history switch would mislead."""
    from custom_components.swiss_post.const import CONF_ACCOUNT_DETAILS

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user-1",
        data={CONF_SOURCE: SOURCE_ACCOUNT},
        options={},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    keys = {str(key.schema) for key in result["data_schema"].schema}
    assert CONF_ACCOUNT_DETAILS in keys
    assert CONF_INCLUDE_HISTORY not in keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_ACCOUNT_DETAILS: True,
        },
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_ACCOUNT_DETAILS] is True


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: parcels},
    )


def _init_input(
    *, add="", remove=None, history=False,
    filter_type="days", amount=7,
) -> dict:
    """Build the sectioned options-form submission."""
    parcels: dict = {"add": add}
    if remove is not None:
        parcels["remove"] = remove
    return {
        "parcels": parcels,
        "delivered": {
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        "history": {CONF_INCLUDE_HISTORY: history},
    }


async def _open_options_step(hass, entry, step_id: str):
    """Start the options flow and select one of its two top-level routes."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_options_parcel_list_can_be_cleared(hass):
    """A submitted empty list removes the final manually tracked parcel."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: [{CONF_TRACKING_CODE: "EXAMPLE111111"}]})
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": []}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == []


async def test_options_settings_preserve_parcel_list(hass):
    """Saving settings must never replace the manually tracked parcel list."""
    parcels = [{CONF_TRACKING_CODE: "EXAMPLE111111"}]
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: parcels})
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 7, CONF_INCLUDE_HISTORY: False}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == parcels


async def _start_reauth(hass, entry):
    """Begin the reauth flow for ``entry``."""
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": "reauth",
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=dict(entry.data),
    )


def _account_entry(**data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user-1",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ACCOUNT_SUB: "user-1",
            CONF_EMAIL: "me@example.ch",
            CONF_ID_TOKEN: "old-idt",
            CONF_REFRESH_TOKEN: "old-rt",
            CONF_DEVICE_ID: "dev-1",
            **data,
        },
    )


async def test_reauth_replaces_only_the_tokens(hass):
    """A renewed sign-in must keep the entry, swapping just the token pair."""
    entry = _account_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".build_authorize_url",
            return_value=_AUTHORIZE,
        ),
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".async_exchange_code",
            new=AsyncMock(
                return_value={
                    "id_token": "new-idt",
                    "refresh_token": "new-rt",
                    "access_token": "new-at",
                }
            ),
        ),
        patch(
            "custom_components.swiss_post.config_flow.decode_jwt_claims",
            return_value={"sub": "user-1", "email": "me@example.ch"},
        ),
    ):
        result = await _start_reauth(hass, entry)
        assert result["type"] == "form"
        assert result["step_id"] == "reauth_confirm"
        # The form must carry both the fresh link and the how-to-find-it docs.
        assert result["description_placeholders"]["authorize_url"] == _AUTHORIZE[0]
        assert result["description_placeholders"]["docs_url"]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _REDIRECT_OK}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ID_TOKEN] == "new-idt"
    assert entry.data[CONF_REFRESH_TOKEN] == "new-rt"
    # The account identity is untouched.
    assert entry.data[CONF_ACCOUNT_SUB] == "user-1"


async def test_reauth_refuses_a_different_account(hass):
    """Signing in as somebody else must not hijack the existing entry."""
    entry = _account_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".build_authorize_url",
            return_value=_AUTHORIZE,
        ),
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".async_exchange_code",
            new=AsyncMock(
                return_value={
                    "id_token": "x",
                    "refresh_token": "y",
                    "access_token": "z",
                }
            ),
        ),
        patch(
            "custom_components.swiss_post.config_flow.decode_jwt_claims",
            return_value={"sub": "somebody-else"},
        ),
    ):
        result = await _start_reauth(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _REDIRECT_OK}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_ID_TOKEN] == "old-idt"


async def test_reauth_reports_a_rejected_sign_in(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".build_authorize_url",
            return_value=_AUTHORIZE,
        ),
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".async_exchange_code",
            new=AsyncMock(side_effect=SwissPostAccountInvalidAuth("no")),
        ),
    ):
        result = await _start_reauth(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _REDIRECT_OK}
        )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_account_flow_reports_a_connection_failure(hass):
    with (
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".build_authorize_url",
            return_value=_AUTHORIZE,
        ),
        patch(
            "custom_components.swiss_post.config_flow.SwissPostAccountClient"
            ".async_exchange_code",
            new=AsyncMock(side_effect=SwissPostAccountApiError("down")),
        ),
    ):
        result = await _pick_source(hass, SOURCE_ACCOUNT)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _REDIRECT_OK}
        )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


def test_parse_redirect_url_tolerates_messy_pasting():
    """Users paste with whitespace and newlines; extra params are ignored."""
    from custom_components.swiss_post.config_flow import _parse_redirect_url

    code, state = _parse_redirect_url(
        "  https://app.post.ch/mainapp/auth/callback"
        "?code=ABC&iss=https%3A%2F%2Flogin.swissid.ch&state=ST&client_id=x\n"
    )
    assert (code, state) == ("ABC", "ST")
    assert _parse_redirect_url("nonsense") == (None, None)
    assert _parse_redirect_url("") == (None, None)
