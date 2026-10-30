"""Offline evaluation; Gold data is loaded only by explicit scoring commands."""
from .core import evaluate
from .models import EvaluationReport, GoldGraph

__all__ = ['evaluate', 'EvaluationReport', 'GoldGraph']
