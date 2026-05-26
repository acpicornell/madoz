# Madoz ben fet — notes inicials

> **Nota (2026-05-26)**: aquestes notes són la motivació original del
> projecte (maig 2025). Reflecteixen un disseny inicial que cotejava
> el corpus contra el mirall de diccionariomadoz.com. Aquell mirall es
> va eliminar del projecte per dues raons: la seva llicència no és
> explícita (incompatible amb la publicació AGPL-3.0 del nostre codi i
> dades) i el projecte upstream no s'ha actualitzat des de 2023. La
> validació encreuada es fa ara amb un segon OCR independent
> (Tesseract 5) sobre el mateix facsímil IA. Vegeu el README per
> l'arquitectura actual; aquesta nota es manté com a registre
> històric.

Side-project segregat de [Nomenclator](https://github.com/acpicornell/nomenclator).
Objectiu: re-digitalitzar el subset balear del *Diccionario geográfico-
estadístico-histórico de España y sus posesiones de Ultramar* de Pascual
Madoz (1845–1850) directament dels facsímils en domini públic
d'Internet Archive.

## Motivació

A Nomenclator s'usa diccionariomadoz.com com a font Madoz. Funciona per
al cas d'ús (extreure variants ortogràfiques de municipis), però té dos
problemes:

1. **Errors numèrics** introduïts per la transcripció humana sobre OCR
   antic. A l'entrada de Maria de la Salut, diu "363 casas, 975 vec.",
   quan l'edició original diu "262 casas, 275 vec." (verificat amb el
   facsímil escanejat). Per anàlisi quantitativa no és fiable.
2. **Cobertura no exhaustiva**. Cens preliminar (vegeu Nomenclator
   commit `ca9be64` i SOURCES.md): el lloc font té el ~95-97% de les
   entrades balears del Madoz canònic. ~30-50 entries menors (predios,
   alqueries, caps, puntes) no estan publicades.

A més, diccionariomadoz.com **no exposa el número de tom ni de pàgina**
de cada entrada. Per a un projecte acadèmic, la traçabilitat (citar
"Madoz, tom II, p. 595") és fonamental.

## Estratègia: pipeline en dues fases

### Fase 1 — Indexació (gratuïta, deterministica)

Per a cada un dels 16 toms, descarregar d'Internet Archive:

- `_chocr.html.gz` (~64 MB) — hOCR comprimit amb `id="word_LEAF_INDEX"`
  per saber a quina pàgina cau cada paraula.
- `_page_numbers.json` (~107 KB) — mapa `leafNum -> pàgina printada`.
- `_djvu.txt` (~6 MB) — text pla, per a cerques ràpides.

Detectar entrades balears amb un regex robust (vegeu
`scripts/index_volume.py`). Output: `data/index/tomo<vol>.jsonl` amb
una fila per entrada:

```json
{"vol": "02", "leaf": 603, "page_printed": "595",
 "title": "ARTA", "context": "V. de la isla de Mallorca, prov., aud. terr..."}
```

Estimació: ~1.500-2.000 entrades balears totals als 16 toms.

### Fase 2 — Extracció de qualitat (Claude Vision)

Per a cada entrada indexada:

1. Descarregar la imatge de la pàgina: `https://archive.org/download/
   diccionariogeogr<vol>mado/page/n{LEAF}_w1600.jpg`. 350 DPI a l'origen
   permet llegir el text amb precisió.
2. Per a entrades que travessen pàgines (PALMA, MAHON), agafar les
   imatges del rang corresponent i passar-les com a context conjunt.
3. Enviar a Claude Sonnet 4.6 (o Opus 4.7) amb prompt que demani JSON
   estructurat:
   ```json
   {"title": "ARTA", "place_type": "villa",
    "island": "Mallorca", "judicial_district": "Manacor", ...,
    "content_full": "...text net...",
    "stats": {"casas": 1323, "vecinos": ..., "habitantes": ...}}
   ```

Cost estimat per al subset balear:
- Sonnet 4.6 amb Vision: ~$30-60 total
- Opus 4.7: ~$150-300
- Anthropic Batch API: 50% més barat

## Què hi ha ja a `data/`

- `data/txt_djvu/tomo01.txt ... tomo16.txt`: els 16 OCRs brut, ja
  descarregats al projecte Nomenclator durant el cens preliminar.
  Conserveu-los: no cal re-baixar.
- `data/chocr/tomo02.html.gz`: chOCR del Tom 2 (POC). Per als altres
  toms, executar `python scripts/fetch_volume.py <vol>`.
- `data/page_numbers/tomo02.json`: mapa leaf→pàgina del Tom 2.
- `data/pages/tomo02_leaf603.jpg`: pàgina 595 (printada) del Tom 2,
  on apareix l'entrada ARTA. Provada visualment: la imatge és correcta.

## Estat del POC

Validat amb una entrada al Tom 2: **ARTA = Tom 02, leaf 603, pàgina
printada 595**. Imatge descarregada (`data/pages/tomo02_leaf603.jpg`)
verificada per inspecció: conté efectivament l'entrada esperada.

Validacions confirmades:
- ✓ chocr d'IA porta `id="word_LEAF_INDEX"` exploitable
- ✓ `_page_numbers.json` resol leaf → pàgina printada amb confiança 96
- ✓ URL pública per descarregar la pàgina com a JPEG (1224×1732 px)

## Passos següents

1. Executar `scripts/fetch_volume.py` per als 16 toms (~1 GB descàrrega).
2. Executar `scripts/index_volume.py` per a cada tom → JSONL d'índex.
3. Fusionar els 16 JSONL i deduplicar per (tom, leaf, title).
4. Decidir esquema JSON exacte de la fase 2 (camps, valors d'enum,
   tractament de taules estadístiques internes).
5. Provar el prompt Vision amb una mostra de 10 entrades (curtes i
   llargues) abans de batch.
6. Comparar amb diccionariomadoz.com per detectar divergències; marcar
   discrepàncies per a revisió humana.

## Decisió pendent

- **Quina llicència** per a aquest projecte?
- **Esquema de sortida**: replicar el de Nomenclator (`madoz_entries`)
  o aprofitar per dissenyar-lo millor (camps separats per
  `judicial_district`, `municipality`, `place_type`, etc. com a enum)?
- **Què fer amb les taules estadístiques** intern a entries grans (com
  PALMA): mantenir-les com a markdown, com a CSV adjunt, o extreure-les
  a una taula separada `madoz_stats`?
