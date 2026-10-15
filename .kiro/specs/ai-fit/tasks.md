# Implementation Plan: AI Fit

## Overview

This plan builds the AI Fit agent in Python end-to-end on AWS Bedrock (Claude Opus via the Bedrock Converse API with tool use), backed by the six provided CSV datasets. The build order follows the design's layering: a pure data layer first, then the shared pure helpers (occasion normalization, measurement lookup, quality logic), then the deterministic Guardrail Validator, then the 10 Agent Tools, then the reasoning rubric / structured-output contract, then the orchestrator Bedrock loop, and finally the end-to-end demonstration scenario.

Testing is test-driven where practical. Property-based tests use **Hypothesis** and target the deterministic Guardrail Validator and the pure helper layers — each of the 18 Correctness Properties is a single property-based test at a minimum of 100 iterations, tagged `Feature: ai-fit, Property {number}: {property_text}`. Example/behavioral tests cover Claude's rubric adherence at temperature 0 plus an identical-signal stability check. Integration tests use a mocked Bedrock runtime and a mocked tool dispatcher. Smoke tests cover model wiring and the demo scenario.

## Tasks

- [x] 1. Set up project structure, config, and testing framework
  - Create the Python package layout (`ai_fit/` with `data/`, `tools/`, `reasoning/`, `orchestrator/`, `trace/` modules) and a `tests/` tree
  - Pin dependencies with exact versions: `boto3` (Bedrock Runtime), `hypothesis`, `pytest`; add a `requirements.txt` / `pyproject.toml`
  - Add a config module holding the Claude Opus Bedrock model id, region, `temperature=0`, the 10s tool timeout, and the 2-retry constants
  - Add dataset path constants pointing at the six CSVs in the workspace root
  - _Requirements: 9.1, 9.6_

- [x] 2. Build the data layer (CSV loader + in-memory store)
  - [x] 2.1 Implement CSV loaders and the in-memory store keyed for fast SKU and customer lookup
    - Load all six datasets: orders (`All OMS orders...`), OMS returns (`OMS Data...`), in-store exchanges (`McGill_Instore_return_exchanges`), reviews (`Synthetic_Customer_Reviews`), customer profiles (`ALDO_FIT_Final_Synthetic_Customer_Profiles`), product catalog (`Synthetic_product_skus`)
    - Build SKU-keyed and customer-keyed indexes
    - **Strip protected attributes (age, gender, ethnicity, nationality, location) at the source** so they are never materialized into model-visible records; retain `sizing_system` and product `gender_category`
    - _Requirements: 9.2, 10.2, 10.5, 10.6, 10.7_

  - [x] 2.2 Implement the data-model mappers from raw CSV columns to the design's typed records
    - Map `CustomerProfile`, `ProductCatalogEntry`, `SkuFitSignals` (OMS returns joined with exchanges on `Original Order Number` → `order_number`), `SkuReviewSignals`, and `QualityReturns` (quality counts and `units_sold` from the SAME OMS SKU key)
    - Normalize catalog `fit_guidance` free text to `runs small | runs large | true to size`; derive `offered_sizes` as the distinct sizes present for the style
    - Expose `foot_length_cm` / `foot_width_cm` only when `measurements_completed = 1`
    - _Requirements: 1.1, 1.11, 1.16, 3.1, 5.1, 5.10_

  - [x]* 2.3 Write unit tests for the data layer
    - Assert protected attributes are absent from every emitted record; assert the exchange join and the same-key quality/units join; assert `offered_sizes` derivation and `fit_guidance` normalization
    - _Requirements: 10.2, 10.6, 5.10, 1.16_

- [x] 3. Implement the Occasion Normalization Layer
  - [x] 3.1 Implement the shared, side-effect-free occasion mapping
    - Map profile tokens (`Everyday`, `School / University` → everyday wear; `Work` → work; `Going out`, `Special occasions` → special occasion), catalog tokens (`casual`, `travel` → everyday wear; `work` → work; `night out`, `wedding` → special occasion), and review tokens (same as catalog) to the canonical set `{everyday wear, work, special occasion}`
    - Split pipe-delimited catalog occasions and semicolon-delimited profile occasions; drop any unmapped token
    - _Requirements: 2.9, 2.10, 3.8_

  - [x]* 3.2 Write property test for occasion normalization
    - **Property 10: Occasion normalization is a total mapping with defined drops**
    - **Validates: Requirements 2.9, 2.10, 3.8**

- [x] 4. Implement pure sizing helpers used by the validator
  - [x] 4.1 Implement `measurement_lookup`, `clamp_to_offered`, half-size arithmetic, and the OMS dominance / confidence-tier helpers
    - `measurement_lookup(foot_length_cm, sizing_system)` using the design's monotonic half-size table (men's offset by sizing system)
    - `clamp_to_offered(size, offered_sizes)` snapping to the nearest offered half size
    - `half_steps_between` / `apply_half_steps`; OMS dominance (±20pp gap → one half size); confidence base tier (`<5` low, `5-14` medium, `>=15` high) and ordered tier adjustments on the `low=0,medium=1,high=2` scale clamped to `[0,2]`
    - _Requirements: 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.11, 1.14_

  - [x]* 4.2 Write property test for offered-range membership
    - **Property 8: Recommended size stays within the offered range**
    - **Validates: Requirements 1.8**

- [x] 5. Implement the Guardrail Validator (deterministic backstop)
  - [x] 5.1 Implement `validate(proposed_decision, signals) -> ValidatedDecision`
    - Enforce the six invariants per the design pseudocode: required-tool guard (reject), protected-attribute presence (reject), tool-failure short-circuit (Stated_Usual_Size + low), measurement precedence override, non-measurement net single half-size cap, offered-range clamp, confidence bounds + sparse/medium cap, and width/comfort recomputed as warning-only
    - Record every clamp/reject correction into the trace structure; expose the supporting-signal count including outcome signals
    - Pure and deterministic: no I/O, no randomness
    - _Requirements: 1.2, 1.4, 1.5, 1.6, 1.8, 1.11, 1.12, 1.13, 1.15, 1.17, 1.18, 1.19, 1.20, 3.4, 3.9, 3.11, 3.12, 3.13, 9.7, 10.1, 10.2, 10.6, 4.4_

  - [x]* 5.2 Write property test for validator determinism
    - **Property 1: Determinism of the validator for identical inputs**
    - **Validates: Requirements 10.4**

  - [x]* 5.3 Write property test for protected-attribute non-influence
    - **Property 2: Protected attributes never influence the output**
    - **Validates: Requirements 10.1, 10.2, 10.6**

  - [x]* 5.4 Write property test for the non-measurement half-size cap
    - **Property 3: Non-measurement size change is capped at one half size**
    - **Validates: Requirements 1.5, 1.6, 1.12, 1.13, 1.19**

  - [x]* 5.5 Write property test for OMS dominance direction
    - **Property 4: OMS dominance determines direction correctly**
    - **Validates: Requirements 1.5, 1.6, 1.7**

  - [x]* 5.6 Write property test for measurement precedence
    - **Property 5: Measurement precedence overrides all other size signals**
    - **Validates: Requirements 1.11**

  - [x]* 5.7 Write property test for confidence caps
    - **Property 6: Confidence never exceeds its caps**
    - **Validates: Requirements 1.4, 1.17, 1.18, 1.19, 1.20, 3.4**

  - [x]* 5.8 Write property test for width/comfort warning-only behavior
    - **Property 7: Width, shape, and comfort signals never change the size**
    - **Validates: Requirements 3.9, 3.11, 3.12, 3.13**

  - [x]* 5.9 Write property test for the required-tool guard
    - **Property 17: Recommendations require their mandatory tool calls**
    - **Validates: Requirements 9.7**

  - [x]* 5.10 Write property test for global validator compliance
    - **Property 18: The validator never allows a non-compliant recommendation through**
    - **Validates: Requirements 1.5, 1.6, 1.8, 1.11, 1.12, 1.13, 1.19, 1.20, 3.4, 10.1, 10.2, 10.6**

  - [x]* 5.11 Write property test for outcome-signal reinforcement
    - **Property 15: Outcome signals reinforce and are counted**
    - **Validates: Requirements 4.4**

- [x] 6. Checkpoint - Ensure data layer, helpers, and validator tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement the read Agent Tools (data access)
  - [x] 7.1 Implement `get_sku_fit_signals`, `get_customer_profile`, `get_product_catalog_entry`
    - Return the exact output shapes in the design's tool catalog; `get_customer_profile` returns protected-attribute-free records with `sizing_system`, `usual_shoe_size`, fit profile, comfort priority, normalized occasions, and optional foot measurement
    - `get_product_catalog_entry` returns `found=false` for absent SKUs
    - _Requirements: 1.1, 1.3, 1.5, 1.6, 1.7, 1.10, 1.11, 1.16, 2.1, 2.2, 8.1, 8.5, 8.6, 10.1, 10.7_

  - [x] 7.2 Implement `get_sku_reviews`, `get_recommendation_outcomes`, `get_quality_returns_and_units`
    - Review tool returns fit/width/occasion-cue counts; outcomes tool returns kept/returned breakdown; quality tool returns per-SKU quality counts, units sold, and reason breakdown over the same OMS key
    - _Requirements: 3.1, 3.3, 3.5, 3.9, 4.3, 4.4, 4.5, 5.1, 5.2, 5.10_

  - [x]* 7.3 Write unit tests for the read tools
    - Assert output shapes, protected-attribute absence from the profile tool, and the empty/absent-SKU paths
    - _Requirements: 2.1, 8.6, 10.1, 10.7_

- [x] 8. Implement the write/action Agent Tools and their logic
  - [x] 8.1 Implement the recommendation-outcome write path (`record_recommendation_outcome`) and outcome store
    - Write an initial `unknown` outcome linking SKU, recommended size, and order; mask direct identifiers (hash `order_number`); support update to kept / returned too big / returned too small / returned other; make updates reusable with no human approval
    - _Requirements: 4.1, 4.2, 4.5, 4.7_

  - [x]* 8.2 Write unit test for outcome identifier masking
    - **Property 14: Emitted artifacts always mask customer identifiers** (outcome-record portion)
    - **Validates: Requirements 4.7, 7.5, 10.3**

  - [x] 8.3 Implement `generate_quality_issue_report` and `get_quality_returns_and_units` report logic
    - Flag a SKU when `quality_count >= 5` OR (`units_sold >= 30` AND `rate >= 5%`); classify only the four quality reasons; rank descending by count with rate-only flags last; informational-only (no delist/reprice/remove); exclude customer identifiers; emit the explicit empty-case message
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10_

  - [x]* 8.4 Write property test for quality classification
    - **Property 11: Quality classification matches the reason whitelist**
    - **Validates: Requirements 5.2**

  - [x]* 8.5 Write property test for the quality flag predicate
    - **Property 12: Quality flag predicate**
    - **Validates: Requirements 5.3**

  - [x]* 8.6 Write property test for quality report ordering
    - **Property 13: Quality report ordering**
    - **Validates: Requirements 5.5**

  - [x] 8.7 Implement `route_to_human_reviewer` and the audit log
    - Record intent only (no order/pricing/payment mutation); append an audit entry with triggering request, routed action type, and timestamp; return `{routed, audit_id, timestamp}`
    - _Requirements: 11.1, 11.2, 11.3, 11.6_

  - [x] 8.8 Implement occasion matching and `find_alternative_styles`
    - Occasion match = set intersection of normalized customer and catalog occasions, naming matches; incorporate review occasion cues; alternative styles filter catalog-present (available) SKUs by requested occasion, rank by category closeness then smallest absolute price difference, cap at top 3, and handle the none-available case
    - _Requirements: 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.5, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x]* 8.9 Write property test for occasion matching
    - **Property 9: Occasion matching reflects set intersection**
    - **Validates: Requirements 2.3, 2.4**

  - [x]* 8.10 Write property test for alternative-style ranking and cap
    - **Property 16: Alternative styles are ranked and capped at three**
    - **Validates: Requirements 6.2**

- [x] 9. Checkpoint - Ensure all tool and property tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement the Reasoning Rubric and Structured Output Contract
  - [x] 10.1 Author the Claude Opus system prompt encoding the rubric
    - Encode signal precedence (measurement > OMS >= 5 > customer tendency bias > sparse catalog/review fallback), the dominance rule (±20pp), the net single half-size cap, the confidence arithmetic (base tier + ordered capped adjustments + sparse cap + high fit_return_rate temper), measurement precedence, and width/comfort as warning-only
    - Define the Structured Output Contract Claude must return (`recommended_size`, `confidence_level`, `size_rationale` citing ≥1 historical signal, `cited_signals[]`, optional `width_warning`, `comfort_note`)
    - _Requirements: 1.5, 1.6, 1.7, 1.11, 1.12, 1.13, 1.17, 1.18, 1.19, 1.20, 3.4, 3.12, 7.2_

  - [x] 10.2 Implement the structured-output parser/validator for Claude's returned object
    - Parse Claude's JSON decision into the `ProposedDecision` type consumed by the Guardrail Validator; handle malformed output defensively
    - _Requirements: 7.2, 9.3_

- [x] 11. Implement the Reasoning Trace Builder
  - [x] 11.1 Implement the ordered trace assembly with identifier masking
    - Record each reasoning step, each named tool invoked, data retrieved, and the decision in execution order; record validator corrections; mask `customer_uid` and `cnsmr_prof_sid`; produce a customer-facing explanation (plain language, cites ≥1 historical signal) and a full view for the Human_Reviewer; record failed tools and unavailable data
    - _Requirements: 7.1, 7.2, 7.5, 7.6, 11.5, 10.3_

  - [x]* 11.2 Write property test for trace identifier masking
    - **Property 14: Emitted artifacts always mask customer identifiers** (trace portion)
    - **Validates: Requirements 4.7, 7.5, 10.3**

- [x] 12. Implement the Orchestrator (Bedrock Converse tool-use loop)
  - [x] 12.1 Implement the tool dispatcher with timeout, retries, and the required-tool guard
    - Wrap every tool call with a 10s timeout and up to 2 retries; record failures to the trace; track invoked tools; enforce the required-tool guard (reject size/occasion recommendations missing their mandatory tool calls)
    - _Requirements: 8.7, 9.6, 9.7, 7.6_

  - [x] 12.2 Implement the Bedrock Converse loop at temperature 0 wiring tools, Claude, validator, and trace
    - Call Converse with the rubric system prompt, `toolConfig` for the 10 tools, and `temperature=0`; feed tool results back to Claude; receive the structured decision; pass it to the Guardrail Validator; build and mask the Reasoning_Trace; present confidence and explanation; present low-confidence recommendations as advisory
    - Handle Bedrock throttling (retry at most 2, then fail message) vs non-throttling errors (immediate failure message); decline + route direct financial requests
    - _Requirements: 9.1, 9.3, 9.4, 9.5, 11.2, 11.3, 11.4, 1.9, 1.10_

  - [x]* 12.3 Write integration tests with mocked Bedrock and mocked dispatcher
    - Assert throttling → retry-then-fail and non-throttling → immediate-fail; assert tool timeout/retry (at most 2) and the incomplete-recommendation path that preserves partial results and records the failed tool in the trace
    - _Requirements: 9.4, 9.5, 8.7, 9.6, 7.6_

  - [x]* 12.4 Write example/behavioral tests for Claude rubric adherence and stability
    - On representative scenarios (clear OMS lean, measurement present, sparse catalog-only, high fit_return_rate, width-only concern) assert correct direction/magnitude, confidence tier, measurement precedence, and warning-only width/comfort; add an identical-signal stability check through the full Claude → validator path
    - _Requirements: 10.4, 7.2, 1.11_

- [x] 13. Wire the end-to-end demonstration scenario
  - [x] 13.1 Implement the demo entry point producing a size recommendation, an occasion match result, and a visible masked Reasoning_Trace
    - Drive a representative SKU + customer through the full orchestrator path; surface the customer-facing explanation, confidence, occasion match, and the masked trace
    - _Requirements: 7.4, 7.1, 7.2, 7.3_

  - [x]* 13.2 Write smoke tests for model wiring and the demo scenario
    - Config check that the agent is wired to the Claude Opus model on the Bedrock runtime; end-to-end demo scenario producing a size recommendation, an occasion match result, and a visible Reasoning_Trace
    - _Requirements: 9.1, 7.4_

- [x] 14. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster hackathon MVP; core implementation tasks are never optional.
- Each of the 18 Correctness Properties is implemented as a single Hypothesis property-based test at a minimum of 100 iterations, tagged `Feature: ai-fit, Property {number}: {property_text}`.
- Property tests are placed close to the code they validate (validator, helpers, tools) to catch errors early. Example/behavioral and integration tests cover the LLM and Bedrock surfaces, which are not amenable to PBT.
- Each task references the specific requirements it implements for traceability.
- The availability proxy (catalog-present = available) is used throughout; real-time inventory is future work per the design's Open Item.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "3.1", "4.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.2", "4.2"] },
    { "id": 3, "tasks": ["5.1"] },
    { "id": 4, "tasks": ["5.2", "5.3", "5.4", "5.5", "5.6", "5.7", "5.8", "5.9", "5.10", "5.11"] },
    { "id": 5, "tasks": ["7.1", "7.2", "8.1", "8.3", "8.7", "8.8"] },
    { "id": 6, "tasks": ["7.3", "8.2", "8.4", "8.5", "8.6", "8.9", "8.10"] },
    { "id": 7, "tasks": ["10.1", "10.2", "11.1"] },
    { "id": 8, "tasks": ["11.2", "12.1"] },
    { "id": 9, "tasks": ["12.2"] },
    { "id": 10, "tasks": ["12.3", "12.4", "13.1"] },
    { "id": 11, "tasks": ["13.2"] }
  ]
}
```
