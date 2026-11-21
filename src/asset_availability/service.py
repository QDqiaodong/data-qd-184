"""Report assembly for the date-level availability CLI."""
from .engine import report


def run(data):
    return report(data)
