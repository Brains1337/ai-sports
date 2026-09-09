"""Shared building blocks for the ai-sports sync scripts.

Every literal that crosses a script boundary lives in `constants`. Every
database write goes through `db.run_tracked` so a run that succeeds but
writes nothing is recorded as `empty` rather than looking like success.
"""
