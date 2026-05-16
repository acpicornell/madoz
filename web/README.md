# Madoz · Web estàtica

Explorador local dels articles balears del *Diccionario geográfico-estadístico-histórico* de Pascual Madoz (1845-1850).

Web 100% estàtica, sense backend, sense WASM, sense workers. Un sol JSON (~770 KB), JS vanilla.

## Llançar

Cal servir per HTTP (no `file://`, perquè `fetch()` no funciona amb file URLs):

```bash
python3 -m http.server 8001 -d web
```

Obre <http://localhost:8001>.

## Estructura

- `index.html` — single-page app, dues pestanyes (Explorar, Estadístiques)
- `style.css` — estil (cream + terracotta, mateixa paleta que el projecte Nomenclàtor IB)
- `app.js` — UI vanilla, ~280 línies, sense dependències
- `data.json` (~770 KB) — entrades amb tot el text + URL a diccionariomadoz.com quan hi ha article corresponent (generat amb `scripts/export_web_data.py`)

## Pestanyes

1. **Explorar** — filtres (cerca, illa, partit, municipi, tipus, volum, confiança, només-amb-article-a-diccionariomadoz), taula sortable, descàrrega CSV dels resultats actuals. Clica una fila per veure la descripció completa, els estadístics (vecinos, almas, contribución...), les referències creuades i l'enllaç a diccionariomadoz.com (quan n'hi hagi).
2. **Estadístiques** — recomptes per illa, tipus de lloc, partit judicial, top 20 municipis, cobertura per volum i solapament amb diccionariomadoz.com.

## Re-exportar el JSON després de tocar la BD

```bash
python3 scripts/export_web_data.py
```

(idempotent; sobreescriu `web/data.json`)
