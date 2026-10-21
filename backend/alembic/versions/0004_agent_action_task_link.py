"""agent action task link

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21 17:28:43.167196

Phase 3 S4. Links an agent action to the task, and the task step, that proposed it.

* `agent_actions.task_id` (nullable, indexed, FK -> agent_tasks.id ON DELETE SET NULL): list a task's proposals.
* `agent_actions.task_step_id` (nullable, UNIQUE, FK -> agent_task_steps.id ON DELETE SET NULL): one step can
  create at most one action, so a resumed or retried step can never propose twice.

Backward compatible: both columns are nullable and NULL for every existing (chat) action. Batch mode is used
because SQLite can only add constraints by copying the table; on PostgreSQL the same operations are plain ALTERs.
Downgrade removes only the task link: every action row, its status and its payload are kept.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('agent_actions') as batch:
        batch.add_column(sa.Column('task_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('task_step_id', sa.Integer(), nullable=True))
        batch.create_index(batch.f('ix_agent_actions_task_id'), ['task_id'], unique=False)
        batch.create_unique_constraint(batch.f('uq_agent_actions_task_step_id'), ['task_step_id'])
        batch.create_foreign_key(
            batch.f('fk_agent_actions_task_id_agent_tasks'), 'agent_tasks', ['task_id'], ['id'], ondelete='SET NULL'
        )
        batch.create_foreign_key(
            batch.f('fk_agent_actions_task_step_id_agent_task_steps'),
            'agent_task_steps',
            ['task_step_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('agent_actions') as batch:
        batch.drop_constraint(batch.f('fk_agent_actions_task_step_id_agent_task_steps'), type_='foreignkey')
        batch.drop_constraint(batch.f('fk_agent_actions_task_id_agent_tasks'), type_='foreignkey')
        batch.drop_constraint(batch.f('uq_agent_actions_task_step_id'), type_='unique')
        batch.drop_index(batch.f('ix_agent_actions_task_id'))
        batch.drop_column('task_step_id')
        batch.drop_column('task_id')
