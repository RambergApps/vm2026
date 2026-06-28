#!/usr/bin/env python3
"""
Genererer trygg, offentlig innleveringsstatus for utslagstipping.

Formål:
- index.html kan vise om innlogget deltaker mangler tips i nåværende utslagsrunde.
- Filen avslører ikke score/tips, kun hvilke kamp-ID-er og bonusfelt som er levert.

Leser:
  tippinger/**/*.json

Skriver:
  data/innleveringsstatus.json

Merk:
- Gruppespillfiler hoppes over stille. De er ikke relevante for popupen.
- Utslagstips kan ligge i mapper som tippinger/r32/, tippinger/r16/ osv.,
  eller ha meta.runde/payload.runde satt til en støttet utslagsrunde.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TIPPINGER_DIR = REPO_ROOT / "tippinger"
DATA_DIR = REPO_ROOT / "data"
OUT_FILE = DATA_DIR / "innleveringsstatus.json"

RUNDER = {"r32", "r16", "qf", "sf", "final"}
GRUPPESPILL_NAVN = {"gruppe", "grupper", "gruppespill", "group", "groupstage", "group_stage"}
HELHETSBONUS_FELT = ("flest_maal_lag", "totale_maal_utslag", "golden_boot")


def lag_deltaker_id(navn: str) -> str:
    s = (navn or "").lower().strip()
    s = (
        s.replace("æ", "ae")
        .replace("ø", "oe")
        .replace("å", "aa")
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
    )
    s = re.sub(r"[^a-z0-9]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "ukjent"


def les_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        print(f"ADVARSEL: Hopper over ugyldig JSON {path}: {exc}")
        return None


def normaliser_rundenavn(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def runde_fra_fil(path: Path, payload: dict[str, Any]) -> str | None:
    """
    Returnerer støttet utslagsrunde, eller None hvis filen ikke er en utslagstipping.
    Gruppespillfiler er forventet i repoet og skal ikke gi advarsel.
    """
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    kandidater = [
        normaliser_rundenavn(meta.get("runde")),
        normaliser_rundenavn(payload.get("runde")),
    ]
    kandidater.extend(normaliser_rundenavn(part) for part in reversed(path.parts))

    for verdi in kandidater:
        if verdi in RUNDER:
            return verdi
        if verdi in GRUPPESPILL_NAVN:
            return None
    return None


def har_verdi(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def tom_deltaker(navn: str, deltaker_type: str) -> dict[str, Any]:
    return {
        "navn": navn,
        "deltaker_type": deltaker_type or "ordinaer",
        "runder": {},
    }


def tom_runde() -> dict[str, Any]:
    return {
        "kamper": set(),
        "bonus": False,
        "bonus_id": None,
        "helhetsbonus": {},
    }


def normaliser_for_json(data: dict[str, Any]) -> dict[str, Any]:
    deltakere_out: dict[str, Any] = {}
    for deltaker_id, deltaker in sorted(data.items(), key=lambda x: (x[1].get("navn", ""), x[0])):
        runder_out: dict[str, Any] = {}
        for runde, runde_data in sorted(deltaker.get("runder", {}).items()):
            helhet = {
                felt: True
                for felt in HELHETSBONUS_FELT
                if bool(runde_data.get("helhetsbonus", {}).get(felt))
            }
            runde_out: dict[str, Any] = {
                "kamper": sorted(str(k) for k in runde_data.get("kamper", set()) if str(k).strip()),
                "bonus": bool(runde_data.get("bonus")),
            }
            if runde_data.get("bonus_id"):
                runde_out["bonus_id"] = runde_data.get("bonus_id")
            if helhet:
                runde_out["helhetsbonus"] = helhet
            runder_out[runde] = runde_out

        deltakere_out[deltaker_id] = {
            "navn": deltaker.get("navn") or deltaker_id,
            "deltaker_type": deltaker.get("deltaker_type") or "ordinaer",
            "runder": runder_out,
        }

    return {
        "format": "innleveringsstatus_v1",
        "sist_oppdatert": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deltakere": deltakere_out,
    }


def bygg_innleveringsstatus() -> dict[str, Any]:
    deltakere: dict[str, Any] = {}

    if not TIPPINGER_DIR.exists():
        print(f"ADVARSEL: Fant ikke {TIPPINGER_DIR}")
        return normaliser_for_json(deltakere)

    filer = sorted(TIPPINGER_DIR.rglob("*.json"))
    print(f"Leser {len(filer)} tippingfil(er) fra {TIPPINGER_DIR}")

    hoppet_gruppespill = 0
    hoppet_ukjent = 0
    behandlet = 0

    for path in filer:
        payload = les_json(path)
        if not payload:
            continue

        runde = runde_fra_fil(path, payload)
        if runde not in RUNDER:
            # Ikke støy på gamle/ordinære gruppespillfiler. De er ikke relevante her.
            path_parts = {normaliser_rundenavn(part) for part in path.parts}
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            explicit = {normaliser_rundenavn(meta.get("runde")), normaliser_rundenavn(payload.get("runde"))}
            if path_parts & GRUPPESPILL_NAVN or explicit & GRUPPESPILL_NAVN or not explicit - {""}:
                hoppet_gruppespill += 1
            else:
                hoppet_ukjent += 1
                print(f"ADVARSEL: Hopper over fil uten støttet utslagsrunde: {path}")
            continue

        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        navn = str(meta.get("navn") or payload.get("navn") or "").strip()
        deltaker_id = str(meta.get("deltaker_id") or "").strip() or lag_deltaker_id(navn)
        if not deltaker_id or deltaker_id == "ukjent":
            print(f"ADVARSEL: Hopper over utslagsfil uten deltaker/navn: {path}")
            continue

        deltaker_type = str(meta.get("deltaker_type") or "ordinaer").strip() or "ordinaer"
        deltaker = deltakere.setdefault(deltaker_id, tom_deltaker(navn or deltaker_id, deltaker_type))
        if navn:
            deltaker["navn"] = navn
        if deltaker_type:
            deltaker["deltaker_type"] = deltaker_type

        runde_data = deltaker["runder"].setdefault(runde, tom_runde())

        tippinger = payload.get("tippinger") if isinstance(payload.get("tippinger"), list) else []
        for tips in tippinger:
            if not isinstance(tips, dict):
                continue
            kamp_id = str(tips.get("kamp_id") or tips.get("id") or "").strip()
            if kamp_id:
                runde_data["kamper"].add(kamp_id)

        bonus = payload.get("bonus") if isinstance(payload.get("bonus"), dict) else None
        if bonus and har_verdi(bonus.get("svar")):
            runde_data["bonus"] = True
            if bonus.get("id"):
                runde_data["bonus_id"] = str(bonus.get("id"))

        helhet = payload.get("helhetsbonus") if isinstance(payload.get("helhetsbonus"), dict) else {}
        for felt in HELHETSBONUS_FELT:
            if har_verdi(helhet.get(felt)):
                # Helhetsbonus gjelder hele utslagsfasen, men fristen er R32.
                r32_data = deltaker["runder"].setdefault("r32", tom_runde())
                r32_data.setdefault("helhetsbonus", {})[felt] = True

        behandlet += 1

    print(f"  → Behandlet {behandlet} utslagsfil(er)")
    if hoppet_gruppespill:
        print(f"  → Hoppet over {hoppet_gruppespill} gruppespillfil(er)")
    if hoppet_ukjent:
        print(f"  → Hoppet over {hoppet_ukjent} fil(er) uten støttet utslagsrunde")

    return normaliser_for_json(deltakere)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = bygg_innleveringsstatus()
    OUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    antall = len(output.get("deltakere", {}))
    print(f"Skrev {OUT_FILE} med {antall} deltaker(e)")


if __name__ == "__main__":
    main()
