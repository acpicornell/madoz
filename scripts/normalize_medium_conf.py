"""Normalize the title and/or description of text_entries rows marked
``confidence='medium'``.

These 144 rows were extracted in a previous Claude Code session and
the model left obvious OCR noise in the descriptions (missing spaces,
``ténn`` for ``térm``, ``laisla`` for ``la isla``, leading stray
periods, etc.). This script applies hand-curated normalizations
without inventing new content — every correction is grounded in
either an unambiguous OCR-noise pattern, a sibling chocr entry on
the same leaf, or the curated diccionariomadoz.com title.

Three parallel lists:
  - TITLE_FIXES:      (id, new_title) for OCR-mangled titles.
  - PLACE_TYPE_FIXES: (id, new_place_type) for rows misclassified by
    the LLM (rare).
  - DESC_FIXES:       (id, new_description) for description cleanups.

All three are independent; a row can appear in any combination.

Same shape as the previous fix scripts: dry-run by default, ``--apply``
writes the DB and the source JSON. Idempotent.

  python scripts/normalize_medium_conf.py            # dry run
  python scripts/normalize_medium_conf.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"


# (text_entry_id, new_title) — only when the title is OCR-garbled.
TITLE_FIXES: list[tuple[int, str]] = [
    # Pilot
    (8755, "PUNXUAT"),       # was 'PUiNXUAT' — stray lowercase 'i' OCR-noise; toponym verified (es Pou de Punxuat, Algaida)
    # Batch 1 (vols 01-11)
    # — none, all titles already clean.
    # Batch 2 (vol 12)
    (8646, "OPHIUSA"),       # was 'OPHIüSA' — curated 120692 = OPHIUSA (the classical Greek/Latin name)
    (8692, "PEROT (son)"),   # was 'PLROT (son)' — same leaf has multiple 'PEROT (son)' siblings; the chocr 'PL' is OCR misread of 'PE'
    # Batch 3 (vol 13 first half)
    (8759, "QUINT (so)"),    # was 'QUINSTILANS' — the chocr regex matched a Galician 'QUINSTILANS' on the same leaf, but the description the LLM extracted is for the adjacent Balearic 'QUINT (so)' predio. Re-title.
    (8769, "RAFAL AMAGAT"),  # was "RAFAL AMAGA!'" — chocr OCR rendered the final T as '!'' (exclamation+apostrophe); toponym verified
    # Batch 4 (vol 13 second half)
    (8874, "SAN LLORENS"),   # was 'SAN LLORIiNS' — curated 39857 confirms; chocr 'LLORIiNS' is OCR misread of 'LLORENS'
    # Batch 5 (vols 14-16)
    (9038, "VERDERA (so)"),  # was 'VERDERA' — the description (starting with '(so):') is actually for VERDERA (so) Llubí; curated has two VERDERA (SO) rows
    # Post-batch user correction
    (8970, "TAULERA (la)"),  # was 'TAULEBA (la)' — chocr regex misread of 'TAULERA (la)'. Confirmed by user; this row (Palma district) corresponds to curated 107899 = TAULERA (LA) Bañalbufar
]


# (text_entry_id, new_place_type)
PLACE_TYPE_FIXES: list[tuple[int, str]] = [
    (8759, "predio"),        # paired with the title fix above — QUINT (so) is a predio
    (8876, "feligresía"),    # SAN MATEO: previously 'aldea' (the LLM mistakenly extracted the Gerona aldea entry); the Balearic SAN MATEO is the Ibiza feligresía linked via 39871
]


# (text_entry_id, new_description) — descriptions normalized for OCR
# noise. Each correction is grounded; commit message will reference
# the patterns applied per row.
DESC_FIXES: list[tuple[int, str]] = [
    # Pilot
    (8639, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Alaró."),
    (8717, "cala pequeña en la isla, tercio y prov. marítima de Mallorca, distrito de Andraitx, del part. de Cartagena, sit. al E. de la c. de Palma."),
    (8754, "predio en la isla de Mallorca, prov. de Baleares, part. jud., térm. y jurisd. de la v. de Manacor."),
    (8755, "ald. ó cas. en la isla de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud. de Palma, térm. y jurisd. de Algaida."),
    (8890, "predio en el valle den March, en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza; tiene un molino de aceite."),
    # Batch 1 (vols 01-11) — only one fix needed.
    (7933, "Punta de la isla, tercio y prov. marít. de Mallorca, distr. de Alcudia, apostadero de Cartagena. Sit. entre la punta del Viento y el cast. de Pollenza, entre el puerto de este nombre. (V. Pollenza.)"),
    # Batch 2 (vol 12) — clean leading-noise and OCR-typo patterns.
    (8625, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza."),
    (8627, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Felanitx."),
    (8631, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Valldemosa."),
    (8638, "predio en la isla de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Algaida."),
    (8640, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Felanitx."),
    (8646, "nombre que se dió en la antigüedad á la isla llamada hoy Formentera."),
    (8647, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Porreras."),
    (8649, "l. en la isla y dióc. de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud. de Palma (5 hor.), ayunt. de Buñola (2). sit. en un valle circuido de altos montes, poblados de encinas y pinos; su clima es frió, pero sano, y las enfermedades comunes son catarros. Tiene 50 casas; 1 escuela de instrucción primaria, concurrida por 20 alumnos; 1 igl. parr. (S. Jorge), aneja de la de Buñola, servida por 1 vicario temporal y amovible, que nombra el diocesano; 1 cementerio fuera de la pobl. El térm. confina N. Lluch ó Escorca; E. Alaró; S. Buñola, y O. Soller. El terreno es muy feraz generalmente; le cruzan varios caminos locales, que se hallan en mal estado. prod.: trigo, aceite, bellota, nueces y seda; cria ganado lanar, asnal y de cerda (V. el cuadro sinóptico.)."),
    (8659, "alq. en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Maria."),
    (8664, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Porreras."),
    (8665, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Valldemosa."),
    (8668, "campos antiguamente del predio Bocar en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza."),
    (8673, "alq. en la isla de Mallorca, prov. de Baleares, part. jud., térm. y jurisd. de la v. de Inca."),
    (8674, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de San Juan."),
    (8677, "ald. ó cas. en la isla de Mallorca, part. jud. de Manacor, prov., aud. terr. y c. g. de Baleares, térm. jurisd. de la v. de Son Servera."),
    (8685, "predio en la isla de Mallorca, prov. de Baleares, part. jud., térm. y jurisd. de la v. de Manacor."),
    (8689, "alq. en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Maria."),
    (8692, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Llubí."),
    # Batch 3 (vol 13 first half) — same cleanup patterns: leading
    # garbage, OCR letter swaps verifiable from corpus, joined words.
    (8760, "ald. en la isla y dióc. de Mallorca, part. jud. de Palma, prov., aud. terr., c. g. de Baleares, térm. y jurisd. de Algaida. Tiene una igl. parr. aneja de la de este último pueblo, dedicada á los Santos Cosme y Damián, servida por un vicario temporal y amovible y un sacerdote ordenado á título de patrimonio. Su pobl. y riqueza unida á Algaida."),
    (8761, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Felanitx."),
    (8885, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Santagny."),
    (8894, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Bañalbufar."),
    (8699, "c. desaparecida en la isla de Mallorca; estuvo sit., según opinión de muchos escritores, en el lugar de la c. de Alcudia á la parte del puerto mayor hacia el N. en el que en el dia se llama estanque de Sta. Ana, en el sitio donde hoy existe un oratorio dedicado á esta santa; á sus inmediaciones se ven vestigios y gradas de un anfiteatro, y se han descubierto con frecuencia varias antigüedades, como estatuas, cabezas, columnas de aquel tiempo de los romanos, sepulturas, urnas, cenizas, epigramas, títulos, y particularmente muchas medallas y monedas de cobre y de plata de emperadores romanos, que según el Dr. D. Juan Binimelis, historiador de Mallorca, pasaban de 5 arrobas de peso las que él habia visto reunidas; en estos campos se han hallado ladrillos rotos en gran cantidad, subterráneos tabicados, hornos y otras cosas."),
    (8710, "cueva en el predio de Ariant, en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza: en su entrada tiene un salón muy estenso, y en su pavimento se ven las bocas de varias cavernas, pero se ignora si ha habido persona que se haya atrevido á bajar á ellas."),
    (8712, "cuartón en la isla de Ibiza (V. el art. de dicha isla)."),
    (8718, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Felanitx."),
    (8719, "cala en la isla de Mallorca, perteneciente á la prov. y tercio marítimo de este nombre, distr. de Andraix, departamento de Cartagena: se halla en el térm. y jurisd. de la c. de Palma, defendida enteramente de los vientos; puede contener hasta fragatas de mayor porte, pero en corto número; en otro tiempo estaba mas limpia y capaz, y era el abrigo de las embarcaciones mayores que hacian el gran comercio de la isla; tiene una pequeña batería al lado de la c., y por privilegio del rey D. Pedro de Aragón, cerraba su entrada una cadena, conservándose aun en una y otra orilla las rocas en que se prendía: en la parte opuesta á la batería está sit. el ant. faro, que sirve de guia á las embarcaciones, el cual es giratorio con dos minutos de luz y uno de oscuro, elevado sobre el mar 48 varas, y visible su luz á 13 millas y 2/10 de dist.; en la punta del N. hay otra torre casi de la misma hechura que la de la linterna, nombrada torre de los Pelaires."),
    (8720, "ald. en la isla de Mallorca, part. jud. de Palma, prov., aud. terr., c. g. de Baleares, térm. y jurisd. de Marratxí; tiene un oratorio con culto público."),
    (8721, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Calvià."),
    (8722, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Sineu."),
    (8728, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Porreras."),
    (8731, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Andraitx."),
    (8759, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Esporlas."),
    (8764, "ald. en la isla de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud. de Palma, ayunt., térm. y jurisd. de Deyá."),
    (8766, "predio en la isla de Menorca, prov. de Baleares, part. jud. de Mahón, térm. y jurisd. de Alayor."),
    (8769, "predio subdividido hoy en pequeñas porciones, en la isla de Menorca, prov. de Baleares, part. jud., térm. y jurisd. de la c. de Mahón."),
    (8770, "predio en la isla de Menorca, prov. de Baleares, part. jud., térm. y jurisd. de la c. de Mahón."),
    (8771, "predio en la isla de Menorca, prov. de Baleares, part. jud., térm. y jurisd. de la c. de Mahón."),
    (8774, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Sta. Margarita."),
    (8776, "predio en la isla de Menorca, prov. de Baleares, part. jud. de Mahón, térm. y jurisd. de Alayor."),
    # Batch 4 (vol 13 second half) — same cleanup patterns.
    (8777, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Maria."),
    (8779, "predio en la isla de Menorca, prov. de Baleares, part. jud. de Mahón, térm. y jurisd. de Alayor."),
    (8781, "predio en la isla de Menorca, prov. de Baleares, part. jud. de Mahón, térm. y jurisd. de Alayor."),
    (8782, "predio en la isla de Menorca, prov. de Baleares, part. jud., térm. y jurisd. de la c. de Mahón."),
    (8787, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Llubí."),
    (8789, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Felanitx."),
    (8790, "predio en la isla de Mallorca, prov. de Baleares, part. jud. y térm. jurisd. de la v. de Manacor."),
    (8791, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Felanitx."),
    (8793, "ald. en la isla y dióc. de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud., térm. y jurisd. de la c. de Palma: tiene una capilla con culto público."),
    (8797, "predio en la isla de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud., térm. y jurisd. de la c. de Palma."),
    (8805, "con esta denominación se conocen tres distintos predios en la isla de Mallorca, prov. de Baleares, part. jud., térm. y jurisd. de la v. de Manacor."),
    (8806, "hay dos predios de igual nombre en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Artá."),
    (8808, "con esta denominación se conocen dos predios distintos en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Santagny."),
    (8813, "predio con huerta en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Campanet."),
    (8816, "con este nombre se conocen dos predios distintos en la isla de Mallorca, prov. de Baleares, part. jud., térm. y jurisd. de la v. de Manacor."),
    (8823, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Escorca."),
    (8833, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Artá."),
    (8836, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Porreras."),
    (8837, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Petra."),
    (8840, "granja en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Maria."),
    (8848, "predio en la isla de Mallorca, prov. de Baleares, part. jud., térm. y jurisd. de la v. de Manacor."),
    (8849, "ald. en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Sansellas."),
    (8854, "pequeña ald. en la isla de Mallorca, part. jud. y térm. jurisd. de la c. de Palma."),
    (8855, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. del l. de Montuiri."),
    (8856, "bajo esta denominación se conocen dos predios distintos en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Felanitx."),
    (8857, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Campos."),
    (8864, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la villa de Campos."),
    (8869, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Campos."),
    (8873, "ald. en la isla y dióc. de Menorca, part. jud. de Ciudadela, prov., aud. terr. y c. g. de Baleares, ayunt. de Mercadal: sit. en el térm. de esta v., en terreno hondo y húmedo, por cuya causa es insalubre su clima, y propenso á fiebres intermitentes. Tiene 19 cas., y una igl. parr. (San Juan Bautista) servida por un vicario de provisión del diocesano. Su pobl. y riqueza unida á Mercadal."),
    (8874, "felig. en la isla, part. jud. y dióc. de Ibiza, aud. terr. y c. g. de Baleares, distr. municipal de San Juan Bautista. Tiene una igl. parr. (San Lorenzo) servida por un cura de segundo ascenso. Su pobl. y riqueza (V. Ibiza)."),
    (8876, "felig. en la isla, part. jud. y dióc. de Ibiza, aud. terr. y c. g. de Baleares, distr. municipal de San Antonio. Tiene una igl. parr. (San Mateo) servida por un cura de segundo ascenso. Su pobl. y riqueza (V. Ibiza)."),
    (8878, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza."),
    (8896, "predio con huerta en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Campanet."),
    (8900, "ald. en la isla de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud. de Palma, ayunt., térm. y jurisd. de la v. de Andraix."),
    (8902, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Soller."),
    # Batch 5 (vols 14-16)
    (8956, "isleta en la isla, prov. y tercio marítimo de Mallorca, térm. de la c. de Palma, departamento de Cartagena, distr. de Andraix: sit. á 2 millas al N. 27° E. del cabo de Cala Figuera, con paso entre ella y la costa de mas de 10 brazas de fondo."),
    (8919, "ald. en la isla y dióc. de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud., térm. y jurisd. de la c. de Palma: tiene una capilla con culto público."),
    (8921, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Campos."),
    (8944, "predio y marquesado en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Alaró."),
    (8947, "ald. en la isla y dióc. de Mallorca, partido jud. de Palma, prov., aud. terr., c. g. de Baleares, térm. y jurisd. de la v. de Calvià."),
    (8948, "ald. en la isla de Mallorca, part. jud. de Manacor, térm. y jurisd. de la v. de Campos, á la cual está unida su pobl. y riqueza."),
    (8949, "predio con huerta en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Campanet."),
    (8959, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Artá."),
    (8967, "casa de campo en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Buger."),
    (8972, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la villa de Alaró."),
    (8975, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza."),
    (9002, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza."),
    (9016, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Valldemosa."),
    (9018, "predio en la isla de Mallorca, prov. de Baleares, part. jud., térm. y jurisd. de la v. de Manacor."),
    (8988, "dos predios en la isla de Mallorca, prov. de Baleares, partido jud. de Inca, térm. y jurisd. de la villa de Lloseta."),
    (8990, "predio en la isla de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Algaida."),
    (8999, "ald. con oratorio público en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Campanet."),
    (9020, "ald. en la isla y dióc. de Mallorca, prov., aud. terr. y c. g. de Baleares, part. jud. de Palma, térm. y jurisd. de Calvià, de cuyo l. depende; tiene una capilla con culto público."),
    (9024, "ald. en la isla y dióc. de Mallorca, prov., aud. terr., c. g. de Baleares, part. jud. de Palma, térm. y jurisd. de la v. de Calvià, tiene una capilla con culto público; su pobl. y riqueza unida á la de dicha v."),
    (9027, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Porreras."),
    (9028, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza."),
    (9029, "reunión de casas contiguas á las del predio de este nombre, en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza."),
    (9036, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Manacor, térm. y jurisd. de la v. de Felanitx."),
    (9038, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la villa de Llubí."),
    (9072, "predio en la isla de Mallorca, prov. de Baleares, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza."),
    (9055, "pobl. desaparecida en la isla de Mallorca, part. jud. de Inca, térm. y jurisd. de la v. de Pollenza."),
    (9056, "pobl. desaparecida en la isla de Mallorca, part. jud. de Inca, térm. jurisd. de la c. de Alcudia."),
    (9057, "granja en la isla de Mallorca, part. jud. de Inca, térm. y jurisd. de la v. de Llubí."),
    (9064, "predio y marquesado de igual nombre en la isla de Mallorca (Baleares), part. jud., térm. y jurisd. de la v. de Inca."),
]


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    # Build plan
    t_plan = []
    for tid, nt in TITLE_FIXES:
        row = con.execute(
            "SELECT title, source_file FROM text_entries WHERE id=?", [tid]
        ).fetchone()
        if not row:
            print(f"  [skip-T] id={tid} not found")
            continue
        ot, src = row
        if ot == nt:
            print(f"  [skip-T] id={tid} title already fixed")
            continue
        t_plan.append((tid, ot, nt, src))

    pt_plan = []
    for tid, npt in PLACE_TYPE_FIXES:
        row = con.execute(
            "SELECT place_type, source_file FROM text_entries WHERE id=?",
            [tid],
        ).fetchone()
        if not row:
            print(f"  [skip-PT] id={tid} not found")
            continue
        opt, src = row
        if opt == npt:
            print(f"  [skip-PT] id={tid} place_type already fixed")
            continue
        pt_plan.append((tid, opt, npt, src))

    d_plan = []
    for tid, nd in DESC_FIXES:
        row = con.execute(
            "SELECT title, description, source_file FROM text_entries WHERE id=?",
            [tid],
        ).fetchone()
        if not row:
            print(f"  [skip-D] id={tid} not found")
            continue
        title, od, src = row
        if od == nd:
            print(f"  [skip-D] id={tid} desc already fixed")
            continue
        d_plan.append((tid, title, od, nd, src))

    print(f"\n{len(t_plan)} title changes:")
    for tid, ot, nt, src in t_plan:
        print(f"  id={tid:5}  title: {ot!r} -> {nt!r}")

    print(f"\n{len(pt_plan)} place_type changes:")
    for tid, opt, npt, src in pt_plan:
        print(f"  id={tid:5}  place_type: {opt!r} -> {npt!r}")

    print(f"\n{len(d_plan)} description changes:")
    for tid, title, od, nd, src in d_plan:
        print(f"  id={tid:5}  {title!r}  desc {len(od)}->{len(nd)} chars")

    if not apply:
        if t_plan or pt_plan or d_plan:
            print("\nDRY RUN — pass --apply to commit.")
        return

    # Apply
    for tid, ot, nt, src in t_plan:
        path = PROJECT / src
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for e in data.get("entries", []):
                if e.get("title") == ot:
                    e["title"] = nt
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        con.execute("UPDATE text_entries SET title=? WHERE id=?", [nt, tid])
        print(f"  ✓ T id={tid}")

    # Resolve place_type fixes by id — match in JSON by (current) title
    # since titles may have just been updated above.
    for tid, opt, npt, src in pt_plan:
        row = con.execute(
            "SELECT title FROM text_entries WHERE id=?", [tid]
        ).fetchone()
        cur_title = row[0] if row else None
        path = PROJECT / src
        if path.exists() and cur_title:
            data = json.loads(path.read_text(encoding="utf-8"))
            for e in data.get("entries", []):
                if e.get("title") == cur_title:
                    e["place_type"] = npt
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        con.execute(
            "UPDATE text_entries SET place_type=? WHERE id=?", [npt, tid]
        )
        print(f"  ✓ PT id={tid}")

    for tid, title, od, nd, src in d_plan:
        path = PROJECT / src
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for e in data.get("entries", []):
                if e.get("description") == od:
                    # Preserve the LLM's first-pass extraction the first
                    # time we touch this entry; never overwrite it on
                    # subsequent runs.
                    if not e.get("description_raw"):
                        e["description_raw"] = od
                    e["description"] = nd
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        con.execute(
            "UPDATE text_entries "
            "SET description=?, "
            "    description_raw=COALESCE(description_raw, ?) "
            "WHERE id=?",
            [nd, od, tid],
        )
        print(f"  ✓ D id={tid}")

    print(
        f"\nApplied {len(t_plan)} title + {len(pt_plan)} place_type + "
        f"{len(d_plan)} description fixes."
    )


if __name__ == "__main__":
    main()
