"""Minion software-engineering agent package.

The package is split into a control plane (API, orchestration, persistence,
queue/event transport) and an execution/runtime plane (sandbox, context, skills,
repository intelligence, tools, and model loop). Imports stay minimal here so
importing minion never starts workers or allocates resources.
"""
__version__ = "0.1.0"
