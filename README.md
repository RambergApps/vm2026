# VM 2026 Tippekonkurranse

Intern tippekonkurranse for VM 2026 (USA/Canada/Mexico, 11. juni – 19. juli).

## Sider

| Side | URL | Beskrivelse |
|------|-----|-------------|
| Leaderboard | `https://DITT-BRUKERNAVN.github.io/vm2026/` | Live stillingstabell |
| Tipp gruppespill | `https://DITT-BRUKERNAVN.github.io/vm2026/tippe/` | Innlevering gruppespill |
| Tipp utslagsrunder | `https://DITT-BRUKERNAVN.github.io/vm2026/tippe/utslagsrunder.html` | Innlevering utslagsrunder |

## Mappestruktur

```text
vm2026/
├── index.html                  ← Leaderboard (forsiden)
├── tippe/
│   ├── index.html              ← Tipping-app gruppespill
│   └── utslagsrunder.html      ← Tipping-app utslagsrunder
├── data/
│   ├── data.js                 ← Genereres av GitHub Actions
│   ├── deltakere.json          ← Genereres av GitHub Actions
│   └── status.json             ← Hvilke runder som er åpne
├── tippinger/
│   ├── gruppespill/            ← JSON-filer fra deltakere
│   ├── r32/
│   ├── r16/
│   ├── qf/
│   ├── sf/
│   └── final/
├── scripts/
│   └── poengregning.py         ← Kjøres av GitHub Actions
├── regler.md                   ← Poengregler
└── .github/
    └── workflows/
        └── oppdater.yml        ← GitHub Actions konfig
```

## Poengregler

Alle kamper poengberegnes på **resultat etter ordinær tid / 90 minutter**. Ekstraomganger, straffer og hvilket lag som går videre i utslagsrundene gir ikke kamp-poeng.

| Type | Riktig utfall etter 90 min | Eksakt resultat etter 90 min |
|------|-----------------------------|------------------------------|
| Gruppespill | 2p | +4p |
| Runde av 32 | 3p | +4p |
| Runde av 16 | 4p | +4p |
| Kvartfinale | 5p | +4p |
| Semifinale | 6p | +4p |
| Finale | 7p | +4p |
| Turneringsvinner (tippa ved start) | 70p | — |

**Maks totalt: 601 poeng**

## Vedlikehold

GitHub Actions kjører automatisk hver time og oppdaterer leaderboard.
Du trenger ikke gjøre noe manuelt under turneringen.

### Åpne utslagsrunder manuelt

Oppdater `data/status.json` og sett `"aapen": true` for aktuell runde.
GitHub Actions vil oppdage endringen og oppdatere appen automatisk.
