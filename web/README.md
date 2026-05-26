# Madoz · Web estàtica

Explorador local dels articles balears del *Diccionario geográfico-estadístico-histórico* de Pascual Madoz (1845-1850).

Web 100% estàtica, sense backend, sense WASM, sense workers. Un sol JSON (~900 KB), JS vanilla.

## Llançar

Cal servir per HTTP (no `file://`, perquè `fetch()` no funciona amb file URLs):

```bash
python3 -m http.server 8001 -d web
```

Obre <http://localhost:8001>.

## Estructura

- `index.html` — single-page app
- `style.css` — estil (cream + terracotta, mateixa paleta que el projecte Nomenclàtor IB)
- `app.js` — UI vanilla, sense dependències
- `data.json` (~900 KB, 1217 entries) — generat amb `scripts/export_web_data.py`
- `abbreviations.json` — glossari d'abreviatures de Madoz

## Pestanyes

1. **Inici** — visió general del projecte, estadístiques de portada.
2. **Explorar** — filtres (cerca, illa, partit, municipi, tipus, volum, confiança), taula sortable, descàrrega CSV dels resultats actuals. Clica una fila per veure la descripció completa, els estadístics (vecinos, almas, contribución…) i les referències creuades.
3. **Estadístiques** — recomptes per illa, tipus de lloc, partit judicial, top 20 municipis i cobertura per volum.
4. **Demografia** — gràfics SVG inline a partir del camp `stats` JSON de cada article.
5. **Notes** — notes acadèmiques sobre el Diccionari de Madoz.
6. **Abreviatures** — glossari de les abreviatures que usa Madoz al text.

## Re-exportar el JSON després de tocar la BD

```bash
python3 scripts/export_web_data.py
```

(idempotent; sobreescriu `web/data.json`)
