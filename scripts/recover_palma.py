"""Fix the two PALMA entries that the LLM extraction got wrong.

The chocr text for leaf 12/586 starts with the tail of a PREVIOUS
peninsular article (PALAZUELOS or similar — "Valles y Revilla; Peral
y Pinilla" are villages in Palencia province, not Mallorca), and the
LLM mistakenly grabbed that as the body of "PALMA". The REAL PALMA
part. jud. article appears later in the same chocr, beginning with
"PALMA: part. jud. de término en la isla y dióc. de Mallorca…".

Similarly leaf 12/588 starts with the broken OCR of the part. jud.
stats table; the LLM captured that noise as "PALMA". The REAL
PALMA c. article begins after the table with "PALMA: c. con ayunt.
y aduana de primera clase…".

This script:
  * Replaces id=8655 description with the actual PALMA (part. jud.) text
  * Replaces id=8656 description with the actual PALMA (c.) text
  * Sets place_type / island / judicial_district / municipality
  * Re-titles both for clarity
  * Patches the source JSONs so a future load_text.py is consistent

The texts are transcribed from data/text/_chocr/page_12_586.txt and
page_12_588.txt (mild OCR cleanup of whitespace/glue but otherwise
faithful to the chocr).

Idempotent (writes only if description still starts with the wrong
fragment).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

PALMA_PART_JUD = (
    "PALMA: part. jud. de término en la isla y dióc. de Mallorca, "
    "prov., aud. terr., c. g. de Baleares, compuesto de una c., 12 v. "
    "y 20 ald. ó lugarejos, que forman 18 ayunt.; las dist. de las "
    "principales pobl. entre sí, de estas á Palma, que es la cap. en "
    "todos conceptos, y á la corte, asi como su pobl., riqueza, "
    "contr. y otros datos estadísticos se manifiestan en los estados "
    "que incluimos en este artículo. "
    "SIT. Y CLIMA. Se halla ocupando toda la parte O. de la isla; "
    "reinan con frecuencia los vientos del N. y O.; el clima es "
    "templado y sano, aunque algo húmedo, y nebulosa su atmósfera. "
    "Confina por el N. con el mar, y el part. de Inca; por el E. con "
    "el mismo y el de Manacor; por el S. y O. el mar; su estension "
    "es difícil fijar con exactitud, por las muchas inflexiones que "
    "forman los puertos y calas de la costa, y los términos de los "
    "pueblos limítrofes. "
    "TERRITORIO. El que ocupa este part. es sin duda el mas pintoresco "
    "de la isla; una cord. de montes empieza en las playas de Andraix "
    "y de Calviá, que es el estremo occidental de aquella, y siguiendo "
    "por los términos de Estallenchs, Bañalbufar, Puigpuñent, "
    "Valldemosa, Deyá y Soller, termina al N. internándose por Lluch "
    "y Pollenza en el part. de Inca hasta el cabo de Formentó; toda "
    "esta cord. presenta un aspecto tan sorprendente como variada su "
    "perspectiva; poblada de bosques arbolados de pinos y acebuches, "
    "y esmaltada por las frecuentes y bien sit. pobl., forma el mas "
    "delicioso contraste con las fértiles y cultivadísimas llanuras "
    "en que vegeta con lozanía el almendro, el viñedo y los frutales; "
    "mereciendo particular mención el valle de naranjos de Soller, "
    "regado por un torrente que le cruza, y recibe sus aguas de "
    "varias fuentes, ademas de las que vierten los elevados montes "
    "que le rodean; no son menos notables las cercanías de Palma, "
    "cuya c. está rodeada á 1 leg. en contorno de jardines y caseríos "
    "que forman como una pobl. continuada, tanto por la llanura que "
    "se estiende á SE., como por las verdes colinas del NO. y O.; los "
    "puntos mas culminantes de esta cord. son: el Puig de Galatzó en "
    "Puigpuñent, el Coll de Soller, Mola del Esclop en Calviá, y el "
    "Teix en Valldemosa; el terreno en general es arenoso y seco, "
    "así es que se desgracian muchas cosechas en años escasos de "
    "lluvias. "
    "Los puertos principales de la parte de costa que comprende este "
    "part. son: el de Palma, Porto Pi, Porraca, Paguera, Andraitx y "
    "Soller; no deteniéndonos en la descripción de ellos por haberla "
    "hecho minuciosamente en el art. de Mallorca prov. é isla, al "
    "cual remitimos á nuestros lectores. "
    "Ningún rio se halla en todo el part. sólo sí algunos torrentes, "
    "que corren en el invierno y estaciones lluviosas, utilizando "
    "poco sus aguas para el riego, á cuyo objeto se emplean también "
    "las de algunos manantiales. "
    "CAMINOS. Cruzan el part. varias carreteras; la general que "
    "conduce de Palma á Alcudia, y se halla en buen estado; las de "
    "Lluchmayor, Manacor y Soller en estado regular, y otros caminos "
    "vecinales que están en lo general bastante deteriorados, á "
    "pesar de que han recibido mejoras de consideración de pocos "
    "años á esta parte. "
    "PRODUCCIONES. Cereales, aceite, almendras, algarrobas, "
    "legumbres, hortalizas, esquisitos vinos y naranjas, y frutas de "
    "todas clases; se cria ganado de todas especies aunque en corto "
    "número; caza de perdices, conejos y liebres, y abundante pesca "
    "de mar. "
    "INDUSTRIA. Este ramo de la riqueza no se ha desarrollado aun en "
    "ningún punto de esta isla; está concretado á la ind. agrícola, "
    "si bien van estableciéndose algunas fáb. de hilados, ademas de "
    "las de tejidos de lienzos ordinarios; hay una fáb. de fundición "
    "de hierro, muchos molinos harineros de viento y de agua, y en "
    "los pueblos de la montaña las almazaras necesarias para la "
    "elaboración del aceite; se ejercen todas las artes mecánicas "
    "indispensables, y algunas profesiones científicas. "
    "COMERCIO. Se esportan los frutos sobrantes, y se importan "
    "efectos coloniales, quincalla, maderas y otros art. Se celebra "
    "mercado semanal en Palma los sábados, y una feria anual en 21 "
    "de diciembre. "
    "[Madoz inclou aquí extenses taules estadístiques que el chocr "
    "OCR no llegeix de manera coherent; vegeu el facsímil per als "
    "valors de població, riquesa, contribució per municipi.]"
)

PALMA_C = (
    "PALMA: c. con ayunt. y aduana de primera clase, cab. del part. "
    "jud. de su nombre, cap. de la isla, dióc. y prov. marít. de "
    "Mallorca, de la prov. civil y c. g. de Baleares; es residencia "
    "del capitán general, de la autoridad superior política, aud. "
    "terr., silla ep., de la intendencia de rent. y de todas las "
    "autoridades y corporaciones provinciales, así civiles, como "
    "militares y eclesiásticas. "
    "SITUACIÓN Y CLIMA. Se halla á la orilla del mar en la citada "
    "isla de Mallorca, por los 39° 34' 4\" lat. N., y los 6° 24' 14\" "
    "long. E., al seno de una bahía de 3 1/2 leg. de largo y 1 1/2 "
    "de ancho desde el cabo Cala Figuera al SO. y cabo Blanco al "
    "SSE., que forma un semicírculo ó ángulo de 90°; tendida sobre "
    "una línea de 1/2 leg. desde el molinar de Sant Matgí, que da "
    "por el O. en su glasis, hasta el de Calatrava, que se une con "
    "él por medio de las baterías de Sant Onofre y el Carnatje "
    "avanzadas al E., ocupa en su fondo una larga milla SN., desde "
    "el muelle al rebellín de San Antonio, presentando casi toda su "
    "pobl. en forma de anfiteatro, con esposición al SO.; defendida "
    "de los fríos y recios vientos del N. por la alta cord. de "
    "montañas de la isla; goza de buena ventilación, de un clima "
    "estremadamente benigno y saludable, y de una atmósfera pura y "
    "despejada; las enfermedades comunes son las estacionales, "
    "catarros y fiebres intermitentes. "
    "FORTIFICACIONES. La c. se halla circuida de una muralla de "
    "piedra arenosa y blanda de 44 palmos de espesor, y de "
    "escelente mampostería. La circunvalación de la contraescarpa "
    "mide 25,700 pies geométricos, o sean 7,939 varas castellanas. "
    "En 1562 el ingeniero Jorge Fretin, de nación italiano, levantó "
    "los planos de esta fortificación de orden de Felipe II, y ya en "
    "aquel año se dió principio á la obra de los 13 baluartes que "
    "defienden el recinto. Se entra á la c. por 8 puertas bien "
    "distribuidas, 3 que miran al mar y 5 á tierra; la puerta "
    "principal llamada del Muelle, es sencilla y grandiosa; se "
    "compone de sillares almohadillados, y encima tiene una estatua "
    "de Ntra. Sra. de la Concepción. "
    "INTERIOR DE LA POBLACIÓN Y SUS AFUERAS. Consta la c. "
    "aproximadamente de unas 5,000 casas, que forman 236 isletas ó "
    "manzanas, distribuidas en 15 barrios que constituyen 4 "
    "cuarteles; las casas pueden dividirse en dos clases; la primera "
    "las de la nobleza, que todas se parecen en su construcción ant. "
    "y sólida y en su distribución interior, con grandes escaleras y "
    "pilares de mármol; estensos salones muy elevados de techo, que "
    "en lo general están ocupados con galerías de pinturas, en las "
    "cuales hay cuadros de mucho mérito; son por lo común de 3 pisos "
    "con el bajo. Se distinguen entre estas casas las del conde de "
    "Montenegro, marqués del Reguer, de Ariañy, de Solleric, de "
    "Villafranca y de Vivot, y las de los Sres. Villalonga, O'Ryan, "
    "Ripoll y otras; las mas ant. datan del siglo XIV. Las que "
    "constituyen la segunda clase son las de alquiler, de 3 ó 4 "
    "pisos en lo general. Las calles por lo común son rectas y "
    "estrechas. Hay varias plazas que dan ensanche y desahogo á la "
    "pobl.; tales son: la de Atarazana, adornada de árboles, con "
    "una fuente pública; la de las Capillas, que es el punto de "
    "reunión diaria del comercio; la de Cort, llamada así porque en "
    "el edificio consistorial celebraba sus sesiones el grande y "
    "general consejo del reino de Mallorca; la Nova ó de Sta. "
    "Eulalia; la del Mercadal ó del Carbón; la Nueva de la Pescadería; "
    "la del Mercado; las plazas del Gall, del Socors, del Temple y "
    "del Cali. Para la seguridad y vigilancia nocturna hay 30 "
    "serenos y un cabo. "
    "[L'article continua als fulls 589-590+ del Tom XII amb descripció "
    "detallada de la catedral, la Llonja, el palau del capità general, "
    "el palau episcopal, la casa consistorial, la presó, els convents, "
    "i moltes altres seccions. Vegeu el facsímil per al text complet "
    "o la versió ampliada de diccionariomadoz.com.]"
)


def main() -> None:
    apply = "--apply" in sys.argv
    con = duckdb.connect(str(DB), read_only=not apply)

    # Idempotency: skip if the entries already contain the new content.
    fixes = [
        {
            "id": 8655,
            "title": "PALMA (part. jud.)",
            "place_type": "partido judicial",
            "island": "Mallorca",
            "judicial_district": "Palma",
            "municipality": None,
            "description": PALMA_PART_JUD,
            "source_file": "data/text/page_12_586.json",
            "marker": "vicaria compuesta de 22 pueblos",  # old garbage starts with this
        },
        {
            "id": 8656,
            "title": "PALMA",
            "place_type": "ciudad",
            "island": "Mallorca",
            "judicial_district": "Palma",
            "municipality": "Palma",
            "description": PALMA_C,
            "source_file": "data/text/page_12_588.json",
            "marker": "-2 a",  # old garbage starts with this
        },
    ]

    pending = []
    for f in fixes:
        cur = con.execute(
            "SELECT title, substr(description, 1, 30) FROM text_entries WHERE id=?",
            [f["id"]],
        ).fetchone()
        if cur is None:
            print(f"  [skip] id={f['id']} not found")
            continue
        if cur[0] == f["title"] and not cur[1].startswith(". " + f["marker"]) \
                and not cur[1].startswith(f["marker"]):
            print(f"  [skip] id={f['id']} already fixed")
        else:
            pending.append(f)

    print(f"\n{len(pending)} PALMA entries to fix.")
    if not apply:
        for f in pending:
            print(f"  - id={f['id']}: {f['title']} (description {len(f['description'])} chars)")
        if pending:
            print("\nDRY RUN — pass --apply to commit.")
        return

    for f in pending:
        # Patch source JSON
        src = PROJECT / f["source_file"]
        if src.exists():
            data = json.loads(src.read_text(encoding="utf-8"))
            for e in data.get("entries", []):
                if e.get("title") == "PALMA":
                    e["title"] = f["title"]
                    e["place_type"] = f["place_type"]
                    e["island"] = f["island"]
                    e["judicial_district"] = f["judicial_district"]
                    if f["municipality"]:
                        e["municipality"] = f["municipality"]
                    else:
                        e.pop("municipality", None)
                    e["description"] = f["description"]
                    e["confidence"] = "high"
                    e["note"] = (
                        "Manually re-extracted 2026-05-16 from the chocr "
                        "text: the LLM pass had captured the wrong paragraph "
                        "(tail of a previous peninsular article on the same "
                        "leaf). Stats tables not included (chocr noise)."
                    )
            src.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        # Patch DB
        con.execute(
            """UPDATE text_entries
               SET title=?, place_type=?, island=?, judicial_district=?,
                   municipality=?, description=?, confidence='high',
                   note=?
               WHERE id=?""",
            [
                f["title"], f["place_type"], f["island"],
                f["judicial_district"], f["municipality"], f["description"],
                ("Manually re-extracted from chocr; the LLM pass had "
                 "captured the wrong paragraph (tail of a previous "
                 "peninsular article). Stats tables omitted."),
                f["id"],
            ],
        )
        print(f"  ✓ id={f['id']} → {f['title']!r}")


if __name__ == "__main__":
    main()
