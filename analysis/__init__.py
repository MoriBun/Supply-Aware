"""Analysis: evaluation of policies and uplift scores on simulator output.

Nothing in ``sim/`` imports this package. ``hidden/`` tables are
ground truth for evaluation only and are never joined into training data
(docs/schema.md): :func:`analysis.io.load_run` therefore never returns them;
use :func:`analysis.io.load_hidden` explicitly when evaluating.
"""
