"""Report assembly for the joint dispatch CLI."""
from .engine import solve
from .model import validate


def run(data):
    return solve(validate(data))
