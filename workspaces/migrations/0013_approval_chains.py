"""Approval chains (P2-04, P2-07) — the expand half.

`Workspace.requires_approval` is deliberately **not** dropped here. It is
what `0014` reads to give every existing workspace a chain that means the
same thing it did before, and a column removed before the migration that
reads it is a migration that reads nothing.
"""


import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0012_delete_postcomment'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='approvalaction',
            name='actor',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='approval_actions', to=settings.AUTH_USER_MODEL),
        ),
        migrations.CreateModel(
            name='ApprovalChain',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('is_default', models.BooleanField(default=False)),
                ('blocks_publish', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='approval_chains', to='workspaces.workspace')),
            ],
            options={
                'ordering': ['-is_default', 'name'],
            },
        ),
        migrations.CreateModel(
            name='ApprovalStage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.PositiveSmallIntegerField()),
                ('name', models.CharField(max_length=120)),
                ('min_approvals', models.PositiveSmallIntegerField(default=1)),
                ('allow_self_approve', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('chain', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='stages', to='workspaces.approvalchain')),
                ('required_approvers', models.ManyToManyField(blank=True, related_name='approval_stages', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['order'],
            },
        ),
        migrations.AddField(
            model_name='approvalaction',
            name='stage',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='approval_actions', to='workspaces.approvalstage'),
        ),
        migrations.AddConstraint(
            model_name='approvalchain',
            constraint=models.UniqueConstraint(condition=models.Q(('is_default', True)), fields=('workspace',), name='one_default_approval_chain_per_workspace'),
        ),
        migrations.AddConstraint(
            model_name='approvalchain',
            constraint=models.UniqueConstraint(fields=('workspace', 'name'), name='unique_approval_chain_name_per_workspace'),
        ),
        migrations.AddConstraint(
            model_name='approvalstage',
            constraint=models.UniqueConstraint(fields=('chain', 'order'), name='unique_approval_stage_order'),
        ),
        migrations.AddConstraint(
            model_name='approvalstage',
            constraint=models.CheckConstraint(condition=models.Q(('min_approvals__gte', 1)), name='approval_stage_needs_an_approval'),
        ),
    ]
