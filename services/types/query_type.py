"""Query type enum — no ML dependencies."""

from __future__ import annotations

from enum import Enum


class QueryType(Enum):
    SINGLE_ENTITY = "single_entity"
    DOCUMENTS = "documents"
    METADATA = "metadata"
    LIST = "list"
    AGGREGATION = "aggregation"
    TREE = "tree"
    DELETE_CRITERIA = "delete_criteria"
    BULK_UPDATE = "bulk_update"
    PROJECTION = "projection"
    UNKNOWN = "unknown"


__all__ = ["QueryType"]
