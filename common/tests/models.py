"""Test-only models. Never migrated into a real database."""

from django.db import models

from common.encryption import EncryptedCharField, EncryptedTextField


class SecretHolder(models.Model):
    label = models.CharField(max_length=64)
    token = EncryptedCharField(max_length=255, null=True, blank=True)
    note = EncryptedTextField(null=True, blank=True)

    class Meta:
        app_label = "common_tests"

    def __str__(self) -> str:
        return self.label


class FakeWorkspace(models.Model):
    """Stands in for workspaces.Workspace, which lands in Phase 2."""

    name = models.CharField(max_length=64)

    class Meta:
        app_label = "common_tests"

    def __str__(self) -> str:
        return self.name


class ScopedThing(models.Model):
    workspace = models.ForeignKey(FakeWorkspace, on_delete=models.CASCADE)
    label = models.CharField(max_length=64)

    class Meta:
        app_label = "common_tests"

    def __str__(self) -> str:
        return self.label
