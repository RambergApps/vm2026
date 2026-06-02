# VM 2026 Tippekonkurranse

Intern tippekonkurranse for VM 2026 (USA/Canada/Mexico, 11. juni – 19. juli).

## Sider

| Side | URL | Beskrivelse |
|------|-----|-------------|
| Leaderboard | `https://DITT-BRUKERNAVN.github.io/vm2026/` | Live stillingstabell |
| Tipp gruppespill | `https://DITT-BRUKERNAVN.github.io/vm2026/tippe/` | Innlevering gruppespill |
| Tipp utslagsrunder | `https://DITT-BRUKERNAVN.github.io/vm2026/tippe/utslagsrunder.html` | Innlevering utslagsrunder |

## Mappestruktur

```
vm2026/
├── index.html                  ← Leaderboard (forsiden)
├── tippe/
│   ├── index.html              ← Tipping-app gruppespill
│   └── utslagsrunder.html      ← Tipping-app utslagsrunder
├── data/
│   ├── data.js                 ← Genereres av GitHub Actions
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

| Type | Riktig vinner | Eksakt resultat |
|------|--------------|-----------------|
| Gruppespill | 2p | +2p |
| Runde av 32 | 3p | +2p |
| Runde av 16 | 4p | +2p |
| Kvartfinale | 5p | +2p |
| Semifinale | 6p | +2p |
| Finale | 7p | +2p |
| Turneringsvinner (tippa ved start) | 35p | — |

**Maks totalt: 408 poeng**

## Vedlikehold

GitHub Actions kjører automatisk hver time og oppdaterer leaderboard.
Du trenger ikke gjøre noe manuelt under turneringen.

### Åpne utslagsrunder manuelt

Oppdater `data/status.json` og sett `"aapen": true` for aktuell runde.
GitHub Actions vil oppdage endringen og oppdatere appen automatisk.
