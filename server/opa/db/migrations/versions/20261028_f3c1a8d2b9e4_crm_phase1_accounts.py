"""crm phase 1 accounts: organizations, users (was owners), partners, clients, client_assignments

Revision ID: f3c1a8d2b9e4
Revises: e5a9c3b7d201
Create Date: 2026-10-28
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f3c1a8d2b9e4'
down_revision = 'e5a9c3b7d201'
branch_labels = None
depends_on = None

DEFAULT_ORG_ID = "00000000000000000000000000"


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)


def upgrade() -> None:
    op.create_table('organizations',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _ts('created_at'), _ts('updated_at'),
        sa.PrimaryKeyConstraint('id'))
    op.execute(f"INSERT INTO organizations (id, name, settings) VALUES ('{DEFAULT_ORG_ID}', 'Default', '{{}}')")

    op.create_table('partners',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('org_id', sa.String(length=26), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        _ts('created_at'), _ts('updated_at'),
        sa.CheckConstraint("status IN ('prospect', 'active', 'suspended', 'ended')", name='ck_partners_status'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_partners_org_id'), 'partners', ['org_id'], unique=False)

    op.create_table('clients',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('org_id', sa.String(length=26), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        _ts('created_at'), _ts('updated_at'),
        sa.CheckConstraint("status IN ('active', 'archived')", name='ck_clients_status'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_clients_org_id'), 'clients', ['org_id'], unique=False)

    # owners -> users. Foreign keys from every other table follow the rename automatically.
    op.rename_table('owners', 'users')
    op.execute("ALTER TABLE users RENAME CONSTRAINT owners_pkey TO users_pkey")
    op.execute("ALTER TABLE users RENAME CONSTRAINT owners_email_key TO users_email_key")
    op.add_column('users', sa.Column('org_id', sa.String(length=26), nullable=True))
    op.add_column('users', sa.Column('phone', sa.String(length=32), nullable=True))
    op.add_column('users', sa.Column('name', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('role', sa.String(length=16), nullable=True))
    op.add_column('users', sa.Column('partner_id', sa.String(length=26), nullable=True))
    op.add_column('users', sa.Column('status', sa.String(length=16), nullable=True))
    op.add_column('users', sa.Column('mfa_required', sa.Boolean(), nullable=True))
    op.add_column('users', sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('deactivated_by', sa.String(length=26), nullable=True))
    # Backfill: the existing owner(s) become active admins of the default organization.
    op.execute(f"UPDATE users SET org_id = '{DEFAULT_ORG_ID}', role = 'admin', status = 'active', mfa_required = false, "
               "name = split_part(email, '@', 1)")
    for col in ('org_id', 'name', 'role', 'status', 'mfa_required'):
        op.alter_column('users', col, nullable=False)
    op.create_index(op.f('ix_users_org_id'), 'users', ['org_id'], unique=False)
    op.create_foreign_key('fk_users_org_id', 'users', 'organizations', ['org_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key('fk_users_partner_id', 'users', 'partners', ['partner_id'], ['id'], ondelete='RESTRICT')
    op.create_check_constraint('ck_users_role', 'users', "role IN ('admin', 'advisor', 'partner')")
    op.create_check_constraint('ck_users_status', 'users', "status IN ('invited', 'active', 'suspended', 'deactivated')")
    op.create_check_constraint('ck_users_partner_role', 'users', "(role = 'partner') = (partner_id IS NOT NULL)")

    op.create_table('client_assignments',
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('org_id', sa.String(length=26), nullable=False),
        sa.Column('client_id', sa.String(length=26), nullable=False),
        sa.Column('user_id', sa.String(length=26), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('can_edit', sa.Boolean(), nullable=False),
        sa.Column('assigned_by', sa.String(length=26), nullable=True),
        _ts('assigned_at'),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_reason', sa.String(length=200), nullable=True),
        sa.CheckConstraint("kind IN ('primary', 'collaborator')", name='ck_client_assignments_kind'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['assigned_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_client_assignments_client_id'), 'client_assignments', ['client_id'], unique=False)
    op.create_index('uq_client_assignments_primary', 'client_assignments', ['client_id'], unique=True,
                    postgresql_where=sa.text("kind = 'primary' AND ended_at IS NULL"))
    op.create_index('ix_client_assignments_user_active', 'client_assignments', ['user_id'], unique=False,
                    postgresql_where=sa.text('ended_at IS NULL'))


def downgrade() -> None:
    op.drop_table('client_assignments')
    for name in ('ck_users_partner_role', 'ck_users_status', 'ck_users_role'):
        op.drop_constraint(name, 'users', type_='check')
    op.drop_constraint('fk_users_partner_id', 'users', type_='foreignkey')
    op.drop_constraint('fk_users_org_id', 'users', type_='foreignkey')
    op.drop_index(op.f('ix_users_org_id'), table_name='users')
    for col in ('deactivated_by', 'deactivated_at', 'last_login_at', 'mfa_required', 'status', 'partner_id', 'role', 'name', 'phone', 'org_id'):
        op.drop_column('users', col)
    op.execute("ALTER TABLE users RENAME CONSTRAINT users_email_key TO owners_email_key")
    op.execute("ALTER TABLE users RENAME CONSTRAINT users_pkey TO owners_pkey")
    op.rename_table('users', 'owners')
    op.drop_table('clients')
    op.drop_table('partners')
    op.drop_table('organizations')
