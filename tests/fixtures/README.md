# Testmodeller

All testgeometri byggs normalt **i koden** (se `tests/conftest.py` och
`tests/test_resize.py`). Inga binära filer i repot: de blir stora, går inte att
granska i en diff och säger ingenting om varför de ser ut som de gör.

Undantaget är regressionsfall — en verklig modell där något faktiskt gick fel.
Lägg en sådan fil här, under 5 MB, så plockar `tests/test_fixtures.py` upp den
automatiskt. Saknas filen hoppas testet över, så CI:n går igenom ändå.

## Väntade filer

| Fil | Varför |
|-----|--------|
| `ds nas_ds nas_Body1.3mf` | Modellen där Y 240 → 250 mm lade hela tillskottet på ett ställe och panelen visade 230 × 240 × 182 mm medan måttsektionen visade 270 × 250 × 182 mm. |
