"""Trend pipeline (design.md §8.4).

The stages are separate modules because they are separately testable and
separately wrong-able; `extraction` is the only one callers should reach for.
"""

from trends.services.extraction import cached_clusters, extract, top_cluster

__all__ = ["cached_clusters", "extract", "top_cluster"]
