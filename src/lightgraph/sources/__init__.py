"""On-demand source adapters; database rows never have a persistence API."""
from .base import SourceAdapter, EphemeralBatch, QueryStats
from .memory import MemorySource
from .budget import QueryBudget
from .excel import ExcelSource
from .csv import CSVSource
from .gauss import GaussSource
__all__ = ["SourceAdapter", "EphemeralBatch", "QueryStats", "MemorySource", "ExcelSource", "CSVSource", "GaussSource", "QueryBudget"]
