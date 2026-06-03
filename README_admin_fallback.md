# VM 2026 — Admin for manuell kamp/resultat-fallback

Dette settet legger til en adminflyt der OpenFootball fortsatt er master, men der manuelle kamper/resultater kan brukes midlertidig når OpenFootball mangler kamp eller score.

## Filer som skal inn i VM2026-repoet

```text
admin/resultater.html
scripts/poengregning.py
data/manuelle-kamper.json
data/mangler-resultater.json
.github/workflows/motta-resultat.yml
.github/workflows/oppdater.yml
.github/workflows/motta-tipping.yml
```

## Fil som skal inn i Vercel submit-repoet

```text
api/submit-resultat.js
```

I Vercel må du legge til environment variable:

```text
ADMIN_TOKEN=<din admin-kode>
```

Eksisterende variabler beholdes:

```text
GITHUB_TOKEN
GITHUB_OWNER
GITHUB_REPO
```

## Prioritet i poengregning

1. Hvis OpenFootball har ferdig resultat, brukes OpenFootball.
2. Hvis OpenFootball mangler score, men manuell fallback finnes, brukes manuell score.
3. Hvis OpenFootball mangler hele kampen, men manuell kamp finnes, brukes manuell kamp.
4. Når OpenFootball senere får resultat på samme kamp_id, ignoreres manuell fallback automatisk.

## Admin-side

Publiseres som:

```text
https://rambergapps.github.io/vm2026/admin/resultater.html
```

Funksjoner:

- Legg inn enkeltkamp.
- Legg inn resultat etter 90 minutter.
- Bulk-importer gruppespillkamper fra CSV.
- Slett manuell fallback.
- Se kamper som mangler resultat.
- Se manuelle kamper/resultater.

## CSV-eksempel

```csv
gruppe,dato,hjemmelag,bortelag
A,2026-06-11,Mexico,South Africa
A,2026-06-11,South Korea,Czech Republic
B,2026-06-12,Canada,Playoff winner
```

Med resultater:

```csv
gruppe,dato,hjemmelag,bortelag,hjemme,borte,ferdig
A,2026-06-11,Mexico,South Africa,2,1,true
```
