
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('content', '0010_post_visibility'),
        ('workspaces', '0013_approval_chains'),
    ]

    operations = [
        migrations.AddField(
            model_name='post',
            name='current_stage',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='posts_in_review', to='workspaces.approvalstage'),
        ),
        migrations.AddField(
            model_name='post',
            name='locked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='post',
            name='proposed_delivery_mode',
            field=models.CharField(blank=True, choices=[('REMINDER', 'Reminder'), ('AUTO_PUBLISH', 'Auto-publish')], max_length=16),
        ),
        migrations.AddField(
            model_name='post',
            name='proposed_scheduled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
