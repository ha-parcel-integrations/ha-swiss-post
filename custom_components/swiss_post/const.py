"""Constants for the Swiss Post parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "swiss_post"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping this carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Which optional contract fields this carrier's API actually populates — feeds
# the comparison table on the docs site. Keep in lockstep with
# normalize_parcel() in parcels.py: everything not listed here comes back as a
# literal None there. Swiss Post is the one carrier in the suite whose merged
# two-host payload fills every optional field, weight and dimensions included.
CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Swiss Post splits parcel tracking over two keyless hosts that each hold half
# the data, so this integration talks to both:
#
#   Surface A (``service.post.ch/ekp-web``) — status, ETA, weight, dimensions
#   and the delivery booleans. Its ``events`` array is *always* empty.
#   Surface B (``eosapi.postlogistics.ch``) — the event timeline, and nothing
#   else worth mapping (see ``parcels.build_history``).
#
# Neither needs a key or an account, but surface A needs a per-session
# anonymous handshake — cookie plus CSRF token.

# --- Surface A: the consumer tracking API behind Swiss Post's own web UI -----
EKP_USER_URL = "https://service.post.ch/ekp-web/api/user"
EKP_HISTORY_URL = "https://service.post.ch/ekp-web/api/history"
EKP_HISTORY_ITEM_URL = (
    "https://service.post.ch/ekp-web/api/history/not-included/{digest}"
)
EKP_REFERER = "https://service.post.ch/ekp-web/ui/"

# The handshake's CSRF token comes back on a *response* header. Swiss Post
# spells it upper-case; aiohttp's header mapping is case-insensitive, so read
# it through the response headers rather than a plain dict built from them.
CSRF_HEADER = "X-CSRF-TOKEN"

# The endpoint answers a plain HA user agent, but it is a consumer web API and
# a browser-shaped request is the traffic it expects to see.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# --- Surface B: the event timeline ------------------------------------------
EOS_HISTORY_URL = "https://eosapi.postlogistics.ch/api/trackandtrace/public"
EOS_ORIGIN = "https://tracking.postlogistics.ch"

# Language of the event descriptions (``de-DE``, ``fr-FR``, ``it-IT``, …).
# Swiss Post writes them in the parcel's own language, not the reader's, and
# German is the majority language in the delivery area — but the value is only
# ever shown as free text on a history entry, so a wrong guess is cosmetic.
EOS_CULTURE = "de-DE"

# Human-facing deep link surfaced on each parcel's ``url`` field: the same
# search page the ekp-web API sits behind.
TRACKING_URL = "https://service.post.ch/ekp-web/ui/entry/search/{tracking_code}"

# Tracked parcels live in the config entry options as a list of
# ``{tracking_code}`` dicts — this carrier has no account or parcel feed, so the
# user enters the codes themselves. Kept as dicts so future per-parcel fields
# slot in without an options migration.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional, no user-facing interval
# option.
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one tracked,
# not-yet-delivered parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of
# its planned_from (or has no planned_from at all); mid = anything else still
# in flight. This is a barcode-based coordinator (Section 2.1): when every
# tracked parcel is delivered, or nothing is tracked, polling stops entirely
# instead of falling to the mid tier — see coordinator.py's
# ``_hottest_tier_minutes``.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Here the option also decides the *call count*: the timeline only
# exists on surface B, so a parcel costs two requests with history off and
# three with it on. That is the whole reason it stays off by default.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20

# --- Source selection --------------------------------------------------------
# This integration has two independently-configured transports: the public
# tracking codes (surfaces A + B above) and the SwissID account inbox below.
# Existing entries predate CONF_SOURCE and are treated as tracking hubs.
CONF_SOURCE = "source"
SOURCE_TRACKING = "tracking"
SOURCE_ACCOUNT = "account"

# --- Account source: the SwissID-authenticated app backend (mobserv) ---------
# The logged-in user's parcel inbox, discovered automatically — no tracking
# codes. Reached with a SwissID OAuth token, same backend the Post app uses.
#
# OIDC is a standard Authorization-Code + PKCE flow against a **public** client
# (no secret). These values are transport material recovered from the official
# app, not user credentials: never surface them in UI, diagnostics or logs.
OIDC_AUTHORIZE_URL = "https://login.swissid.ch/idp/oauth2/authorize"
OIDC_TOKEN_URL = "https://login.swissid.ch/idp/oauth2/access_token"
OIDC_CLIENT_ID = "swisspost_main_prod"
OIDC_REDIRECT_URI = "https://app.post.ch/mainapp/auth/callback"
OIDC_SCOPE = "openid profile email address phone"

# The inbox endpoint. The bearer it accepts is the JWT **id_token**, not the
# opaque access_token (the access_token is rejected as "Malformed token"); any
# UUID works as the device id.
ACCOUNT_OVERVIEW_URL = "https://app.post.ch/mobserv/v1/mailpiece/tracking/overview"

# The per-parcel enrichment. The inbox is a summary — no weight, dimensions or
# event timeline — so those come from a second call per parcel.
#
# Deliberately the *anonymous* ``search`` endpoint keyed on the tracking number,
# not the authenticated ``detail`` endpoint keyed on the inbox's opaque
# ``mailpieceKey``: both return the same record, but this one has been seen
# answering a real ``200`` and needs no bearer, so an enrichment failure can
# never cost the entry its token chain. ``dontFollow=true`` keeps the lookup
# from attaching the parcel to this device's followed list on every poll.
ACCOUNT_DETAIL_URL = "https://app.post.ch/mobserv/v1/mailpiece/tracking/search"

# How many enrichment calls may be in flight at once. The inbox is small, but a
# busy account should not fan out one request per parcel simultaneously.
ACCOUNT_DETAIL_CONCURRENCY = 5

# Opt-in, and off by default, because it costs one request per parcel per poll
# on top of the single inbox call — the same trade-off as the tracking surface's
# history option, which is why that one is off by default too.
CONF_ACCOUNT_DETAILS = "account_details"
DEFAULT_ACCOUNT_DETAILS = False

# Refresh the id_token this many seconds before it actually expires, so a poll
# never races the expiry. The id_token lives 3600 s; the refresh_token rotates
# on every refresh and must be persisted back to the entry each time.
ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS = 120

# Config-entry keys for the account source. The device id is a per-entry random
# UUID (any value is accepted); the tokens rotate and are persisted on refresh.
CONF_ACCESS_TOKEN = "access_token"
CONF_ID_TOKEN = "id_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_DEVICE_ID = "device_id"
CONF_ACCOUNT_SUB = "account_sub"
# The account holder's email (from the id_token), used only to name the device
# and the config entry. Redacted from diagnostics.
CONF_EMAIL = "email"

# Human-facing deep link for an account parcel (no postcode needed; the search
# page resolves a bare tracking number).
ACCOUNT_TRACKING_URL = "https://service.post.ch/ekp-web/ui/entry/search/{tracking_code}"

# The redirect URI is meant to open the mobile app, so in a desktop browser it
# bounces straight on to a Swiss Post page advertising the app — the address
# carrying the code is never visible in the address bar and has to be caught in
# the browser's network log. These per-browser instructions are linked from the
# sign-in form, mirroring the DHL Germany flow.
REDIRECT_URL_DOCS_URL = (
    "https://github.com/ha-parcel-integrations/ha-swiss-post/blob/main/docs/"
    "finding-the-redirect-url.md"
)
