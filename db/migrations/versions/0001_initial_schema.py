"""Initial schema: shop, conversation and outcome tables.

The constraints here are the backend guarantees. Services rely on them,
and tests/test_schema.py tries to break each one directly.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE = """
CREATE TYPE action_kind     AS ENUM ('return', 'exchange');
CREATE TYPE proposal_status AS ENUM ('open', 'confirmed', 'superseded', 'declined', 'committed', 'expired');
CREATE TYPE action_status   AS ENUM ('active', 'cancelled');
CREATE TYPE review_status   AS ENUM ('open', 'closed');
CREATE TYPE assistant_kind  AS ENUM ('workflow', 'agent');

-- ---------------------------------------------------------------- the shop
-- Seeded from fixtures; readable text IDs so scenarios can name them.

CREATE TABLE shoppers (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    persona_key text NOT NULL UNIQUE
);

CREATE TABLE styles (
    id     text PRIMARY KEY,
    name   text NOT NULL,
    colour text NOT NULL
);

CREATE TABLE skus (
    id       text PRIMARY KEY,
    style_id text NOT NULL REFERENCES styles (id),
    size     text NOT NULL,
    -- Stock can never go negative, so two shoppers cannot both get the last unit.
    stock    integer NOT NULL CHECK (stock >= 0),
    UNIQUE (style_id, size)
);

CREATE TABLE orders (
    id           text PRIMARY KEY,
    shopper_id   text NOT NULL REFERENCES shoppers (id),
    placed_on    date NOT NULL,
    delivered_on date              -- NULL = no delivery record, which goes to review
);
CREATE INDEX orders_shopper_idx ON orders (shopper_id);

-- One row per purchased unit, so two identical shirts are two rows.
CREATE TABLE order_units (
    id         text PRIMARY KEY,
    order_id   text NOT NULL REFERENCES orders (id),
    sku_id     text NOT NULL REFERENCES skus (id),
    final_sale boolean NOT NULL DEFAULT false   -- a property of this purchase, not the style
);
CREATE INDEX order_units_order_idx ON order_units (order_id);

-- ------------------------------------------------------- the conversation

CREATE TABLE sessions (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    shopper_id     text NOT NULL REFERENCES shoppers (id),   -- fixed at start; tools never take a shopper ID
    assistant      assistant_kind NOT NULL,
    config_version text NOT NULL,
    trace_id       text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Each version of a proposal is its own immutable row. A correction marks the
-- old row 'superseded' and inserts version + 1, so the history of what the
-- shopper was shown is kept for replays.
CREATE TABLE proposals (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id         uuid NOT NULL REFERENCES sessions (id),
    order_unit_id      text NOT NULL REFERENCES order_units (id),
    action             action_kind NOT NULL,
    replacement_sku_id text REFERENCES skus (id),
    version            integer NOT NULL CHECK (version >= 1),
    status             proposal_status NOT NULL DEFAULT 'open',
    summary            text NOT NULL,              -- exactly what the confirmation screen showed
    confirmed_at       timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    -- An exchange needs a replacement size; a return must not have one.
    CONSTRAINT proposal_shape CHECK ((action = 'exchange') = (replacement_sku_id IS NOT NULL)),
    -- 'confirmed' and 'committed' rows always record when the shopper confirmed.
    CONSTRAINT confirmed_has_time CHECK (status NOT IN ('confirmed', 'committed') OR confirmed_at IS NOT NULL),
    UNIQUE (session_id, version)
);
-- At most one proposal per session is waiting on the shopper, so "Confirm" is never ambiguous.
CREATE UNIQUE INDEX one_live_proposal_per_session
    ON proposals (session_id) WHERE status IN ('open', 'confirmed');

CREATE TABLE messages (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id    uuid NOT NULL REFERENCES sessions (id),
    role          text NOT NULL CHECK (role IN ('shopper', 'assistant')),
    text          text NOT NULL,
    question_kind text CHECK (question_kind IN
        ('which_item', 'which_size', 'confirm', 'choose_alternative', 'offer_review', 'other')),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_session_idx ON messages (session_id, id);

CREATE TABLE tool_calls (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id    uuid NOT NULL REFERENCES sessions (id),
    trace_id      text NOT NULL,
    turn          integer NOT NULL,
    tool          text NOT NULL,
    args          jsonb NOT NULL DEFAULT '{}',
    result_status text NOT NULL CHECK (result_status IN ('ok', 'refused', 'error', 'timeout')),
    reason_code   text,             -- set when refused; the grader counts invalid write attempts from this
    duration_ms   integer,
    est_cost_usd  numeric(10, 6),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tool_calls_session_idx ON tool_calls (session_id, id);

-- -------------------------------------------------- the outcomes (graded)

CREATE TABLE actions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reference          text NOT NULL UNIQUE,
    -- One proposal can be committed only once: a double click or a retry
    -- after a lost response finds this row and returns its reference.
    proposal_id        uuid NOT NULL UNIQUE REFERENCES proposals (id),
    order_unit_id      text NOT NULL REFERENCES order_units (id),
    action             action_kind NOT NULL,
    replacement_sku_id text REFERENCES skus (id),
    status             action_status NOT NULL DEFAULT 'active',
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT action_shape CHECK ((action = 'exchange') = (replacement_sku_id IS NOT NULL))
);
-- A unit can have only one active action, even from two different proposals.
CREATE UNIQUE INDEX one_active_action_per_unit
    ON actions (order_unit_id) WHERE status = 'active';

CREATE TABLE review_cases (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    shopper_id    text NOT NULL REFERENCES shoppers (id),
    order_unit_id text REFERENCES order_units (id),   -- NULL when no unit was identified
    reason        text NOT NULL,
    question      text NOT NULL,
    trace_id      text NOT NULL,
    status        review_status NOT NULL DEFAULT 'open',
    created_at    timestamptz NOT NULL DEFAULT now()
);
-- Asking for review again about the same thing reuses the open case.
CREATE UNIQUE INDEX one_open_case_per_issue
    ON review_cases (shopper_id, coalesce(order_unit_id, ''), reason) WHERE status = 'open';
"""

DOWNGRADE = """
DROP TABLE review_cases, actions, tool_calls, messages, proposals, sessions,
           order_units, orders, skus, styles, shoppers;
DROP TYPE assistant_kind, review_status, action_status, proposal_status, action_kind;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
