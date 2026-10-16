# Requirements Document

## Introduction

AI Fit Demo UI is a browser-based demonstration surface for the existing AI Fit agent. It presents a simulated ALDO product detail page (PDP) and layers the agent's size recommendation, occasion match, and masked reasoning trace onto that page through an "ALDO FIT" assistant panel, so a hackathon audience can watch the agent reason over a realistic storefront interaction.

The UI does not re-implement any fit logic. All recommendations come from the existing `ai_fit` orchestrator (`ConverseOrchestrator.produce_recommendation`) and its `Recommendation` result object, plus the existing `match_occasion` and `find_alternative_styles` tools. This spec covers only the presentation surface and the thin HTTP layer that exposes the agent to a web page.

The demo is explicitly a prototype, not a real storefront, and must say so on screen. It runs a single scripted flow driven by a visible on-page guide: select size 7, add to bag, see a fit warning, choose to find alternatives, and expand the agent's simulated activity (the reasoning trace). The surface must preserve the Responsible-AI properties the agent already guarantees — in particular, no Protected_Attribute is shown, and the reasoning trace shown to the audience is the masked trace with direct customer identifiers redacted.

## Glossary

- **Demo_UI**: The browser-based product detail page and AI Fit assistant panel that this spec defines.
- **PDP**: The simulated ALDO product detail page the Demo_UI renders, including product imagery, price, color, and the size grid.
- **Fit_Panel**: The "ALDO FIT" assistant panel on the PDP that triggers and displays the agent's fit insight, warnings, alternatives, and reasoning trace.
- **Demo_Guide**: The on-page strip listing the ordered steps of the scripted demonstration (select size, add to bag, show fit warning, find alternatives, expand activity), which can be hidden.
- **Recommendation_Service**: The thin HTTP layer that wraps the existing AI Fit agent and returns its results to the Demo_UI as JSON.
- **Recommendation**: The existing `ai_fit.orchestrator.converse.Recommendation` object produced by `ConverseOrchestrator.produce_recommendation`, carrying `ok`, `recommended_size`, `confidence`, `advisory`, `explanation`, `supporting_signal_count`, `warnings`, `note`, `trace`, and `failure_reason`.
- **AI_Fit_Agent**: The existing agent defined by the `ai-fit` spec; this spec treats it as a dependency and does not modify its reasoning.
- **Masked_Reasoning_Trace**: The `trace` field of a Recommendation — the ordered reasoning steps, tools invoked, and validator corrections, with `customer_uid` and `cnsmr_prof_sid` redacted, as produced by the existing trace builder.
- **Occasion_Match_Result**: The dict returned by the existing `match_occasion` tool for the browsed SKU and the demo customer's occasion.
- **Alternative_Styles_Result**: The dict returned by the existing `find_alternative_styles` tool, listing up to three occasion-matching alternative products.
- **Fit_Warning**: A width/comfort or advisory message surfaced to the shopper, derived from the Recommendation's `warnings`, `advisory`, and `note` fields.
- **Protected_Attribute**: As defined by the `ai-fit` spec — age, gender, ethnicity, nationality, and location — which must never be displayed.
- **Prototype_Disclaimer**: On-screen text stating the page is an unofficial hackathon prototype and not an ALDO storefront.
- **Stub_Mode**: A run mode in which the Recommendation_Service uses the existing stubbed Bedrock client and a small fixture dataset, so the demo runs with no live AWS access and no production-CSV load.

## Requirements

### Requirement 1: Simulated product detail page

**User Story:** As a hackathon presenter, I want a product detail page that looks like an ALDO storefront, so that the demonstration is credible and recognizable.

#### Acceptance Criteria

1. THE Demo_UI SHALL render a PDP for a single configured product, including the product name, price, color options, and product imagery.
2. THE Demo_UI SHALL render a size grid showing the product's offered sizes as selectable controls.
3. WHEN the Demo_UI loads, THE Demo_UI SHALL display the Prototype_Disclaimer stating the page is an unofficial hackathon prototype and not an ALDO storefront.
4. THE Demo_UI SHALL display a Demo_Guide listing the ordered demonstration steps, and SHALL provide a control to hide the Demo_Guide.
5. THE Demo_UI SHALL NOT display any Protected_Attribute of the demo customer anywhere on the page.

### Requirement 2: Size selection and fit insight trigger

**User Story:** As a shopper on the PDP, I want to select my size and get a fit insight, so that I know whether this size will fit before adding to bag.

#### Acceptance Criteria

1. WHEN a user selects a size in the size grid, THE Demo_UI SHALL record the selected size as the Stated_Usual_Size for the fit request.
2. THE Fit_Panel SHALL display the demo customer's saved fit profile summary (usual size and comfort priority) without revealing any Protected_Attribute.
3. WHEN the user activates the fit-insight control, THE Demo_UI SHALL send a recommendation request to the Recommendation_Service carrying the browsed SKU, the selected size, the demo customer identifier, and the demo occasion.
4. WHILE a recommendation request is in flight, THE Demo_UI SHALL show a pending state and SHALL NOT present a prior result as the current one.
5. IF no size is selected when the user activates the fit-insight control, THEN THE Demo_UI SHALL prompt the user to select a size and SHALL NOT send a recommendation request.

### Requirement 3: Displaying the size recommendation

**User Story:** As a shopper, I want to see the recommended size and how confident the assistant is, so that I can decide what to buy.

#### Acceptance Criteria

1. WHEN the Recommendation_Service returns a Recommendation with `ok` true, THE Fit_Panel SHALL display the `recommended_size`, the `confidence` level, and the plain-language `explanation`.
2. WHEN a Recommendation's `advisory` field is true, THE Fit_Panel SHALL present the recommendation as advisory rather than definitive.
3. THE Fit_Panel SHALL display the `supporting_signal_count` as the amount of historical evidence behind the recommendation.
4. WHEN a Recommendation includes a non-empty `warnings` list or a `note`, THE Fit_Panel SHALL display each warning and the note as a Fit_Warning.
5. WHEN the recommended size differs from the user's selected size, THE Fit_Panel SHALL make the difference visible to the shopper.

### Requirement 4: Fit warning and the add-to-bag flow

**User Story:** As a shopper about to add a shoe to my bag, I want to be warned if the fit or occasion is a concern, so that I can reconsider before buying.

#### Acceptance Criteria

1. THE Demo_UI SHALL provide an "Add to Bag" control on the PDP.
2. WHEN the user adds the product to the bag and a Fit_Warning is present for the selected size, THE Demo_UI SHALL display the Fit_Warning at the point of adding to bag.
3. WHEN a Fit_Warning is displayed, THE Demo_UI SHALL offer the user an option to find alternative styles.
4. THE Demo_UI SHALL allow the user to proceed with the original selection despite a Fit_Warning.

### Requirement 5: Occasion match and alternative styles

**User Story:** As a shopper, I want to be shown other styles that suit my occasion when this one does not, so that I can still find something appropriate.

#### Acceptance Criteria

1. WHEN the user chooses to find alternatives, THE Demo_UI SHALL request an Alternative_Styles_Result from the Recommendation_Service for the browsed SKU and the demo occasion.
2. WHEN an Alternative_Styles_Result contains one to three alternatives, THE Demo_UI SHALL display each alternative's product name, price, and suited occasions.
3. WHEN an Alternative_Styles_Result reports none available, THE Demo_UI SHALL display an explicit "no alternatives currently available" message.
4. THE Demo_UI SHALL display the Occasion_Match_Result for the browsed product, naming whether and which occasion matched.
5. THE Demo_UI SHALL present at most three alternative styles, matching the agent's cap.

### Requirement 6: Masked reasoning trace ("simulated activity")

**User Story:** As a hackathon judge, I want to expand the assistant's reasoning and see which data it used, so that I can evaluate its transparency and that it respects privacy.

#### Acceptance Criteria

1. THE Fit_Panel SHALL provide an expandable "activity" control that, when expanded, displays the Masked_Reasoning_Trace from the Recommendation's `trace` field.
2. THE Demo_UI SHALL display the trace's ordered steps, the tools invoked, and any validator corrections.
3. THE Demo_UI SHALL NOT display the raw values of `customer_uid` or `cnsmr_prof_sid`; where the trace masked an identifier, THE Demo_UI SHALL show the redaction placeholder as received.
4. WHERE the trace records a failed tool or unavailable data, THE Demo_UI SHALL show that the tool failed and the data was unavailable.
5. THE activity control SHALL default to collapsed so the shopper view is uncluttered until expanded.

### Requirement 7: The recommendation HTTP service

**User Story:** As the Demo_UI, I want a simple endpoint that returns the agent's result as JSON, so that the page can render it without embedding agent logic.

#### Acceptance Criteria

1. THE Recommendation_Service SHALL expose an endpoint that accepts a browsed SKU, a selected size, a customer identifier, and an occasion, and returns a Recommendation and an Occasion_Match_Result as JSON.
2. THE Recommendation_Service SHALL expose an endpoint that accepts a browsed SKU and an occasion and returns an Alternative_Styles_Result as JSON.
3. THE Recommendation_Service SHALL load the agent's data store and wiring once at startup and reuse it across requests, and SHALL NOT reload the data store per request.
4. THE Recommendation_Service SHALL serialize the Recommendation using its existing fields without adding, removing, or altering any Protected_Attribute, and without unmasking the trace.
5. THE Recommendation_Service SHALL serve the Demo_UI static assets so the demo runs from a single process.

### Requirement 8: Graceful failure and resilience

**User Story:** As a presenter, I want the demo to behave sanely when the agent cannot produce a result, so that a live failure does not break the presentation.

#### Acceptance Criteria

1. WHEN the Recommendation_Service returns a Recommendation with `ok` false, THE Fit_Panel SHALL display the recommendation's customer-facing failure message derived from `failure_reason` and SHALL NOT show a size or confidence as if one were produced.
2. IF the Recommendation_Service is unreachable or returns an error, THEN THE Demo_UI SHALL display a non-blocking error state and SHALL allow the user to retry.
3. WHEN running in Stub_Mode, THE Recommendation_Service SHALL produce a Recommendation without any live AWS access and without loading the production datasets.
4. THE Demo_UI SHALL remain usable for size selection and add to bag even when a recommendation request fails.

### Requirement 9: Deployment and run mode

**User Story:** As a developer running the demo, I want to start it with a single command, so that setup is fast during a hackathon.

#### Acceptance Criteria

1. THE Recommendation_Service SHALL be startable with a single documented command.
2. THE Recommendation_Service SHALL support a Stub_Mode flag or environment variable selecting the stubbed agent and fixture data for an offline demo.
3. WHERE live mode is selected, THE Recommendation_Service SHALL use the existing Bedrock-backed agent wiring unchanged.
4. THE Recommendation_Service SHALL depend only on the existing `ai_fit` package plus a single documented web framework dependency pinned to an exact version.
