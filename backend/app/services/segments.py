"""Link function (segment) from the roles at both ends: Backbone, Distribusi or Last Mile.

    backbone      core and edge routers among themselves (P/DCI/PE/BNG ↔ P/DCI/PE/BNG)
    distribution  aggregation/metro towards the edge or among itself (AGG ↔ AGG/PE/BNG/P/DCI)
    last_mile     anything touching an access device (OLT, CSR, access switch)

A device with an unknown role takes the function its known neighbour implies: next to an access device it is
last mile, otherwise distribution (it is at least not a known core router).
"""

from typing import Literal

from .areas import ROLE_TIER

Segment = Literal["backbone", "distribution", "last_mile"]
SEGMENTS: tuple[Segment, ...] = ("backbone", "distribution", "last_mile")
SEGMENT_LABELS: dict[str, str] = {"backbone": "Backbone", "distribution": "Distribusi", "last_mile": "Last Mile"}
SEGMENT_TAGS: dict[str, str] = {"backbone": "BB", "distribution": "DS", "last_mile": "LM"}  # port description prefix

UNKNOWN_TIER = 4


def link_segment(role_a: str | None, role_b: str | None) -> Segment:
    tiers = sorted(ROLE_TIER.get(role or "", UNKNOWN_TIER) for role in (role_a, role_b))
    if tiers[1] == 3 or tiers[0] == 3:
        return "last_mile"
    if tiers[1] <= 1:
        return "backbone"
    return "distribution"
