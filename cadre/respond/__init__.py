"""Response: what cADRe does about a finding. See README.md in this folder.

    ALERTS.update(records, now)      which findings are new (collectors.snapshot)
    act(snap, agent_id, action)      pause / resume / kill one agent (POST /api/respond)
    paused_by_us(pid, started, stat) did cADRe pause this process? (collectors.snapshot)
    release_orphans(listing)         let go of the shell of an agent that ended while paused
    needs_fg(pid)                    resumed, but its terminal still has to hear `fg`
    audit.entries(n)                 the response log, oldest first
    dismiss.mark(snap, who, rule)    mark a finding as a false positive, or take it back
    dismiss.apply_record(rec) / apply_event(ev)    hide what was marked (snapshot, explorer)
"""
from . import audit, dismiss                                    # noqa: F401
from .actions import ACTIONS, Refused, act, needs_fg, paused_by_us, release_orphans        # noqa: F401
from .alerts import ALERTS, AlertLog                            # noqa: F401
