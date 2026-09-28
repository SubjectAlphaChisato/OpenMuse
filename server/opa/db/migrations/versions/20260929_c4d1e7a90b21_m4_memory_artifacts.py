"""m4 memory and artifacts (MEM-01, ART-01)

Revision ID: c4d1e7a90b21
Revises: 2bbd65ea917f
Create Date: 2026-09-29 09:00:00.000000

Self-contained so it can be re-pointed when M3 merges.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'c4d1e7a90b21'
down_revision = '2bbd65ea917f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'memories',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('owner_id', sa.String(length=26), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('scope', sa.String(length=26), nullable=False),
        sa.Column('source_ref', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('confirmed', sa.Boolean(), nullable=False),
        sa.Column('tags', postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('tsv', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english', text)", persisted=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("kind IN ('user_statement', 'inference')", name='ck_memories_kind'),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_memories_owner_id', 'memories', ['owner_id'])
    op.create_index('ix_memories_tsv', 'memories', ['tsv'], postgresql_using='gin')

    op.create_table(
        'artifacts',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('owner_id', sa.String(length=26), nullable=False),
        sa.Column('task_id', sa.String(length=26), nullable=True),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('mime', sa.String(length=64), nullable=False),
        sa.Column('origin', sa.String(length=16), nullable=False),
        sa.Column('current_version', sa.Integer(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['owners.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_artifacts_owner_id', 'artifacts', ['owner_id'])
    op.create_index('ix_artifacts_task_id', 'artifacts', ['task_id'])

    op.create_table(
        'artifact_versions',
        sa.Column('artifact_id', sa.String(length=26), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('storage_key', sa.String(length=160), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('source_refs', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_by_task_id', sa.String(length=26), nullable=True),
        sa.Column('note', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('extracted_text', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('artifact_id', 'version'),
    )
    op.create_index('ix_artifact_versions_storage_key', 'artifact_versions', ['storage_key'])


def downgrade() -> None:
    op.drop_index('ix_artifact_versions_storage_key', table_name='artifact_versions')
    op.drop_table('artifact_versions')
    op.drop_index('ix_artifacts_task_id', table_name='artifacts')
    op.drop_index('ix_artifacts_owner_id', table_name='artifacts')
    op.drop_table('artifacts')
    op.drop_index('ix_memories_tsv', table_name='memories')
    op.drop_index('ix_memories_owner_id', table_name='memories')
    op.drop_table('memories')
