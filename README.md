# prompt-hub

Catálogo HTTP de los prompts de la flota (semana 17): los prompts viven dispersos en un
repo por consumidor; `prompt-hub` los lee todos, los sirve versionados con su estructura
(secciones y líneas vía `parser-io`) y renderiza sus templates con variables — **redactado**.
El único LLM es la ficha de catálogo: una descripción validada del prompt, con caché TTL
para no pagarla dos veces.

Es el **tercer consumidor de `parser-io`** (tras content-ray y docs-gen): la regla del
tercer uso (D12) se cumple y el módulo queda consolidado en el core.

## Demo

```bash
uv sync
uv run prompt-hub scan | head -20
# [
#   {
#     "repo": "bench-runner",
#     "path": "prompts/regression-report-generator.md",
#     "id": "regression-report-generator",
#     "version": "0.1.0",
#     ...
```

Y como servicio (los endpoints también se ejercitan en los tests con `TestClient`):

```bash
uv run uvicorn prompt_hub.main:app   # ver abajo: app = create_app(...)
# GET  /health                       → week 17, provider, modelo
# GET  /prompts                      → inventario de la flota
# GET  /prompts/triage-bot/issue-triage-generator → secciones con rango de líneas
# POST /prompts/triage-bot/issue-triage-generator/render {"variables": {...}}
# POST /describe {"repo": "...", "prompt_id": "..."} → ficha LLM validada (prompt-card-v1)
```

Para servir: `app = create_app(provider=OpenCodeCLI("opencode/big-pickle"), fleet=scan_fleet(discover_fleet(...)))`.

## Cómo funciona

- **El inventario es determinista**: cada `prompts/*.md` de la flota se lee con `parser-io`
  (frontmatter aparte, cuerpo en secciones con líneas) — sin LLM.
- **El render respeta la convención de la flota**: solo la sección `Usuario` lleva
  variables; la de `Sistema` tiene llaves de JSON literales y pasa intacta. Lo que sale
  del template pasa por `secure-base` antes de la respuesta.
- **La ficha es el único LLM** (`prompt-card-v1`): propósito, cuándo usarlo, inputs,
  forma de salida. La caché TTL y el límite de ritmo (`cache-ratelimit`) rodean al
  proveedor: pedir la misma ficha dos veces no vuelve a cruzar.
- Errores uniformes de `web-api-base`: prompt inexistente → 404, variable faltante o
  respuesta inválida → 422.

## Recicla de

| Módulo | Uso |
|---|---|
| `parser-io` | tercer uso (D12): cada prompt del catálogo, leído en secciones con líneas |
| `web-api-base` | app, health y errores uniformes de la API |
| `llm-client` | la llamada de la ficha (retry + reparación + span de 20 campos) |
| `schema-validate` | la ficha valida contra `prompt-card-v1` o falla |
| `secure-base` | el excerpt entra saneado al prompt y el render sale redactado |
| `cache-ratelimit` | caché TTL + token bucket alrededor del proveedor |
| `test-kit` | el prompt propio nace evaluado: dataset congelado + replay (D4) |

## Limitaciones

- El catálogo es una foto del escaneo: se regenera al arrancar el servicio, no vigila
  cambios en vivo (un watcher llegaría con el segundo consumidor de la idea).
- `GET /prompts` no pagina: la flota cabe; el día que no quepa, se pagina.
- La ficha describe el texto del prompt, no su calidad: eso es trabajo de `bench-runner`.
- Sin auth: es un catálogo interno de la flota (auth-base, sem. 25).

## Roadmap

- [x] Inventario + detalle + render redactado + ficha LLM validada (sem. 17)
- [x] Tercer consumidor de `parser-io` — consolidación D12 disparada
- [ ] Render por endpoint con template del cuerpo completo (no solo `Usuario`)
- [ ] Diff de versiones de un prompt entre tags del repo consumidor
