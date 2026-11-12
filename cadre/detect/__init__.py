"""cADRe detection: rules that flag risky agent behaviour. Watch-only: nothing is blocked.

Layout (see README.md in this folder for how to add a rule):

    engine.py     Rule, the registry, check_event / check_session / check_record
    helpers.py    command/path extraction, path tests, command_patterns() for regex tables
    rules/        the rules, one file per category
    review.py     what the rest of cADRe calls: findings for events and for agent records

The engine and rules are pure functions of their input (no I/O, no imports from the rest of
cADRe), so they can be tested alone and re-run over old events when a rule changes.
"""
from . import rules  # noqa: F401  (registers every rule)
from .engine import RULES, SEVERITIES, check_event, check_record, check_session, flag_text, rank
from .review import alerting, review_event, review_record

__all__ = ["RULES", "SEVERITIES", "check_event", "check_session", "check_record", "flag_text", "rank",
           "review_event", "review_record", "alerting"]
