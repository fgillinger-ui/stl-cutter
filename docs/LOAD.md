# Delar som ska bära last

Ska en hylla bära något spelar det stor roll *var* den kapas. En fog är alltid
svagare än helt gods, och den ska inte hamna på den punkt som är hårdast
belastad. Det är vad den här funktionen gör — och ingenting mer än så.

## Vad programmet räknar ut

Momentkurvans **form** längs modellen, normaliserad till 0–1, och den används
som en straffpoäng bland de andra i snittplaneringen.

| Upphängning | Böjmoment | Störst | Noll |
|---|---|---|---|
| Utkragad från vägg | *M(x) = w(L−x)²/2* | vid infästningen | ytterst |
| Uppburen i båda ändar | *M(x) = w·x(L−x)/2* | mitt emellan upplagen | i ändarna |

Straffet läggs bara på snitt som ligger **längs lastens spännaxel**. Ett snitt
tvärs lasten böjs inte isär av den, och lämnas därför i fred.

Vikten i kilo ändrar inte var snittet hamnar — kurvans form är densamma. Den
finns med för att den styr utskriftsinställningarna, och för att en siffra att
skriva in är det som gör att man tänker igenom vad hyllan faktiskt ska bära.

## Vad programmet inte räknar ut

**Hur mycket hyllan håller.** Det är med flit.

Ett tidigt försök gjorde just det: yttröghetsmomentet ur tvärsnittspolygonen
med Greens formel, σ = *Mc/I*, ett svar i MPa. På den verkliga hyllan
(250 × 270 × 182 mm, 5 kg utkragat) gav det:

```
 y (mm)   area mm2        I mm4    M Nm   spänning MPa
     35       1581     22383441    5.36           0.03
    143       3375     20938747    1.66           0.01
    251        697      9876677    0.07           0.00
```

0,03 MPa mot PLA:s cirka 50 MPa ser ut som en betryggande marginal. Det är
fel. Formeln behandlar hela tvärsnittet som en sammanhängande balk 182 mm hög,
medan lasten i verkligheten vilar på slatsplanet och stolparna bara finns
längst bak. Den riktiga spänningen är hundratals gånger högre.

Det är den farliga felmoden: **en modell som är lite fel ger ett lugnande
tal**, och det är det talet man hänger upp sin NAS på. En strukturmodell av en
godtycklig mesh är ett eget problem, och FDM varierar dessutom ±50 % med
skrivare, material, kylning och skrivhastighet.

Rangordningen är däremot robust — momentkurvans form beror på upphängningen
och spännvidden, inte på tvärsnittets detaljer. Den säger inte hur mycket som
håller, men den säger var man ska undvika att kapa, och det är frågan
planeraren ställer.

**Utskriftstid** räknas inte heller ut. Det gör slicern, och den gör det med
kännedom om din faktiska maskin.

## Gissningen om upphängning

Två trubbiga regler, valda för att gå att förklara i en mening var:

* **Spännaxeln** är den längsta vågräta axeln. En hylla sticker ut från väggen
  eller spänner mellan två gavlar; åt det hållet är den längst.
* **Infästningen** sitter i den ände som har mest material i tvärsnittet, mätt
  som medelvärdet över den yttersta 15 procenten. Där sitter gavlarna,
  stolparna eller bakstycket som bär upp resten.

Gissningen **visas alltid med sitt skäl**, och skiljer sig ändarna mindre än
30 procent står det i klartext att det är en ren gissning. Fel upphängning
vänder momentkurvan helt — ett snitt som skulle hamna ytterst hamnar innerst —
och det är inget programmet får avgöra i tysthet. Rätta den med
`--support`, `--load-axis` och `--load-end`, eller med rullgardinerna i rutan
*3b. Belastning*.

## Utskriftsinställningarna

De är **tumregler**, inte beräkningar, och de följer av hur FDM går sönder i
böjning: sprickan går mellan lagren, och materialet som bär sitter i skalet
längst från neutrallagret.

| Inställning | Värde | Varför |
|---|---|---|
| Väggar | 4–5 | Väggarna bär böjningen. Från 2 till 5 väggar ger mer än lika mycket extra fyllnad, och kostar mindre tid. |
| Fyllnad | 25 %, gyroid | Över ~30 % ger varje procent lite styrka och mycket tid. Gyroid håller lika bra åt alla håll. |
| Topp/botten | 5 lager | Samma skäl som väggarna. |
| Lagerhöjd | ~65 % av munstycket | Både snabbare och något starkare mellan lagren — färre fogar att spricka i. |
| Temperatur | +5…+10 °C | Lagerhäftningen är den svaga riktningen. Den enda inställningen som är gratis i tid. |
| Fläkt | 30–50 % | Snabb kylning ger fina detaljer men svagare lagerfogar. |
| Orientering | Delen liggande | Lagren ska ligga längs delen, inte tvärs. Kryssrutan *Vänd delarna platt* gör det automatiskt. |

Vill du korta tiden: sänk fyllnaden och höj lagerhöjden. Spara inte in på
väggarna eller temperaturen — det är de som bär.

## Profil till slicern

Inställningarna går att få som en fil slicern läser, i stället för att knappas
in. OrcaSlicer och det som bygger på den — FlashPrint för Flashforge Creator 5,
Bambu Studio, Qidi Studio — läser profiler som JSON:

```bash
stl-cutter profile --load-kg 5 --base-profile "0.20mm Standard @FF C5" --out ./ut
```

I gränssnittet: **Spara slicerprofil…** i rutan *3b. Belastning*. Importera
sedan med *Arkiv → Importera → Importera konfiguration*.

Profilen sätter **bara** de sju inställningar som har med hållfasthet att göra
och ärver allt annat — hastigheter, accelerationer, stöd, primtorn — från den
profil du redan använder. Därför måste du ange vad den heter: det som står i
slicerns rullgardin. En fristående profil hade krävt att programmet hittade på
de övriga hundra värdena, och en profil som ser komplett ut men har gissade
hastigheter är sämre än ingen profil alls.

Temperatur och fläkt hör till **filamentet**, inte processen, och skrivs bara
om du säger vad du kör:

```bash
stl-cutter profile --load-kg 5 --base-profile "0.20mm Standard @FF C5" \
    --filament-base "Flashforge HS PETG @FF C5" --filament-temp 235 --out ./ut
```

Skälet är detsamma som ovan: rådet är "+5 till +10 °C över det normala", och
vad som är normalt beror på om det är PLA (215), PETG (235) eller ASA (255).
Utan den uppgiften skrivs ingen filamentprofil, och det står varför.

## Marginal

Ska lasten vara stor: provbelasta, med marginal, innan något dyrt ställs på
hyllan. Programmet har flyttat snittet till ett bättre ställe — det har inte
lovat att konstruktionen håller.
