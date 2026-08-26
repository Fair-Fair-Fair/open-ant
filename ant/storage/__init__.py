"""Storage layer: conversation history + outbox/audit persistence.

Phase 1: MySQL (asyncmy) is the production backend; the legacy JSONL
backend is retained as ``JsonlHistoryRepository`` for dev/tests.
"""
