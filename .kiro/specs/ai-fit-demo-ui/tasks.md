# Implementation Plan: AI Fit Demo UI

## Overview

This plan builds the thin presentation layer defined in the design: a single Flask process (`Recommendation_Service`) that loads the existing `ai_fit` agent once at startup, exposes three JSON endpoints plus static-asset serving, and a vanilla HTML/CSS/JS `Demo_UI` that renders the simulated PDP, Fit_Panel, Demo_Guide, and masked activity trace from server-supplied JSON.

The implementation adds **no fit logic**. It reuses `ai_fit.demo.make_orchestrator` / `build_stub_client` for wiring, calls `ConverseOrchestrator.produce_recommendation`, `match_occasion`, and `find_alternative_styles`, and serializes their results field-for-field. Per the design's Testing Strategy, property-based testing is intentionally omitted; test steps are serialization/contract tests, example-based endpoint tests (run in Stub_Mode, no AWS), DOM-level UI/integration tests, and a smoke test.

Each task builds on the previous one and ends with wiring the pieces into the single running process. All test sub-tasks are optional (`*`).

## Tasks

- [x] 1. Set up the package, dependency, and startup wiring
  - Create the `ai_fit_demo_ui/` package (`__init__.py`, `__main__.py`, `service.py`) so the demo starts with `python -m ai_fit_demo_ui`.
  - Add the single pinned web dependency `Flask==3.0.3` to `pyproject.toml` and `requirements.txt` alongside the existing `ai_fit` deps; add no front-end build toolchain.
  - Define the `ProductConfig` startup model (`sku`, `name`, `price_usd`, `color_options`, `image_path`, `offered_sizes`, `demo_occasion`, `demo_customer_id`) and a default config for the single demo SKU/customer.
  - _Requirements: 9.1, 9.4_

  - [x] 1.1 Implement `create_app(*, stub, product_config)` loading the agent once
    - Build the `DemoAgent` at startup via `ai_fit.demo.make_orchestrator(client)`, where `client` is `None` for live mode or `build_stub_client(...)` for Stub_Mode, and store the agent + product_config on the app so every request reuses the same loaded `DataStore` and bound tools.
    - Read `offered_sizes` at startup from `get_product_catalog_entry(store, sku).offered_sizes`, falling back to the configured list only when the catalog has none.
    - Select Stub_Mode via `AI_FIT_DEMO_STUB=1` or a `--stub` flag in `__main__.py`; live mode (default) uses `make_orchestrator(None)` unchanged.
    - _Requirements: 7.3, 8.3, 9.2, 9.3_

  - [x] 1.2 Write a startup test asserting single load and no network in Stub_Mode
    - Assert the data store is loaded once and reused (shared object identity / single load call), not reloaded per request.
    - Assert building the app in Stub_Mode performs no Bedrock/network call.
    - _Requirements: 7.3, 8.3, 9.2_

- [x] 2. Implement the recommendation endpoint and JSON serialization contract
  - [x] 2.1 Implement `POST /api/recommendation`
    - Map the body `{sku, size, customer_id, occasion}` to `produce_recommendation(sku, stated_usual_size=size, customer_id=customer_id, direct_occasion=occasion)` and separately call `match_occasion(store, sku, direct_occasions=[occasion])`.
    - Serialize the returned `Recommendation` with `dataclasses.asdict` with no key renaming, filtering, or value transformation, and attach the `occasion_match` dict unchanged.
    - When `recommendation.ok` is false, still return `200` with the serialized failure (including `failure_reason`); never fabricate a size/confidence.
    - Return `400 {error:"size_required"}` when size is missing/invalid (defensive guard behind the UI).
    - _Requirements: 7.1, 7.4, 8.1, 2.5_

  - [x] 2.2 Write serialization / contract tests for the recommendation endpoint
    - Given a known `Recommendation` with a populated masked `trace`, assert the response recommendation sub-object equals `dataclasses.asdict(recommendation)` with `trace.masked_fields` and the redaction placeholder preserved exactly.
    - Assert the `/api/recommendation` payload contains none of the protected keys (age, gender, ethnicity, nationality, location) at any nesting depth.
    - Given `Recommendation(ok=False, failure_reason=...)`, assert the failure fields pass through and no fabricated size/confidence is present.
    - _Requirements: 7.4, 1.5, 2.2, 8.1_

- [x] 3. Implement the alternatives and config endpoints
  - [x] 3.1 Implement `POST /api/alternatives`
    - Call `find_alternative_styles(store, occasion, sku)` and return its dict unchanged (`requested_occasion`, `alternatives[]`, `none_available`, `message`), already capped at three by the tool.
    - _Requirements: 5.1, 5.2, 5.3, 5.5, 7.2_

  - [x] 3.2 Implement `GET /api/config`
    - Return the `DemoUIConfig`: the `ProductConfig` (minus internal ids the browser does not need), a Protected_Attribute-free `customer_summary` (`usual_shoe_size` + `comfort_style_priority` only, derived from `get_customer_profile`), the Prototype_Disclaimer text, and the ordered Demo_Guide steps.
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 2.2_

  - [x] 3.3 Write endpoint tests for alternatives and config (Stub_Mode)
    - Assert `/api/alternatives` returns the `find_alternative_styles` dict and that the none-available case returns `none_available: true`.
    - Assert `/api/config` carries only `usual_shoe_size` + `comfort_style_priority` for the customer and contains no protected keys at any depth.
    - _Requirements: 5.1, 5.3, 1.5, 2.2_

- [x] 4. Add centralized error handling and single-process static serving
  - [x] 4.1 Implement the error handler and static-asset routes
    - Catch any unexpected endpoint exception and return `503 {error, retryable:true}` with a short message and no stack trace.
    - Serve the Demo_UI static assets from the same process: `GET /` returns `index.html`; `GET /static/<asset>` returns `app.js`, `styles.css`, and the product image.
    - _Requirements: 8.2, 7.5_

  - [x] 4.2 Write endpoint tests for error handling and static serving
    - Assert an endpoint that raises internally returns `503 {retryable:true}` and not a stack trace.
    - Assert `GET /` serves the Demo_UI HTML and `GET /static/app.js` serves the script from the single process.
    - _Requirements: 8.2, 7.5_

- [x] 5. Checkpoint - service layer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Build the Demo_UI shell: PDP, disclaimer, Demo_Guide, and client state
  - [x] 6.1 Create `index.html` and `styles.css` for the PDP shell
    - Lay out the PDP region (product image, name, price, color swatches), the Prototype_Disclaimer banner, the Demo_Guide strip with a hide control, the Fit_Panel ("ALDO FIT") container, the Add to Bag control, and the alternatives region.
    - _Requirements: 1.1, 1.3, 1.4_

  - [x] 6.2 Create `app.js` bootstrap: fetch `/api/config` and render PDP + size grid
    - Define the `UIState` object (`selectedSize`, `config`, `lastRecommendation`, `lastOccasionMatch`, `lastAlternatives`, `pending`, `lastError`, `activityExpanded`).
    - On load, fetch `/api/config`, render the PDP from server config, build the size grid from `product.offered_sizes` as selectable controls, render the disclaimer and Demo_Guide (with working hide control), and render the customer fit summary (usual size + comfort priority only, no Protected_Attribute).
    - Record a clicked size into `UIState.selectedSize`; never compute fit in the browser.
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 2.1, 2.2_

  - [x] 6.3 Write DOM-level tests for the PDP shell
    - Assert the size grid is built from `offered_sizes` and that selecting a size records it; assert the disclaimer and Demo_Guide (with hide) render and no protected field appears in the DOM.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2_

- [x] 7. Implement the fit-insight request and recommendation rendering
  - [x] 7.1 Wire the "Get fit insight" control and pending/guard states
    - On activation, guard that a size is selected; if none, prompt "please select a size" and issue no request.
    - When a size is selected, set `pending`, hide/grey the prior result so it is not read as current, and `POST /api/recommendation` with `{sku, size, customer_id, occasion}`; store the response into `UIState`.
    - _Requirements: 2.3, 2.4, 2.5_

  - [x] 7.2 Render the recommendation from server JSON
    - Render `recommended_size`, `confidence`, and `explanation` when `ok` is true; present advisory framing when `advisory === true`; show `supporting_signal_count` as the evidence amount.
    - Render each `warnings[]` entry and `note` as a Fit_Warning; highlight the difference when `recommended_size !== selectedSize`.
    - When `ok` is false, show the customer-facing failure message from `failure_reason`/`explanation` and show no size/confidence.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 8.1_

  - [x] 7.3 Write DOM-level tests for fit-insight trigger and rendering
    - Assert the no-size guard prompts and issues no request; assert the pending state hides the prior result; assert a successful recommendation renders size/confidence/explanation/evidence/advisory/warnings and a highlighted mismatch; assert an `ok:false` response shows the failure and no size/confidence.
    - _Requirements: 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 8.1_

- [x] 8. Implement the add-to-bag flow, alternatives, and occasion match
  - [x] 8.1 Implement the Add to Bag warning flow
    - On Add to Bag, if a Fit_Warning is present for the selected size, surface it at the point of adding, offer a "Find alternatives" option, and allow "Add anyway" to proceed with the original selection.
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 8.2 Render alternatives and the occasion match
    - On "Find alternatives", `POST /api/alternatives` for the browsed SKU and demo occasion and render each alternative's name, price, and suited occasions (at most three); when `none_available` is true, show the explicit "no alternatives currently available" message.
    - Render the occasion-match result, naming whether and which occasion matched from `occasion_match.status` / `matched_occasions`.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x] 8.3 Write DOM-level tests for the add-to-bag and alternatives flow
    - Assert add-to-bag with a warning surfaces it, offers "Find alternatives", and allows "Add anyway"; assert alternatives render ≤3 entries with name/price/occasions and the none-available message when empty; assert the occasion match is named.
    - _Requirements: 4.2, 4.3, 4.4, 5.2, 5.3, 5.4, 5.5_

- [x] 9. Implement the collapsible masked activity (trace) rendering
  - [x] 9.1 Render the activity trace under a default-collapsed toggle
    - Render `recommendation.trace` under an expandable "activity" control that defaults to collapsed; on expand, show the ordered `trace.steps[]`, tools invoked, and `trace.validator_corrections[]`.
    - Render masked identifiers verbatim as received (never resolve `customer_uid` / `cnsmr_prof_sid`); render `trace.failed_tools[]` as "tool failed / data unavailable".
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 9.2 Write DOM-level tests for the activity trace
    - Assert the activity section defaults collapsed, expands to show ordered steps/tools/validator corrections, renders the redaction placeholder verbatim, and shows failed-tool/unavailable-data entries.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 10. Implement non-blocking error and retry handling in the UI
  - On a non-2xx response or network error, set `lastError` and show a non-blocking error banner with a Retry action that re-issues the POST; keep size selection and add-to-bag usable throughout.
  - _Requirements: 8.2, 8.4_

  - [x] 10.1 Write DOM-level tests for error and retry resilience
    - Assert a failed request shows a non-blocking error with Retry and that size selection + add-to-bag remain usable.
    - _Requirements: 8.2, 8.4_

- [x] 11. Checkpoint and end-to-end smoke test
  - [x] 11.1 Write a single-command Stub_Mode smoke test
    - Start the app in Stub_Mode via the single command, issue one `POST /api/recommendation`, and assert a `200` with a recommendation and a visible masked trace; assert the served page contains the Prototype_Disclaimer and Demo_Guide.
    - _Requirements: 9.1, 1.3, 1.4, 8.3, 9.2_

  - [x] 11.2 Final checkpoint - Ensure all tests pass
    - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- Per the design's Testing Strategy, property-based testing is intentionally omitted. Tests are serialization/contract tests, example-based endpoint tests (run in Stub_Mode with `build_stub_client`, no AWS), DOM-level UI/integration tests, and one end-to-end smoke test.
- The feature adds no fit logic; all judgment comes from the existing `ai_fit` package, serialized field-for-field with the masked trace untouched.
- Each task references specific requirement clauses for traceability.
- Checkpoints ensure incremental validation at the service boundary and at the end.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "6.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "3.1", "3.2", "4.1", "6.2"] },
    { "id": 2, "tasks": ["2.2", "3.3", "4.2", "6.3", "7.1", "7.2", "8.1", "8.2", "9.1", "10.1"] },
    { "id": 3, "tasks": ["7.3", "8.3", "9.2"] },
    { "id": 4, "tasks": ["11.1"] }
  ]
}
```
