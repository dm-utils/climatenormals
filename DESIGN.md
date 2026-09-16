# Langjarige weersnormalen (climate-normals): databron, cache en 3 ontsluitingskanalen

## Context

Doel: een weersvoorspelling (startend met temperatuur, neerslag volgt snel) voor een
opgegeven locatie en datum, gebaseerd op een gewogen gemiddelde van drie historische
vensters (bijv. 40% laatste ~100 jaar, 30% laatste 30 jaar, 30% laatste 10 jaar —
exacte weging nog aan te scherpen). Eerder deze sessie is uitgezocht:

- **Databron voor het lange venster**: NOAA GHCN-Daily. Op basis van het echte
  stationsbestand (132.437 stations wereldwijd) is een lijst van **26 landen**
  vastgesteld met ≥1 station met ≥80 jaar TMAX-historie, elk met een voorgestelde
  regionale granulariteit (state/oblast tot landelijk-met-1-punt). Zie de conversatie
  voor de volledige tabel; dit is ons startpunt, niet in dit bestand herhaald.
- **Databron voor de recente vensters (30j/10j)**: een dichter, wereldwijd uniform
  netwerk i.p.v. de schaarse ≥80-jaar-stations — Open-Meteo's Historical Weather API
  (ERA5-reanalyse, 1940-heden, ~25 km grid, geen gaten, geen key nodig) bevraagd op
  het centroid van elke regio.
- **Opslag/hosting**: geen externe database nodig. Geschatte omvang (280-360 regio's ×
  366 dagen × ~5-6 velden) is ~10-100 MB — ruim binnen Vercel Hobby (100 GB
  data-overdracht/maand, 1 GB Blob Storage gratis). Wordt als statisch JSON-bestand
  meegebouwd, periodiek (jaarlijks) ververst via een scheduled job — geen live
  API-calls naar externe bronnen per bezoeker.
- **Ontsluiting**: gebruiker wil dit via **drie kanalen** — een pagina op
  datamodder.com/utils, een publieke API, én een Home Assistant-integratie (zelfde
  patroon als de eerder gebouwde Travel Forecast-integratie). Dat betekent: bouw één
  gedeelde API die website én HA-integratie allebei aanroepen, in plaats van de
  logica te dupliceren.

## Architectuur

**Nieuwe, losstaande repo: `dm-utils/climatenormals`** (matcht de naamgeving van
`formatsql`/`filetools`/`travelforecast`). Bevat de databuild-pipeline én de API die
als hosted service draait — dit is het gedeelde fundament waar zowel de website als de
HA-integratie straks op leunen.

### 1. Regio's en grenzen
- Gebruik **geoBoundaries** (CC-BY 4.0, ADM0/ADM1/ADM2, actief onderhouden) voor de
  regiogrenzen — niet GADM (herdistributie-restricties). Alleen de grenzen van onze
  26 landen ophalen en verkleinen tot een lokaal GeoJSON-bestand (`regions.geojson`),
  niet de hele wereld.
- Elke regio krijgt een stabiel `region_id` (bijv. `NL-noord`, `DE-BY` voor Beieren,
  `US-CA` voor California, `IT` voor landelijk Italië).
- Adres/coördinaat → regio: geocoderen (zelfde patroon als Travel Forecast, een
  geocoding-API) + point-in-polygon tegen `regions.geojson`.

### 2. Databuild-pipeline (Python, matcht de eerdere GHCN-analyse-aanpak)
Draait **niet** als publieke serverless function maar als apart, periodiek
(jaarlijks) script — bijv. via een GitHub Action op een schema — dat het cache-bestand
genereert en commit. Stappen per regio:
1. Koppel de regio aan de specifieke kwalificerende GHCN-station(s) (uit de eerder
   gegenereerde `ghcnd-inventory.txt`/`ghcnd-stations.txt`-analyse) voor het lange
   venster.
2. Haal de volledige dagelijkse TMAX/TMIN-reeks van dat station op (NOAA's
   per-station data, niet alleen de inventory-metadata).
3. Haal ERA5-daggegevens op via Open-Meteo voor het regio-centroid, voor de 30- en
   10-jaars vensters.
4. Bereken per dag-van-het-jaar (met een ±7-dagen-venster rond die datum, voor meer
   datapunten per gemiddelde) de drie deelgemiddeldes + het gewogen eindresultaat.
   Val netjes terug als een regio minder dan het nominale aantal jaren heeft (bijv.
   Mexico: 97 jaar i.p.v. 100) — gebruik het werkelijk beschikbare aantal jaren.
5. Schrijf naar `normals.json`, met een schema dat ruimte laat voor neerslag later
   zonder breaking change:
   ```json
   {
     "NL-noord": {
       "45": { "temp": { "p100": 8.2, "p30": 8.9, "p10": 9.3, "blend": 8.7 } }
     }
   }
   ```
   (dag-van-het-jaar als sleutel; `precip` komt er later als broertje van `temp` bij.)

### 3. API-laag
Kleine serverless function(s) in dezelfde repo, gehost op Vercel:
- `GET /api/normal?lat=&lon=&date=` → geocode/point-in-polygon → regio-opzoeking in
  `normals.json` → JSON-respons. Puur een lookup, geen live externe calls, dus ruim
  binnen de gratis grenzen.
- Dit endpoint ís de "publieke API" die de gebruiker als los kanaal wil aanbieden.

### 4. Website (later, apart stukje werk)
Nieuwe pagina `datamodder.com/utils/climate-forecast`, zelfde patroon als de
bestaande utils-pagina's (zie `src/app/[lang]/utils/travel-forecast/page.tsx` als
voorbeeld), die het `/api/normal`-endpoint aanroept.

### 5. Home Assistant-integratie (later, apart stukje werk)
Nieuwe repo `dm-utils/climateforecast-ha` (of vergelijkbare naam), **letterlijk
hetzelfde patroon** als `dm-utils/travelforecast`: config_flow voor
adres/coördinaten, coordinator die periodiek `/api/normal` bevraagt i.p.v.
rechtstreeks TomTom, sensor-entiteiten. Grotendeels hergebruik van de net gebouwde
structuur.

## Eerste concrete stap (waar we nu mee beginnen)

Niet meteen alle 26 landen — eerst **valideren met Nederland + Duitsland** (de
voorbeelden die je zelf al noemde), zelfde aanpak als het TomTom-testscriptje bij
Travel Forecast:
1. `dm-utils/climatenormals`-repo aanmaken (lokaal + GitHub, MIT-licentie, commit-msg
   hook zoals bij de andere repo's).
2. Regiogrenzen voor NL (noord/midden/zuid — exacte indeling bepalen a.d.h.v. waar de
   5 kwalificerende Nederlandse stations echt liggen) en Duitsland (16 Bundesländer)
   ophalen via geoBoundaries.
3. Testscript: voor één NL-regio en één DE-regio de volledige pipeline doorlopen
   (station koppelen → data ophalen → 3 vensters berekenen → gewogen resultaat) en de
   uitkomst met de hand beoordelen op plausibiliteit (bijv. decembergemiddelde moet
   kouder zijn dan julig-gemiddelde — sanity check zoals we ook bij Travel Forecast
   deden met de spits-vs-nacht-vergelijking).
4. Pas na die validatie opschalen naar alle 26 landen en de API/website/HA-laag
   bouwen.

## Verificatie
- Pipeline-output handmatig controleren op plausibiliteit (seizoenspatroon, geen
  malle uitschieters) vóór opschaling.
- `normals.json`-bestandsgrootte meten na de NL+DE-pilot en extrapoleren naar 26
  landen, ter bevestiging van de eerder gemaakte 10-100 MB-schatting.
- Na de API-laag: een paar losse `curl`-testaanvragen tegen `/api/normal` met bekende
  coördinaten (net als bij de TomTom-validatie).
