"""Config flow for the Swiss Post parcel tracker integration."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import (
    SwissPostAccountApiError,
    SwissPostAccountClient,
    SwissPostAccountInvalidAuth,
    decode_jwt_claims,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_DETAILS,
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
    DEFAULT_ACCOUNT_DETAILS,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    REDIRECT_URL_DOCS_URL,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)

_LOGGER = logging.getLogger(__name__)

# The field the user pastes the browser redirect URL into. Named as in the DHL
# Germany flow, the suite's other browser-paste OIDC carrier.
CONF_REDIRECT_URL = "redirect_url"
_REDIRECT_SCHEMA = vol.Schema({vol.Required(CONF_REDIRECT_URL): str})


def normalize_tracking_code(value: str) -> str:
    """Return the tracking code upper-cased with separators stripped."""
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def valid_tracking_code(value: str) -> bool:
    """Accept any non-empty code; the API decides what is real."""
    return bool(value)


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _parse_redirect_url(value: str) -> tuple[str | None, str | None]:
    """Pull ``code``/``state`` out of a pasted ``…/auth/callback?code=…`` URL.

    Users paste messily — leading/trailing whitespace, a trailing newline — so
    this strips first. Swiss Post adds its own ``iss`` and ``client_id`` params
    to the redirect; they are simply ignored.
    """
    try:
        query = parse_qs(urlparse(value.strip()).query)
    except ValueError:
        return None, None
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    return code, state


class SwissPostConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Swiss Post integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Hold the per-flow PKCE material across the account steps."""
        self._verifier: str | None = None
        self._state: str | None = None
        self._authorize_url: str = ""
        self._device_id: str | None = None
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> SwissPostOptionsFlowHandler:
        """Return the options flow handler."""
        return SwissPostOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the two sources: the SwissID account inbox or tracking codes."""
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the public tracking hub (single instance, no input needed).

        Tracking is keyed on the tracking code alone — the anonymous session the
        API needs is established behind the scenes — so there is nothing to ask:
        the hub is created straight away and parcels are added afterwards via the
        options flow, the ``swiss_post.track_parcel`` service or a button.
        """
        # The unique_id stays ``DOMAIN`` and must not be "improved" to
        # SOURCE_TRACKING: entries created before the account source existed
        # carry ``DOMAIN``, and changing it here would stop
        # _abort_if_unique_id_configured() recognising them — letting an
        # existing install add a second tracking hub polling the same surface.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Swiss Post",
            data={CONF_SOURCE: SOURCE_TRACKING},
            options={
                CONF_PARCELS: [],
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )

    async def _async_exchange(
        self, redirect_url: str
    ) -> tuple[str | None, dict[str, str] | None]:
        """Parse + exchange a pasted redirect URL.

        Returns ``(error_code, tokens)`` — exactly one of the two is set. The
        ``state`` check is what makes a redirect from an unrelated sign-in
        attempt fail instead of being silently accepted.
        """
        code, state = _parse_redirect_url(redirect_url)
        if not code or state != self._state:
            return "invalid_redirect", None
        client = SwissPostAccountClient(
            async_get_clientsession(self.hass), device_id=self._device_id
        )
        try:
            return None, await client.async_exchange_code(code, self._verifier or "")
        except SwissPostAccountInvalidAuth:
            return "invalid_auth", None
        except SwissPostAccountApiError:
            _LOGGER.debug("Failed to exchange the pasted redirect URL", exc_info=True)
            return "cannot_connect", None

    def _async_authorize_form(
        self, step_id: str, errors: dict[str, str], **placeholders: str
    ) -> ConfigFlowResult:
        """Show the authorize link plus the paste-back field.

        The PKCE pair and ``state`` are minted **once per flow** and reused when
        the form is re-rendered after an error. Minting new ones would change
        the challenge behind the link the user already signed in with, so a
        transient ``cannot_connect`` during the exchange would invalidate a code
        that was never spent — forcing them to redo the whole browser login and
        network-log capture for nothing. Reusing them lets the user simply paste
        again.
        """
        if self._verifier is None:
            self._authorize_url, self._verifier, self._state = (
                SwissPostAccountClient.build_authorize_url()
            )
        authorize_url = self._authorize_url
        if self._device_id is None:
            self._device_id = str(uuid.uuid4())
        return self.async_show_form(
            step_id=step_id,
            data_schema=_REDIRECT_SCHEMA,
            description_placeholders={
                "authorize_url": authorize_url,
                "docs_url": REDIRECT_URL_DOCS_URL,
                **placeholders,
            },
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in with SwissID via a pasted-back browser redirect.

        The sign-in deliberately runs in the user's own browser: SwissID may ask
        for a passkey, a fingerprint or an SMS code, and those only work on
        SwissID's own pages. That keeps the integration from ever asking anyone
        to weaken their account security.

        The redirect address is meant to open the mobile app, so a desktop
        browser bounces straight on to a Swiss Post app page — the user catches
        the address in their browser's network log (see ``docs_url``).
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            error, tokens = await self._async_exchange(user_input[CONF_REDIRECT_URL])
            if error is not None:
                errors["base"] = error
            else:
                assert tokens is not None
                claims = decode_jwt_claims(tokens["id_token"])
                sub = str(claims.get("sub") or "")
                email = claims.get("email")
                await self.async_set_unique_id(f"account:{sub}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Swiss Post ({email})" if email else "Swiss Post account",
                    data={
                        CONF_SOURCE: SOURCE_ACCOUNT,
                        CONF_DEVICE_ID: self._device_id,
                        CONF_ACCOUNT_SUB: sub,
                        CONF_EMAIL: email,
                        CONF_ID_TOKEN: tokens["id_token"],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                        CONF_ACCESS_TOKEN: tokens.get("access_token", ""),
                    },
                    options={
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_ACCOUNT_DETAILS: DEFAULT_ACCOUNT_DETAILS,
                    },
                )

        return self._async_authorize_form(SOURCE_ACCOUNT, errors)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication for the account whose refresh chain died."""
        self._reauth_entry = self._get_reauth_entry()
        self._device_id = entry_data.get(CONF_DEVICE_ID) or str(uuid.uuid4())
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-run the browser login and replace only the stored tokens."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None
        if user_input is not None:
            error, tokens = await self._async_exchange(user_input[CONF_REDIRECT_URL])
            if error is not None:
                errors["base"] = error
            else:
                assert tokens is not None
                claims = decode_jwt_claims(tokens["id_token"])
                # Never let a reauth silently move the entry to another account.
                await self.async_set_unique_id(f"account:{claims.get('sub') or ''}")
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_ID_TOKEN: tokens["id_token"],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                        CONF_ACCESS_TOKEN: tokens.get("access_token", ""),
                        CONF_DEVICE_ID: self._device_id,
                    },
                )

        return self._async_authorize_form(
            "reauth_confirm", errors, email=entry.data.get(CONF_EMAIL) or ""
        )


class SwissPostOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels and integration settings.

    The ``parcels`` page is only offered for a tracking hub — an account inbox
    discovers its own parcels, so it has nothing to edit there.
    """

    def _is_tracking(self) -> bool:
        """Whether this entry is a tracking hub (vs an account inbox)."""
        return (
            self.config_entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_TRACKING
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        menu_options = ["settings"]
        if self._is_tracking():
            menu_options.insert(0, "parcels")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete tracked-code list."""
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = list(
                dict.fromkeys(
                    normalize_tracking_code(code)
                    for code in user_input.get("tracking_codes", [])
                    if normalize_tracking_code(code)
                )
            )
            if any(not valid_tracking_code(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
                    },
                )

        current_codes = [
            parcel[CONF_TRACKING_CODE] for parcel in _current_parcels(self.config_entry)
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle non-parcel integration settings.

        The last toggle differs per source. A tracking hub gets the history
        option, which buys the event timeline from the second public host. An
        account inbox gets the details option instead: there, the timeline comes
        bundled with the weight and the dimensions in one enrichment call per
        parcel, so offering two separate switches would be a lie about what the
        extra requests buy.
        """
        extra_option = (
            CONF_INCLUDE_HISTORY if self._is_tracking() else CONF_ACCOUNT_DETAILS
        )
        extra_default = (
            DEFAULT_INCLUDE_HISTORY if self._is_tracking() else DEFAULT_ACCOUNT_DETAILS
        )

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    extra_option: bool(user_input[extra_option]),
                },
            )

        current = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DELIVERED_FILTER_TYPE,
                        default=current.get(
                            CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["days", "parcels"],
                            translation_key=CONF_DELIVERED_FILTER_TYPE,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_DELIVERED_FILTER_AMOUNT,
                        default=current.get(
                            CONF_DELIVERED_FILTER_AMOUNT,
                            DEFAULT_DELIVERED_FILTER_AMOUNT,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        extra_option,
                        default=current.get(extra_option, extra_default),
                    ): selector.BooleanSelector(),
                }
            ),
        )
