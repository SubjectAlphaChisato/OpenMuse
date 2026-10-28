"""crm phase 2: invitations, account_requests, messages.account_request_id

Revision ID: a7d4e2c1b8f3
Revises: f3c1a8d2b9e4
Create Date: 2026-10-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a7d4e2c1b8f3'
down_revision = 'f3c1a8d2b9e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('invitations',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('org_id', sa.String(length=26), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('phone', sa.String(length=32), nullable=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('partner_id', sa.String(length=26), nullable=True),
        sa.Column('initial_assignments', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('invited_by', sa.String(length=26), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('accepted_user_id', sa.String(length=26), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'advisor', 'partner')", name='ck_invitations_role'),
        sa.CheckConstraint("(role = 'partner') = (partner_id IS NOT NULL)", name='ck_invitations_partner_role'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['partner_id'], ['partners.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['invited_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'))
    op.create_index(op.f('ix_invitations_org_id'), 'invitations', ['org_id'], unique=False)

    op.create_table('account_requests',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('org_id', sa.String(length=26), nullable=False),
        sa.Column('requested_by', sa.String(length=26), nullable=False),
        sa.Column('conversation_id', sa.String(length=26), nullable=True),
        sa.Column('task_id', sa.String(length=26), nullable=True),
        sa.Column('action', sa.String(length=24), nullable=False),
        sa.Column('target_user_id', sa.String(length=26), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('requires_admin', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('decided_by', sa.String(length=26), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("action IN ('invite', 'change_role', 'assign_client', 'transfer_client', 'unassign_client', 'suspend', "
                           "'reactivate', 'deactivate', 'revoke_sessions')", name='ck_account_requests_action'),
        sa.CheckConstraint("status IN ('pending', 'completed', 'declined', 'expired', 'superseded', 'failed')", name='ck_account_requests_status'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['target_user_id'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_account_requests_org_id'), 'account_requests', ['org_id'], unique=False)

    op.add_column('messages', sa.Column('account_request_id', sa.String(length=26), nullable=True))


def downgrade() -> None:
    op.drop_column('messages', 'account_request_id')
    op.drop_table('account_requests')
    op.drop_table('invitations')
