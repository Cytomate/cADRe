"""Detection rules, one module per category. Importing this package registers them all.

    commands.py   dangerous shell commands (a regex table)
    scope.py      credentials, .env files, writes outside the project
    secrets.py    keys, tokens and passwords in commands or prompts
    runaway.py    loops and bursts over a session's events
    process.py    how the agent process runs (permissions, CPU, folder)

A new category is a new module here, added to the import below.
"""
from . import commands, process, runaway, scope, secrets  # noqa: F401  (registers the rules)
