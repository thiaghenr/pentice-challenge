# initial schema
# Revision ID: 0001 · Revises: (none) · Created: 2026-10-27

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "building",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "source_snapshot",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("building_id", sa.String(length=36), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("etag", sa.String(length=200), nullable=True),
        sa.Column("last_modified", sa.String(length=100), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["building_id"],
            ["building.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_snapshot_building_hash",
        "source_snapshot",
        ["building_id", "content_hash"],
        unique=False,
    )
    op.create_table(
        "quiz",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("building_id", sa.String(length=36), nullable=False),
        sa.Column("source_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft", "published", "archived", name="quizstatus", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column(
            "strategy",
            sa.Enum(
                "by_section",
                "by_risk",
                "by_past_mistakes",
                name="strategy",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("llm_provider", sa.String(length=50), nullable=True),
        sa.Column("llm_model", sa.String(length=200), nullable=True),
        sa.Column("prompt_version", sa.String(length=50), nullable=True),
        sa.Column(
            "generation_mode",
            sa.Enum(
                "llm",
                "fallback_cache",
                "fallback_template",
                name="generationmode",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["building_id"],
            ["building.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"],
            ["source_snapshot.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_quiz_building_id"), "quiz", ["building_id"], unique=False)
    op.create_table(
        "attempt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("quiz_id", sa.String(length=36), nullable=False),
        sa.Column("guest_ref", sa.String(length=200), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("in_progress", "submitted", name="attemptstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("question_order_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_score", sa.Float(), nullable=True),
        sa.Column("final_pct", sa.Float(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("previous_attempt_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["previous_attempt_id"],
            ["attempt.id"],
        ),
        sa.ForeignKeyConstraint(
            ["quiz_id"],
            ["quiz.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quiz_id", "guest_ref", "attempt_number"),
    )
    op.create_index(op.f("ix_attempt_guest_ref"), "attempt", ["guest_ref"], unique=False)
    op.create_index(op.f("ix_attempt_quiz_id"), "attempt", ["quiz_id"], unique=False)
    op.create_table(
        "llm_generation_log",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("quiz_id", sa.String(length=36), nullable=True),
        sa.Column("building_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ok", "invalid", "timeout", "error", name="logstatus", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("validation_errors_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["building_id"],
            ["building.id"],
        ),
        sa.ForeignKeyConstraint(
            ["quiz_id"],
            ["quiz.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_llm_generation_log_quiz_id"), "llm_generation_log", ["quiz_id"], unique=False
    )
    op.create_table(
        "question",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("quiz_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "topic",
            sa.Enum(
                "check_in",
                "parking",
                "noise",
                "deposit",
                "pets",
                "smoking",
                "visitors",
                "amenities",
                "waste",
                "emergency",
                "other",
                name="topic",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("section", sa.String(length=200), nullable=False),
        sa.Column("source_quote", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("multi_select", sa.Boolean(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["quiz_id"],
            ["quiz.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quiz_id", "position"),
    )
    op.create_index(op.f("ix_question_quiz_id"), "question", ["quiz_id"], unique=False)
    op.create_table(
        "answer",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("question_id", sa.String(length=36), nullable=False),
        sa.Column("selected_option_ids_json", sa.JSON(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["question.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "question_id"),
    )
    op.create_index(op.f("ix_answer_attempt_id"), "answer", ["attempt_id"], unique=False)
    op.create_index(op.f("ix_answer_question_id"), "answer", ["question_id"], unique=False)
    op.create_table(
        "option",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("question_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["question.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("question_id", "position"),
    )
    op.create_index(op.f("ix_option_question_id"), "option", ["question_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_option_question_id"), table_name="option")
    op.drop_table("option")
    op.drop_index(op.f("ix_answer_question_id"), table_name="answer")
    op.drop_index(op.f("ix_answer_attempt_id"), table_name="answer")
    op.drop_table("answer")
    op.drop_index(op.f("ix_question_quiz_id"), table_name="question")
    op.drop_table("question")
    op.drop_index(op.f("ix_llm_generation_log_quiz_id"), table_name="llm_generation_log")
    op.drop_table("llm_generation_log")
    op.drop_index(op.f("ix_attempt_quiz_id"), table_name="attempt")
    op.drop_index(op.f("ix_attempt_guest_ref"), table_name="attempt")
    op.drop_table("attempt")
    op.drop_index(op.f("ix_quiz_building_id"), table_name="quiz")
    op.drop_table("quiz")
    op.drop_index("ix_source_snapshot_building_hash", table_name="source_snapshot")
    op.drop_table("source_snapshot")
    op.drop_table("building")
