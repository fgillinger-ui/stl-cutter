# Ändra mått utan att deformera modellen

Ibland är modellen rätt, men ett mått är fel. Skåpet ska vara 550 mm djupt i
stället för 250. Röret ska bli 30 cm längre. Hyllan ska passa i en annan nisch.

Att skala modellen löser inte det. En skalning i djupled gör **allt** djupare:
godstjockleken i gavlarna, skruvhålens djup, fasningarna – och de hål som pekar
åt andra hållet blir ovala. Ett Ø8-hål i en platta som sträcks 2,2 gånger blir
ett 8 × 17,6 mm-avlångt hål. Skruven passar inte längre.

`stl-cutter` gör i stället något annat: den letar upp de partier där modellens
tvärsnitt är **konstant** längs den axel du vill ändra, och skjuter in eller tar
bort material just där. Allt annat lämnas exakt som det var.

## Zonbegreppet

Tänk dig att du skivar modellen som ett bröd, vinkelrätt mot den axel du vill
ändra, en skiva per millimeter. Varje skiva har en form: en fyrkant, en ram, en
ram med två hål, två skilda öar. Där flera skivor i rad har **samma** form
ligger ett *prismatiskt parti* – en zon.

```
        gavel        ihåligt parti (zon)         gavel
    ┌───────────┬───────────────────────────┬───────────┐
    │███████████│███                     ███│███████████│
    │███████████│███                     ███│███████████│
    │███████████│███                     ███│███████████│
    └───────────┴───────────────────────────┴───────────┘
     0        4 mm                       246 mm      250 mm
     tvärsnitt =   tvärsnitt = ram, 4 mm väggar,      tvärsnitt =
     hel platta    exakt likadan hela vägen           hel platta
```

Att förlänga lådan från 250 till 550 mm betyder: klyv den mitt i zonen, flytta
den bortre halvan 300 mm bort och fyll mellanrummet med en 300 mm lång bit av
zonens tvärsnitt. Gavlarna, väggtjockleken och hörnradierna rör sig inte – de
har bara flyttat sig 300 mm ifrån varandra.

Att korta av är samma sak baklänges: två snitt 70 mm isär inne i zonen,
mittstycket kastas, resten fogas ihop.

Ett hål är inte en zon. Ett borrat hål börjar och slutar någonstans, och där
tvärsnittet börjar ändra sig tar zonen slut. Det är därför hålen behåller sitt
avstånd till **sin egen** gavel: de ligger utanför zonen, och zonen är det enda
som ändras.

```
     hål ↓                                              ↓ hål
    ┌────┬──────────────────────────────────────────┬────┐
    │  ○ │            zonen växer här               │ ○  │
    └────┴──────────────────────────────────────────┴────┘
     15 mm                                           15 mm
     ↑ kvar efteråt                     efteråt kvar ↑
```

## Så här kör du

Titta först på var modellen går att sträcka:

```
python -m stl_cutter.cli analyze-spans modell.stl --axis y
```

```
Axel Y: modellen är 250.0 mm (-125.0 … 125.0 mm).
  3 parti(er) att sträcka i, sammanlagt 262.0 mm:
   1. Y -120.5 … 120.5 mm (241.0 mm långt, tvärsnitt 4864 mm²)
   2. Y -124.5 … -114.5 mm (10.0 mm långt, tvärsnitt 24000 mm²)
   3. Y 114.5 … 124.5 mm (10.0 mm långt, tvärsnitt 24000 mm²)
  Att korta av går som mest 256.0 mm totalt (2 mm måste bli kvar i varje parti).
```

Ändra sedan måttet:

```
python -m stl_cutter.cli resize modell.stl --y 550 --out modell_550.stl
python -m stl_cutter.cli resize modell.stl --y 550 --select longest
python -m stl_cutter.cli resize modell.stl --x 300 --z 180
```

Programmet skriver ut exakt var materialet hamnade:

```
Y: 240,0 → 250,0 mm. 10,0 mm fördelat på 5 partier: +2,0 mm vid y=-80,0,
+2,0 mm vid y=-40,0, +2,0 mm vid y=0,0, +2,0 mm vid y=40,0 och +2,0 mm vid y=80,0.
```

Eller ändra måttet och kapa i ett svep – måttändringen sker alltid **före**
snittplaneringen:

```
python -m stl_cutter.cli cut modell.stl --resize-y 550 --printer "Bambu P1S"
```

### Flaggor

| Flagga | Betydelse |
|--------|-----------|
| `--x` / `--y` / `--z` | Önskat mått i mm. Utelämnade mått lämnas orörda. |
| `--select` | `auto` (standard), `longest` eller `distribute`. Se nedan. |
| `--distribute` | Samma sak som `--select distribute`. |
| `--longest` | Samma sak som `--select longest`. |
| `--span N` | Använd zon nummer N ur `analyze-spans`-listan. |
| `--mode scale` | Rak skalning. Deformerar godstjocklek och hål – bara som medvetet val. |
| `--min-span MM` | Kortaste parti som räknas som en zon (3 mm som standard). |
| `--step MM` | Avstånd mellan de provade tvärsnitten (1 mm som standard). |
| `--tol` | Relativ tolerans när två tvärsnitt jämförs (0,005 som standard). |

## `auto` – hur insättningspunkterna väljs

`auto` är standard. Den svarar på två frågor i tur och ordning: *måste
resultatet vara symmetriskt?* och *var sitter modellens upprepade mönster?*

### 1. Är modellen spegelsymmetrisk längs axeln?

Modellen speglas i sitt mittplan och de speglade punkterna mäts mot originalets
yta. Ligger alla närmare än 0,2 mm är modellen symmetrisk längs den axeln
(`detect_mirror_symmetry`).

Var den symmetrisk före måste den vara det efter. Det är inte en smaksak: en
symmetrisk modell där hela tillskottet hamnar på ena sidan ser trasig ut även
när måttet stämmer på tiondelen. Kontrollen körs därför oavsett vilken strategi
som valts – även `longest` och `distribute` avbryts om symmetrin går förlorad.

### 2. Vilka partier bildar mönstret?

Bara de **jämnstora** partierna. Ett parti som är kortare än 75 % av det längsta
räknas som en detalj, inte som en del av mönstret, och lämnas i fred.

```
    stege, sex pinnar                     låda med två hål
 ┌──┬──┬──┬──┬──┬──┬──┬──┬──┬──┐     ┌────┬──────────────────┬────┐
 │  │██│  │██│  │██│  │██│  │██│     │ ○  │                  │ ○  │
 └──┴──┴──┴──┴──┴──┴──┴──┴──┴──┘     └────┴──────────────────┴────┘
   14  29 29 29 29 29     14           10        211          10
   ↑                ↑                   ↑                      ↑
   detalj    mönstret: fem lika        detalj      mönstret: ett enda
             stora mellanrum                       långt parti
```

Stegen får sitt tillskott delat lika på de fem mellanrummen – alla blir lika
stora efteråt, och ändstyckena behåller sina 14 mm. Lådan har bara ett parti i
mönstret, så allt hamnar där, och hålen behåller sitt avstånd till gaveln.

### 3. Var läggs det inom mönstret?

* Är modellen symmetrisk grupperas mönstrets partier i **spegelpar** kring
  mittplanet. Varje par får lika mycket, och delar det lika mellan sig.
  Ett parti som själv ligger över mittplanet bildar en egen grupp.
* Finns bara ett parti i mönstret hamnar hela tillskottet där – centrerat på
  **mittplanet** om partiet ligger över det, annars mitt i partiet.
* Är modellen inte symmetrisk fördelas `delta` proportionellt mot partiernas
  längd, precis som `distribute`. Symmetri uppfinns aldrig: en medvetet
  osymmetrisk modell ska förbli osymmetrisk.

Insättningarna räknas ut i **originalets koordinater innan den första körs**,
och utförs sedan från högsta koordinat till lägsta. Annars hade den första
insättningen förskjutit snittplanen för de partier som stod på tur.

### 4. Rätt tvärsnitt vid varje snitt

Mellanstycket extruderas från tvärsnittet **exakt vid snittplanet**, hämtat med
`trimesh.section` där snittet ligger – inte från ett representantsnitt för hela
partiet. Innan snittet görs kontrolleras att tvärsnittet är oförändrat ±0,5 mm
på båda sidor om planet. Är det inte det ligger planet för nära en detalj, och
det flyttas mot partiets mitt och provas igen. Snittplanet håller alltid minst
2 mm till partiets ändar.

Det är den kontrollen som avgör om materialet fyller igen skarven eller skjuter
ut som en buckla.

## När ska man välja vad?

**`auto` (standard)** enligt ovan. Rätt i nästan alla lägen.

**`longest`** lägger hela ändringen i det längsta partiet. Det är rätt när
modellen har *en* rak sträcka och detaljerna sitter i ändarna: en låda, ett rör,
en profil, en balk – och när du vill att allt ska hamna på ett ställe. Det är
inte längre standard, för på en modell med upprepade detaljer gör det ett enda
mellanrum bredare än de övriga.

**`distribute`** fördelar ändringen proportionellt mot zonernas längd – över
**alla** zoner, även de korta som `auto` skulle ha lämnat i fred.

Observera att hyllplanen själva är korta zoner, och med `--distribute` växer
även de – ett 8 mm hyllplan kan bli 10 mm tjockt. Vill du att bara mellanrummen
ska växa höjer du `--min-span` över hyllplanens tjocklek:

```
python -m stl_cutter.cli resize hylla.stl --z 400 --distribute --min-span 30
```

**`--span N`** är för när du vet bättre än programmet: växa bara nedtill, bara
i den bortre sektionen, bara där kabeln ska dras.

## Vad som kontrolleras

Efter varje måttändring körs sex kontroller, och **ingenting levereras om
någon av dem fallerar**. En trasig mesh som ser rätt ut i ett mått är sämre än
ett tydligt felmeddelande.

1. **Sluten mesh.** Resultatet ska vara watertight med konsekventa
   normalriktningar.
2. **Måttet.** Den nya bounding boxen ska stämma med målet inom 0,1 mm.
3. **Volymen.** Volymen ska ha ändrats med *tvärsnittsarean × delta* summerat
   över insättningarna, inom 1 %. Det är kontrollen som avslöjar en boolean som
   svalt eller lagt till material den inte skulle.
4. **De andra måtten.** De axlar som inte skulle ändras ska vara exakt kvar
   inom 0,1 mm. Ett mellanstycke som extruderas från fel tvärsnitt kan skjuta
   ut i sidled: måttet längs den ändrade axeln stämmer, men modellen har blivit
   bredare utan att något sagt ifrån.
5. **Spegelsymmetrin.** Var modellen spegelsymmetrisk längs axeln före ska den
   vara det efter, inom samma 0,2 mm.
6. **Resten av modellen.** Alla tvärsnitt utanför de ändrade zonerna jämförs
   före och efter. Har något av dem förändrats avbryts måttändringen.

## Vad som inte fungerar

**Modeller utan konstant tvärsnitt.** Ett klot, en organisk form, ett koniskt
hölje – där finns ingen zon att växa i, och programmet vägrar gissa:

> Modellen har inget parti med konstant tvärsnitt längs djupet — måttet kan bara
> ändras genom skalning, vilket förändrar godstjocklek och hål.

Vill du ändå ändra måttet kör du `--mode scale`, och får då en varning om att
hål blir ovala och väggar tjockare. Det är ett medvetet val, aldrig något som
sker i smyg.

**Att korta av mer än materialet räcker till.** Varje zon måste behålla 2 mm.
Räcker inte den längsta zonen provas nästa, sedan flera tillsammans; först
därefter kommer felmeddelandet, och det säger hur många millimeter som faktiskt
gick att ta bort.

**Upprepade mönster multipliceras inte.** Det här är den viktigaste kända
begränsningen. En hylla med fyra jämnt fördelade hyllplan som görs 30 % högre
får fyra hyllplan med 30 % större mellanrum – inte fem hyllplan med samma
mellanrum som förut. `auto` och `distribute` bevarar *proportioner*, de lägger
inte till ett femte hyllplan.

Samma sak gäller skruvhål längs en list, kylflänsar, perforeringar och gängor:
antalet element är en egenskap hos modellen, inte något programmet räknar om.
Att känna igen ett upprepat mönster och multiplicera det är ett rimligt
nästa steg, men ligger utanför den här fasen.

**Vinklade och vridna partier.** Zonen definieras längs en av huvudaxlarna.
En modell som är prismatisk längs en lutande riktning känns inte igen; rotera
den först.

**Mycket fina detaljer nära zonens gräns.** Zonerna hittas genom att sampla
tvärsnitt var millimeter (`--step`). En detalj som är kortare än så kan hamna
mellan två prov. Sänk `--step` till 0,5 eller 0,25 mm på små modeller – det tar
längre tid men ser mer.

## Rapporten

`resize_report.json` skrivs bredvid resultatfilen och innehåller ursprungsmått,
målmått, alla hittade zoner, vilka som användes och med hur mycket, den
faktiska volymförändringen mot den väntade, samt varningar.

```json
{
  "original_extents_mm": [250.0, 250.0, 200.0],
  "target_extents_mm": [null, 550.0, null],
  "result_extents_mm": [250.0, 550.0, 200.0],
  "axes": [
    {
      "axis": "Y",
      "from_mm": 250.0, "to_mm": 550.0, "delta_mm": 300.0,
      "mode": "preserve", "span_selection": "auto",
      "resolved_selection": "symmetric-centered",
      "mirror_symmetric_before": true, "mirror_symmetric_after": true,
      "spans_found": [ { "start_mm": -120.5, "end_mm": 120.5, "length_mm": 241.0,
                         "section_area_mm2": 4864.0 } ],
      "spans_used": [ { "start_mm": -120.5, "end_mm": 120.5,
                        "applied_delta_mm": 300.0, "cut_at_mm": 0.0,
                        "cut_section_area_mm2": 4864.0 } ],
      "placement": "Y: 250,0 → 550,0 mm. 300,0 mm tillagt vid y=0,0.",
      "expected_volume_change_mm3": 1459200.0,
      "actual_volume_change_mm3": 1459200.0,
      "warnings": []
    }
  ],
  "warnings": []
}
```
