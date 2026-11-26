"""Model call log, workflow state, and the which_action question kind.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        -- The fixed workflow's state machine remembers where it is between turns.
        ALTER TABLE sessions ADD COLUMN state jsonb NOT NULL DEFAULT '{}';

        -- "Return or exchange?" needs its own tag so the simulated shopper can answer it.
        ALTER TABLE messages DROP CONSTRAINT messages_question_kind_check;
        ALTER TABLE messages ADD CONSTRAINT messages_question_kind_check CHECK (question_kind IN
            ('which_item', 'which_size', 'which_action', 'confirm', 'choose_alternative',
             'offer_review', 'other'));

        -- Every model call, for cost, latency and failure reporting.
        CREATE TABLE model_calls (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            session_id    uuid NOT NULL REFERENCES sessions (id),
            trace_id      text NOT NULL,
            turn          integer NOT NULL,
            purpose       text NOT NULL,        -- e.g. 'extract', 'agent_step'
            model         text NOT NULL,        -- name@digest
            prompt_tokens integer,
            output_tokens integer,
            duration_ms   integer,
            error         text,                 -- e.g. 'malformed_tool_call', 'timeout'
            created_at    timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX model_calls_session_idx ON model_calls (session_id, id);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE model_calls;
        DELETE FROM messages WHERE question_kind = 'which_action';
        ALTER TABLE messages DROP CONSTRAINT messages_question_kind_check;
        ALTER TABLE messages ADD CONSTRAINT messages_question_kind_check CHECK (question_kind IN
            ('which_item', 'which_size', 'confirm', 'choose_alternative', 'offer_review',
             'other'));
        ALTER TABLE sessions DROP COLUMN state;
    """)
