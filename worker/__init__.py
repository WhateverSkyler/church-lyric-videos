"""Render worker for the church PC.

This file must exist. Without it `worker` is only a namespace package, and
because worker/worker.py shares the package's own name, `from worker import
guard` resolves against the wrong thing and fails with an ImportError that
looks nothing like a missing __init__.
"""
