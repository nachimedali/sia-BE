"""Pagination defaults (design.md §7)."""

from rest_framework.pagination import PageNumberPagination


class DefaultPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    # Bounded so a client cannot turn a list endpoint into a full table scan.
    max_page_size = 100
