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
# anonymous handshake — cookie plus CSRF token. That flow, its traps and the
# full payload mapping live in ``carrier-research/api/swiss-post/``.

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

# Refresh interval (minutes) controls how often the coordinator polls the
# carrier. Default 30 min keeps the load on a consumer endpoint gentle; the
# minimum is 15 min for the same reason.
#
# Deliberate divergence from the HA Core rule that polling intervals are not
# user-configurable: that rule targets core integrations, and in a HACS parcel
# tracker a tunable cadence is a wanted feature. Generate with
# ``--interval fixed`` instead when the carrier throttles or soft-bans unusual
# traffic — that drops the option entirely and hard-codes the cadence, so users
# cannot dial it down to something that gets them blocked.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Here the option also decides the *call count*: the timeline only
# exists on surface B, so a parcel costs two requests with history off and
# three with it on. That is the whole reason it stays off by default.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
