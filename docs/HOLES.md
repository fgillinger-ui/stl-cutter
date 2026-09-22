# Hål

Ett hål är en solid som dras bort ur modellen: en cylinder, och för ett
skruvhål dessutom en försänkning i ytan så att skallen går i jämnt. Samma
booleanmotor som fogarna använder (`manifold3d`) gör jobbet.

Hålen borras **före** snitten. Två skäl:

1. Ett hål kan hamna tvärs över ett snitt, och ska då finnas i båda delarna.
2. Snittplaneringen ska se modellen som den faktiskt blir — ett hål ändrar
   tvärsnittsarean, och det är den analysen fogvalet bygger på.

Ordningen i hela kedjan är alltså: `ladda → ändra mått → borra → planera snitt
→ kapa → exportera`.

## I gränssnittet

Fliken **1c. Hål**.

### Peka i vyn

Klicka **Placera hål i vyn** och peka på modellen. Hålet borras vinkelrätt in i
den yta du pekar på — det är vad man menar när man pekar på en modell, också
när ytan lutar. Slå av knappen för att vrida modellen igen.

### Eller skriv in siffrorna

**Lägg till hål** lägger ett hål mitt på ovansidan. Sedan går X, Y, Z, diameter
och djup att skriva in i tabellen, och riktningen att välja i rullgardinen.
Decimalkomma fungerar lika bra som punkt.

Ett hål som placerats med ett klick på en lutande yta har en riktning som inte
är någon av axlarna. Den står då som **Från ytan** i rullgardinen och ligger
kvar tills du väljer något annat.

### Vad hålet ska bli

| Val | Vad det gör |
|-----|-------------|
| **Skruv** | M3–M6. Skruven bestämmer diametern (fri passage enligt ISO 273) och försänkningens mått. |
| **Inget – eget mått** | Du anger diametern själv. |
| **Försänkt skalle** | Konisk försänkning, 90°. Skallen går i jämnt med ytan. |
| **Planförsänkt (insex)** | Cylindrisk ficka för en insexskalle, som sjunker under ytan. |
| **Genomgående** | Hålet går rakt igenom, oavsett hur tjockt godset är. |
| **Djup** | Blindhål. Djupet mäts från ytan och inåt. |

De röda pinnarna i 3D-vyn visar var hålen hamnar och **åt vilket håll de går** —
halva frågan när man pekar på en lutande yta.

### Borra

**Borra hålen** tar bort materialet. Modellen ersätts, och **Ångra borrning**
lägger tillbaka den som den var. En plan som gjordes före borrningen kastas:
den planerades för en annan modell.

Hål som ännu **inte** är borrade sparas i projektfilen. Borrade hål sitter i
modellen, och modellen ligger med i projektfilen — så båda kommer tillbaka.

## På kommandoraden

```bash
# Två M4-hål med försänkt skalle, rakt ned genom en platta
stl-cutter drill platta.stl --hole 10,0,6 --hole=-10,0,6 --screw M4

# Blindhål Ø 6 mm, 12 mm djupt, in från sidan
stl-cutter drill hylla.stl --hole 0,-30,20 --direction +y --diameter 6 --depth 12
```

Punkten är där hålet **börjar**, i modellens koordinater. Negativa tal skrivs
med likhetstecken (`--hole=-10,0,6`) — annars tror kommandotolken att det är en
ny flagga.

## Måtten

Skruvmåtten är avskrivna standardmått, inte uträknade:

| Skruv | Fri passage | Försänkt skalle | Konens djup | Insexficka |
|-------|-------------|-----------------|-------------|------------|
| M3 | 3,4 mm | 6,0 mm | 1,3 mm | 5,5 × 3,0 mm |
| M4 | 4,5 mm | 8,0 mm | 1,75 mm | 7,0 × 4,0 mm |
| M5 | 5,5 mm | 10,0 mm | 2,25 mm | 8,5 × 5,0 mm |
| M6 | 6,6 mm | 12,0 mm | 2,7 mm | 10,0 × 6,0 mm |

Fri passage är medelserien i ISO 273, den försänkta skallen följer ISO 10642 och
insexfickan ISO 4762. Konens djup följer av de två diametrarna och 90°-vinkeln.

**Utskrivet blir ett hål alltid något trängre** än ritat: FDM lägger materialet
en aning innanför konturen och plasten krymper. Ett M4-hål på 4,5 mm brukar
behöva en lätt vridning med borren, eller 0,2 mm extra i diametern. Prova på en
provbit innan du skriver ut hela hyllan.

## Vad programmet inte gör

Det kontrollerar **inte** att hålet lämnar tillräckligt med gods omkring sig.
Hur nära kanten ett hål får sitta beror på material, last, skruv och
utskriftsinställningar, och ett tal som ser beräknat ut men inte är det är sämre
än inget tal alls — samma skäl som i [LOAD.md](LOAD.md).

Däremot sägs det rakt ut när ett hål **inte går in i materialet**: punkten ligger
utanför modellen, eller riktningen pekar ut ur den i stället för in. Felet namnger
vilket hål det gäller och med vilka mått, så att man vet vilket av fem som är fel.

## Felsökning

**"Hål 2 … går inte in i modellen."** Punkten ligger utanför godset eller
riktningen är vänd. Kontrollera Z-värdet: punkten ska ligga *på ytan* där hålet
börjar, inte i modellens mitt.

**"Ø 0.4 mm är för litet för att skriva ut."** Under 1 mm blir hålet igenmurat
av materialets egen bredd. Vill du ha ett litet hål: skriv ut det på 1 mm och
borra upp efteråt.

**Modellen är inte sluten efter borrningen.** Originalet hade troligen redan hål
i ytan, och booleanen ärvde dem. Programmet säger till; kontrollera delen i
slicern, och laga originalmodellen om slicern klagar.
