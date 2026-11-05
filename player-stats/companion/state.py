"""Pure per-process/session state machine, independent of Win32 and HTTP."""
from __future__ import annotations

from collections import Counter





def report_key(report):
    return (int(report["variant_address"]), int(report["generation"]), int(report["result_address"]))


def roster_matches(players, participants):
    """Compare native setup IDs and sides, without deriving IDs from names.

    Equal rosters do not prove a unique session: consecutive games can have
    exactly the same participants. The session remains a header candidate.
    """
    if not isinstance(players, list) or not players:
        return False
    roster = []
    for player in players:
        identifier = player.get("player_user_id")
        side = player.get("alliance")
        if (not isinstance(identifier, str) or not identifier.isascii() or not identifier.isdecimal()
                or len(identifier) > 20 or int(identifier) >= 2**64 - 1
                or type(side) is not int or side not in (0, 1)):
            return False
        roster.append((str(int(identifier)), side))
    return Counter(roster) == Counter((p["id"], p["alliance"]) for p in participants)



