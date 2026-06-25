#!/usr/bin/env python3
"""Logikk for sen påmelding i VM 2026-tippek konkurransen.

Modulen kjøres ikke som et eget workflow-steg. Den importeres av
``poengregning.py`` etter at alle ordinære poengkomponenter er beregnet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

EXPECTED_GROUP_MATCHES = 72
LATE_TYPE = "sen_pamelding"
ORDINARY_TYPE = "ordinaer"


def _canonical_group_matches(resultat_lookup: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Returnerer unike gruppespillkamper, uten eventuelle aliasoppføringer."""
    matches: dict[str, dict[str, Any]] = {}
    for key, kamp in (resultat_lookup or {}).items():
        if not isinstance(kamp, dict) or kamp.get("runde") != "gruppe":
            continue
        canonical = str(
            kamp.get("canonical_kamp_id")
            or kamp.get("alias_for")
            or kamp.get("kamp_id")
            or key
        )
        matches[canonical] = kamp
    return matches


def _round_half_up(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def apply_late_signup_points(
    stilling: list[dict[str, Any]],
    deltakere: dict[str, dict[str, Any]],
    resultat_lookup: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Legger startpoeng på senpåmeldte når gruppespillet er fullført.

    Gjennomsnittet beregnes bare fra deltakere som faktisk har levert
    gruppespillkupong. Senpåmeldte er aldri en del av grunnlaget.
    """
    group_matches = _canonical_group_matches(resultat_lookup)
    finished_count = sum(1 for kamp in group_matches.values() if bool(kamp.get("ferdig")))
    unfinished = sorted(
        key for key, kamp in group_matches.items() if not bool(kamp.get("ferdig"))
    )
    group_complete = (
        len(group_matches) >= EXPECTED_GROUP_MATCHES
        and finished_count >= EXPECTED_GROUP_MATCHES
        and not unfinished
    )

    ordinary_results: list[dict[str, Any]] = []
    late_count = 0
    for result in stilling:
        did = str(result.get("deltaker_id") or "")
        participant = deltakere.get(did, {}) or {}
        participant_type = str(participant.get("deltaker_type") or "").strip().lower()
        if not participant_type:
            participant_type = ORDINARY_TYPE if participant.get("gruppespill") else "ukjent"
        result["deltaker_type"] = participant_type
        result["sen_pamelding"] = participant_type == LATE_TYPE
        if result["sen_pamelding"]:
            late_count += 1
        elif participant.get("gruppespill"):
            ordinary_results.append(result)

    exact_average: float | None = None
    start_points: int | None = None
    if group_complete and ordinary_results:
        exact_average = sum(int(item.get("poeng_gruppespill") or 0) for item in ordinary_results) / len(ordinary_results)
        start_points = _round_half_up(exact_average)

    for result in stilling:
        if result.get("sen_pamelding"):
            if start_points is None:
                result["poeng_start"] = 0
                result["startpoeng_status"] = "venter"
            else:
                result["poeng_start"] = start_points
                result["startpoeng_status"] = "beregnet"
        else:
            result["poeng_start"] = 0
            result["startpoeng_status"] = "ikke_aktuelt"

        result["poeng_totalt"] = sum(
            int(result.get(field) or 0)
            for field in (
                "poeng_gruppespill",
                "poeng_start",
                "poeng_utslagsrunder",
                "poeng_bonus",
                "poeng_helhetsbonus",
                "poeng_turneringsvinner",
            )
        )

    if start_points is not None:
        status = "beregnet"
    elif not group_complete:
        status = "venter_pa_gruppespill"
    else:
        status = "mangler_grunnlag"

    metadata: dict[str, Any] = {
        "status": status,
        "forventet_gruppekamper": EXPECTED_GROUP_MATCHES,
        "gruppekamper_funnet": len(group_matches),
        "gruppekamper_ferdige": finished_count,
        "antall_ordinare_deltakere": len(ordinary_results),
        "antall_senpameldte": late_count,
        "gjennomsnitt_eksakt": round(exact_average, 3) if exact_average is not None else None,
        "startpoeng": start_points,
        "avrunding": "narmeste hele poeng, 0.5 rundes opp",
        "sist_beregnet": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if unfinished:
        metadata["uferdige_gruppekamper"] = unfinished[:20]

    return stilling, metadata
