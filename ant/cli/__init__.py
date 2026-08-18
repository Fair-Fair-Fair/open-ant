"""CLI interface for open-ant.

Deliberately import-free: `python -m ant.cli.main` warns (runpy) when the
package eagerly imports its own module, and `open-ant`/`python -m ant`
don't need it.
"""
