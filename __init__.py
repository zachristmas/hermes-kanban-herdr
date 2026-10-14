"""Kanban Herdr: a dashboard section for the Herdr panes working a card (see dashboard/), and the
``kanban_herdr_link`` agent tool that tags a pane with its card (see herdr_tool.py)."""

try:
    from .herdr_tool import register as _register_tool
except ImportError:  # loaded outside a package (direct script use)
    from herdr_tool import register as _register_tool  # type: ignore


def register(ctx):
    _register_tool(ctx)
