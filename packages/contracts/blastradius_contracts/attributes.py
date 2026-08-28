"""Attribute keys, domain names, service names, and operation names.

Every one of these strings crosses a process boundary and Day 2 detection logic
depends on them. They live here, in the one package all three services import,
rather than being mirrored per service. Never inline a literal.
"""

# --- span attribute keys ---------------------------------------------------
DOMAIN_KEY = "blastradius.domain"
BLOCKING_KEY = "blastradius.blocking"

#: Deviation from v1.2 (see docs/superpowers/specs/2026-08-27-*-design.md §4.2).
#: Carries the logical parent across the auto-instrumented HTTP boundary so the
#: exporter can reconstruct the §6.2 parent edge after filtering auto spans.
PARENT_SPAN_ID_KEY = "blastradius.parent_span_id"

ORDER_ID_KEY = "order.id"
ORDER_CHANNEL_KEY = "order.channel"
ORDER_HAS_PROMO_KEY = "order.has_promo"
ORDER_PAYMENT_METHOD_KEY = "order.payment_method"

DB_POOL_WAIT_MS_KEY = "db.pool.wait_ms"
HTTP_STATUS_CODE_KEY = "http.status_code"
PAYMENT_METHOD_KEY = "payment.method"
ERROR_KIND_KEY = "error.kind"

# --- error kinds (v1.2 §6.4) ----------------------------------------------
ERROR_KIND_TIMEOUT = "timeout"
ERROR_KIND_UPSTREAM_ERROR = "upstream_error"
ERROR_KIND_POOL_TIMEOUT = "pool_timeout"

# --- HTTP header carrying PARENT_SPAN_ID_KEY across the promo hop ----------
PARENT_SPAN_ID_HEADER = "blastradius-parent"

# --- emitting services: real OTel resources, only these two exist ----------
SERVICE_ORDERING_APP = "ordering-app"
SERVICE_PROMO_PROVIDER = "promo-provider"

# --- attribution domains: logical failure domains (v1.2 §3.2 seed) ---------
DOMAIN_ORDERING_APP = "ordering-app"
DOMAIN_PROMO_PROVIDER = "promo-provider"
DOMAIN_PAYMENT_GATEWAY = "payment-gateway"
DOMAIN_ORDER_DATASTORE = "order-datastore"

# --- operations (v1.2 §6.2) ------------------------------------------------
OP_CHECKOUT = "checkout"
OP_VALIDATE_ORDER = "validate_order"
OP_PRICING = "pricing"
OP_LOYALTY_TIER_LOOKUP = "loyalty_tier_lookup"
OP_DB_POOL_ACQUIRE = "db.pool_acquire"
OP_PROMO_APPLY = "promo.apply"
OP_PROMO_HANDLE = "promo.handle"
OP_PAYMENT_AUTHORIZE = "payment.authorize"
OP_DB_PERSIST_ORDER = "db.persist_order"
OP_ANALYTICS_PUBLISH = "analytics.publish"
OP_CONFIRMATION = "confirmation"
