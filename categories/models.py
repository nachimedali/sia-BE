"""Category tree (design.md §6.1, D11).

Categories cover every vertical, not a curated few: trend patterns are extracted
on demand per category and cached (D11), so breadth here costs nothing until a
workspace in that vertical actually asks for a Playbook.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering: ClassVar[list[str]] = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    def ancestors(self) -> list[Category]:
        """Root-first path to this category, excluding itself."""
        chain: list[Category] = []
        node = self.parent
        while node is not None:
            chain.append(node)
            node = node.parent
        return list(reversed(chain))
