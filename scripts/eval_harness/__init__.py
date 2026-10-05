"""Standalone LLM-eval harness for the test-prs-dataset gold benchmark.

Independent of ``src/app``: it defines its own JSON schema, prompt, and
scoring so it can validate any reviewer's output format without depending on
the backend's internal contracts.
"""
