# Requirements Document

## Introduction

AI Fit is an agentic AI assistant that helps ALDO customers find their "better fit" while browsing a product page. The assistant uses an agentic reasoning approach (not a rules engine) built on AWS Bedrock with Claude Opus as the reasoning model, equipped with tools that query historical order, return, and exchange data plus synthetic customer, review, and product datasets.

AI Fit addresses two customer-facing goals and one business goal. For customers, it recommends a better size for a specific product by learning from historical fit-related return and exchange signals, and it checks whether a product matches the occasion the customer wants it for, recommending alternative styles when it does not. For the business, it aims to improve gross margin by reducing fit-related returns and restocking costs, and to improve customer satisfaction through better-fitting, occasion-appropriate recommendations.

Because the feature is evaluated by hackathon judges against Responsible AI criteria, the assistant must make its reasoning, decisions, and actions visible, and must satisfy explicit requirements for fairness, privacy, bias mitigation, explainability, bounded financial authority, and human oversight. The assistant must also degrade gracefully when historical data is sparse or when a customer has no profile. The numeric thresholds in these requirements (the minimum signal gate, the dominance gap, and the confidence tiers) were derived from analysis of the Feb 2024 - Feb 2026 return dataset rather than chosen arbitrarily, and are expected to be re-tuned as outcome data accumulates.

## Glossary

- **AI_Fit_Agent**: The agentic AI assistant, orchestrated on AWS Bedrock using Claude Opus as the reasoning model, that produces size and occasion recommendations and exposes its reasoning.
- **Bedrock_Runtime**: The AWS Bedrock service runtime that hosts the Claude Opus model and executes the agent's tool invocations.
- **Agent_Tool**: A data-access function the AI_Fit_Agent can invoke to retrieve records from a specific data source (orders, returns, exchanges, customer profile, reviews, or product catalog).
- **SKU**: A stock-keeping unit identifying a specific product variant, matching the `sku` field in the order and return datasets.
- **Fit_Return_Signal**: A historical return record whose return reason indicates a fit problem, specifically a reason value of "Too big", "Too small", or "Uncomfortable" (including quoting variants of those values present in the data).
- **Exchange_Signal**: A historical in-store exchange record from the exchange dataset that links an original order to an exchanged SKU and quantity.
- **Stated_Usual_Size**: The size the customer declares as their normal size during the AI Fit interaction, or the size read from the customer profile.
- **Recommended_Size**: The size the AI_Fit_Agent proposes for a given SKU after analyzing fit and exchange signals.
- **Occasion**: A customer-usage category for footwear, limited to "everyday wear", "work", or "special occasion".
- **Customer_Profile**: A synthetic record describing a customer, including the customer's usual_shoe_size, Sizing_System, Customer_Fit_Profile, Comfort_Style_Priority, preferred shopping occasions, and optional Foot_Measurement, keyed to synthetic_customer_id.
- **Sizing_System**: The shoe sizing scale a customer's usual_shoe_size is expressed in, either "Women's sizing" or "Men's sizing", read from the Customer_Profile. The Sizing_System is a sizing-scale attribute, not the customer's personal gender, and is a legitimate recommendation input.
- **Customer_Fit_Profile**: The customer's self-reported fit tendencies from the Customer_Profile, including whether shoes often feel too big or too small, too narrow or too wide, tight around the toes, or heels slip, and whether the customer reports no fit issues or is unsure.
- **Comfort_Style_Priority**: The customer's stated preference from the Customer_Profile, one of "comfort", "style", or "a balance of both".
- **Foot_Measurement**: The customer's measured foot_length_cm and, where present, foot_width_cm from the Customer_Profile, available only when the customer completed measurement; used as a high-precision fit signal.
- **Product_Catalog**: The synthetic product SKU dataset (keyed by `sku`) describing each product's attributes, including category, price, and the occasions the product is suited for. The catalog's occasion tokens ("work", "casual", "night out", "wedding", "travel") are mapped to the canonical Occasion set using the following mapping: "casual" and "travel" map to "everyday wear"; "work" maps to "work"; "night out" and "wedding" map to "special occasion".
- **Catalog_Fit_Guidance**: A per-SKU fit direction from the Product_Catalog fit_guidance field, normalized to "runs small", "runs large", or "true to size".
- **Catalog_Fit_Return_Rate**: A per-SKU numeric return rate from the Product_Catalog fit_return_rate field, used as a reliability weight for the SKU's fit guidance.
- **Catalog_Toe_Box**: A per-SKU width/shape attribute from the Product_Catalog toe_box field, one of "regular", "narrow", or "roomy".
- **Review_Data**: The synthetic customer review dataset containing customer feedback associated with products.
- **Review_Fit_Signal**: A fit indication extracted from Review_Data for a SKU, classified as "runs small", "runs large", or "true to size", derived from review text and any structured fit rating present in the review.
- **Review_Width_Signal**: A width indication extracted from the width_feedback field of Review_Data for a SKU, classified as "normal", "narrow", or "wide".
- **Review_Occasion_Cue**: An Occasion indication derived from the occasion field of Review_Data for a SKU and mapped to the canonical Occasion set using the following mapping: "casual" and "travel" map to "everyday wear"; "work" maps to "work"; "night out" and "wedding" map to "special occasion". Review occasion values with no defined mapping are ignored.
- **Recommendation_Outcome**: A record linking a size recommendation the AI_Fit_Agent made to the subsequent result for that order, classified as "kept", "returned too big", "returned too small", "returned other", or "unknown".
- **Outcome_Fit_Signal**: A fit indication derived from Recommendation_Outcomes for a SKU, used as an additional fit signal when generating future size recommendations.
- **Quality_Return_Signal**: A historical return record whose return reason indicates a product defect rather than a fit or preference issue, specifically a reason value of "Defective", "Poor quality", "Arrived in poor condition", or "Manufacturer defect" (including quoting and casing variants present in the data). Based on analysis of the Feb 2024 - Feb 2026 return dataset, these reasons total approximately 27,870 returns across 17,770 SKUs.
- **Units_Sold**: The total shipped quantity for a SKU, derived from the orders dataset ("All OMS orders"), used as the denominator for the Quality_Return_Rate. Units_Sold and Quality_Return_Signal counts are computed from the same OMS SKU key so the rate is a valid ratio.
- **Quality_Return_Rate**: The count of Quality_Return_Signals for a SKU divided by its Units_Sold, expressed as a percentage. The rate is only applied as a flag criterion for SKUs meeting a minimum Units_Sold floor, which is set to 30 based on the OMS units-sold distribution for SKUs with quality returns (the 25th percentile is 30).
- **Quality_Issue_Report**: A report produced for the product team listing SKUs flagged for potential product quality issues, including each flagged SKU, its Quality_Return_Signal count, its Units_Sold, its Quality_Return_Rate, and the breakdown of quality reasons.
- **Product_Team**: The ALDO internal team responsible for product quality decisions that receives the Quality_Issue_Report.
- **Alternative_Style**: A different product recommended by the AI_Fit_Agent that matches the customer's requested Occasion when the browsed product does not.
- **Reasoning_Trace**: A human-readable record of the AI_Fit_Agent's reasoning steps, the Agent_Tools invoked, the data retrieved, and the resulting decision.
- **Confidence_Level**: A qualitative label ("high", "medium", or "low") the AI_Fit_Agent assigns to a recommendation based on the volume and consistency of supporting data.
- **Minimum_Signal_Threshold**: The minimum count of Fit_Return_Signals plus Exchange_Signals for a SKU required before the AI_Fit_Agent issues a size recommendation other than the Stated_Usual_Size, set to 5 based on analysis of the Feb 2024 - Feb 2026 return dataset (median fit signals per SKU is 2; a threshold of 5 balances coverage against evidence sufficiency).
- **Protected_Attribute**: A customer attribute that must not influence recommendations, including age, gender, ethnicity, nationality, and location.
- **Human_Reviewer**: An ALDO staff member who reviews or overrides AI_Fit_Agent actions where human oversight is required.

## Requirements

### Requirement 1: Size Recommendation from Fit Return Signals

**User Story:** As an ALDO customer browsing a product page, I want the assistant to recommend the size most likely to fit for this specific product, so that I receive a shoe that fits and avoid a return.

#### Acceptance Criteria

1. WHEN a customer provides a Stated_Usual_Size for a SKU, THE AI_Fit_Agent SHALL retrieve the Fit_Return_Signals and Exchange_Signals associated with that SKU through an Agent_Tool.
2. IF the Agent_Tool retrieving Fit_Return_Signals and Exchange_Signals fails to return data, THEN THE AI_Fit_Agent SHALL recommend the Stated_Usual_Size, report a Confidence_Level of "low", and state that fit history could not be retrieved.
3. WHILE the count of Fit_Return_Signals plus Exchange_Signals for a SKU is below 5, THE AI_Fit_Agent SHALL recommend the Stated_Usual_Size and report a Confidence_Level of "low".
4. WHEN the count of Fit_Return_Signals plus Exchange_Signals for a SKU is at least 5 and below 15, THE AI_Fit_Agent SHALL report a Confidence_Level of "medium"; WHEN the count is at least 15, THE AI_Fit_Agent SHALL report a Confidence_Level of "high".
5. WHEN the proportion of "Too big" Fit_Return_Signals for a SKU exceeds the proportion of "Too small" Fit_Return_Signals by at least 20 percentage points, THE AI_Fit_Agent SHALL recommend a Recommended_Size that is one half size smaller than the Stated_Usual_Size.
6. WHEN the proportion of "Too small" Fit_Return_Signals for a SKU exceeds the proportion of "Too big" Fit_Return_Signals by at least 20 percentage points, THE AI_Fit_Agent SHALL recommend a Recommended_Size that is one half size larger than the Stated_Usual_Size.
7. IF neither "Too big" nor "Too small" Fit_Return_Signals exceed the other by at least 20 percentage points for a SKU, THEN THE AI_Fit_Agent SHALL recommend the Stated_Usual_Size.
8. IF a computed Recommended_Size falls below the smallest size offered for the SKU or above the largest size offered for the SKU, THEN THE AI_Fit_Agent SHALL clamp the Recommended_Size to the nearest offered size boundary.
9. IF the Recommended_Size is out of stock for the SKU, THEN THE AI_Fit_Agent SHALL indicate that the recommended size is unavailable and SHALL present the nearest in-stock size.
10. WHEN the AI_Fit_Agent issues a Recommended_Size, THE AI_Fit_Agent SHALL report the count of supporting Fit_Return_Signals and Exchange_Signals used in the decision.
11. WHEN a Foot_Measurement is present in the Customer_Profile for the customer, THE AI_Fit_Agent SHALL treat the Foot_Measurement as the highest-precision fit signal and MAY override a size derived from SKU-level signals, recording the override in the Reasoning_Trace.
12. WHEN the Customer_Fit_Profile indicates the customer usually finds shoes too big AND the SKU's Fit_Return_Signals do not already indicate the SKU runs small, THE AI_Fit_Agent SHALL bias the Recommended_Size toward one half size smaller than the Stated_Usual_Size.
13. WHEN the Customer_Fit_Profile indicates the customer usually finds shoes too small AND the SKU's Fit_Return_Signals do not already indicate the SKU runs large, THE AI_Fit_Agent SHALL bias the Recommended_Size toward one half size larger than the Stated_Usual_Size.
14. THE AI_Fit_Agent SHALL interpret the Stated_Usual_Size in the context of the Customer_Profile Sizing_System when deriving a Recommended_Size.
15. WHEN the AI_Fit_Agent applies a Customer_Fit_Profile or Foot_Measurement signal to a recommendation, THE AI_Fit_Agent SHALL record that customer-side signal in the Reasoning_Trace.
16. WHEN the AI_Fit_Agent produces a size recommendation for a SKU, THE AI_Fit_Agent SHALL retrieve the SKU's Catalog_Fit_Guidance and Catalog_Fit_Return_Rate from the Product_Catalog through an Agent_Tool.
17. WHEN the count of Fit_Return_Signals plus Exchange_Signals for a SKU is at or above the Minimum_Signal_Threshold AND the Catalog_Fit_Guidance agrees with the direction indicated by the SKU's Fit_Return_Signals, THE AI_Fit_Agent SHALL raise the Confidence_Level by at most one tier and SHALL NOT change the Recommended_Size.
18. WHEN the count of Fit_Return_Signals plus Exchange_Signals for a SKU is at or above the Minimum_Signal_Threshold AND the Catalog_Fit_Guidance contradicts the direction indicated by the SKU's Fit_Return_Signals, THE AI_Fit_Agent SHALL lower the Confidence_Level by at most one tier, SHALL NOT change the Recommended_Size, and SHALL note the conflicting signals in the explanation.
19. WHILE the count of Fit_Return_Signals plus Exchange_Signals for a SKU is below the Minimum_Signal_Threshold, THE AI_Fit_Agent SHALL allow the Catalog_Fit_Guidance to determine the Recommended_Size as follows: "runs small" recommends one half size larger than the Stated_Usual_Size, "runs large" recommends one half size smaller, and "true to size" recommends the Stated_Usual_Size, and SHALL report a Confidence_Level no higher than "medium".
20. WHEN the Catalog_Fit_Return_Rate for a SKU is high relative to typical SKUs, THE AI_Fit_Agent SHALL treat the SKU's Catalog_Fit_Guidance as less reliable and SHALL temper the Confidence_Level accordingly.
21. WHEN the AI_Fit_Agent uses Catalog_Fit_Guidance or Catalog_Fit_Return_Rate in a recommendation, THE AI_Fit_Agent SHALL record the catalog-derived fit signals used in the Reasoning_Trace.

### Requirement 2: Occasion Matching

**User Story:** As an ALDO customer, I want the assistant to tell me whether a shoe suits the occasion I need it for, so that I buy a product appropriate for how I plan to wear it.

#### Acceptance Criteria

1. WHEN a customer begins an AI Fit interaction for a SKU, THE AI_Fit_Agent SHALL retrieve the customer's preferred Occasion values from the Customer_Profile through an Agent_Tool, where each value is one of "everyday wear", "work", or "special occasion".
2. WHEN the AI_Fit_Agent has at least one customer Occasion and a SKU, THE AI_Fit_Agent SHALL retrieve the SKU's suited Occasion categories from the Product_Catalog through an Agent_Tool, where each category is one of "everyday wear", "work", or "special occasion".
3. WHEN at least one of the customer's preferred Occasion values is included in the SKU's suited Occasion categories, THE AI_Fit_Agent SHALL inform the customer that the product matches the requested Occasion, naming each matching Occasion.
4. IF none of the customer's preferred Occasion values is included in the SKU's suited Occasion categories, THEN THE AI_Fit_Agent SHALL inform the customer that the product does not match the requested Occasion.
5. WHERE a customer specifies one or more Occasion values directly during the interaction, THE AI_Fit_Agent SHALL use only the directly specified Occasion values in place of the Customer_Profile Occasion values for occasion matching.
6. IF no Occasion value is available from either the Customer_Profile or a direct customer specification, THEN THE AI_Fit_Agent SHALL request that the customer provide an Occasion and SHALL NOT report a match or no-match result until an Occasion is provided.
7. IF an Agent_Tool retrieval of the customer's preferred Occasion or the SKU's suited Occasion categories fails or returns no result, THEN THE AI_Fit_Agent SHALL inform the customer that occasion matching is unavailable and SHALL NOT report a match or no-match result for that interaction.
8. IF a customer-provided or retrieved Occasion value is not one of "everyday wear", "work", or "special occasion", THEN THE AI_Fit_Agent SHALL exclude that value from occasion matching and SHALL inform the customer that the value is not a recognized Occasion.
9. WHEN the AI_Fit_Agent reads preferred occasions from the Customer_Profile, THE AI_Fit_Agent SHALL map the profile occasion tokens to the canonical Occasion set as follows: "Everyday" and "School / University" to "everyday wear"; "Work" to "work"; "Going out" and "Special occasions" to "special occasion".
10. WHEN the AI_Fit_Agent reads a SKU's suited occasions from the Product_Catalog, THE AI_Fit_Agent SHALL map the catalog occasion tokens to the canonical Occasion set as follows: "casual" and "travel" to "everyday wear"; "work" to "work"; "night out" and "wedding" to "special occasion".

### Requirement 3: Review-Informed Fit and Occasion Signals

**User Story:** As an ALDO customer, I want the assistant to factor in what other customers said about a product's fit and best use, so that I get a better recommendation even when there is little return history.

#### Acceptance Criteria

1. WHEN the AI_Fit_Agent produces a size recommendation for a SKU, THE AI_Fit_Agent SHALL retrieve the SKU's Review_Fit_Signals from Review_Data through an Agent_Tool.
2. WHEN the count of Fit_Return_Signals plus Exchange_Signals for a SKU is at or above the Minimum_Signal_Threshold, THE AI_Fit_Agent SHALL base the Recommended_Size on the Fit_Return_Signals and Exchange_Signals and SHALL use Review_Fit_Signals only to adjust the Confidence_Level and enrich the explanation.
3. WHILE the count of Fit_Return_Signals plus Exchange_Signals for a SKU is below the Minimum_Signal_Threshold AND at least 5 Review_Fit_Signals exist for the SKU, THE AI_Fit_Agent SHALL allow the Review_Fit_Signals to determine the Recommended_Size as follows: WHEN "runs small" is the majority Review_Fit_Signal, recommend one half size larger than the Stated_Usual_Size; WHEN "runs large" is the majority Review_Fit_Signal, recommend one half size smaller than the Stated_Usual_Size; otherwise recommend the Stated_Usual_Size.
4. WHEN a Review_Fit_Signal-driven size recommendation is issued, THE AI_Fit_Agent SHALL report a Confidence_Level no higher than "medium" and SHALL state that the recommendation is based on customer reviews rather than return history.
5. WHEN the AI_Fit_Agent performs occasion matching for a SKU, THE AI_Fit_Agent SHALL incorporate Review_Occasion_Cues as supporting evidence for the SKU's suited Occasion categories and SHALL cite them in the explanation when used.
6. IF Review_Data contains no reviews for a SKU, THEN THE AI_Fit_Agent SHALL proceed using only Fit_Return_Signals and Exchange_Signals and SHALL note in the explanation that no reviews were available.
7. WHEN the AI_Fit_Agent uses Review_Fit_Signals or Review_Occasion_Cues in a recommendation, THE AI_Fit_Agent SHALL record the review-derived signals used in the Reasoning_Trace.
8. WHEN the AI_Fit_Agent derives a Review_Occasion_Cue from Review_Data, THE AI_Fit_Agent SHALL map the review occasion value to the canonical Occasion set as follows: "casual" and "travel" to "everyday wear", "work" to "work", "night out" and "wedding" to "special occasion", and SHALL ignore any review occasion value that has no defined mapping.
9. WHEN the AI_Fit_Agent produces a size recommendation for a SKU AND at least 3 Review_Width_Signals exist for the SKU with a majority value of "narrow" or "wide", THE AI_Fit_Agent SHALL present a width fit warning stating the majority width tendency, and SHALL NOT change the Recommended_Size based on the Review_Width_Signal.
10. WHEN the AI_Fit_Agent presents a width fit warning, THE AI_Fit_Agent SHALL record the Review_Width_Signals used in the Reasoning_Trace.
11. WHEN deciding whether to present a width fit warning for a SKU, THE AI_Fit_Agent SHALL combine the Customer_Fit_Profile width indications (too narrow, too wide, tight around the toes, heels slip) with the SKU's Review_Width_Signals, and SHALL present a width fit warning when either source indicates a width or shape concern.
12. WHEN the AI_Fit_Agent produces a recommendation, THE AI_Fit_Agent SHALL incorporate the Comfort_Style_Priority into the explanation and MAY use it to prioritize comfort-oriented or style-oriented guidance, without changing the Recommended_Size.
13. WHEN deciding whether to present a width fit warning for a SKU, THE AI_Fit_Agent SHALL include the SKU's Catalog_Toe_Box alongside the Customer_Fit_Profile width indications and the Review_Width_Signals, and SHALL present a width fit warning when the Catalog_Toe_Box is "narrow" or "roomy" or when either other source indicates a width or shape concern.

### Requirement 4: Recommendation Outcome Learning Loop

**User Story:** As the business, I want the assistant to learn from whether its past recommendations were kept or returned, so that its future recommendations get more accurate over time.

#### Acceptance Criteria

1. WHEN the AI_Fit_Agent issues a Recommended_Size for an order, THE AI_Fit_Agent SHALL create a Recommendation_Outcome record linking the recommendation to the SKU, the recommended size, and the order, with an initial outcome of "unknown".
2. WHEN a return or exchange record for an order with an associated Recommendation_Outcome becomes available through an Agent_Tool, THE AI_Fit_Agent SHALL update the Recommendation_Outcome to "kept", "returned too big", "returned too small", or "returned other" based on the return reason.
3. WHEN the AI_Fit_Agent generates a size recommendation for a SKU, THE AI_Fit_Agent SHALL retrieve the SKU's Outcome_Fit_Signals derived from Recommendation_Outcomes through an Agent_Tool and SHALL include them as an additional fit signal.
4. THE AI_Fit_Agent SHALL treat Outcome_Fit_Signals as reinforcing the Fit_Return_Signals, Exchange_Signals, and Review_Fit_Signals rather than overriding them, and SHALL count Outcome_Fit_Signals toward the supporting-signal count reported with a recommendation.
5. WHEN an updated Recommendation_Outcome becomes available, THE AI_Fit_Agent SHALL make it available for the next recommendation it generates for the same SKU without requiring human approval.
6. WHEN the AI_Fit_Agent uses Outcome_Fit_Signals in a recommendation, THE AI_Fit_Agent SHALL record in the Reasoning_Trace that prior recommendation outcomes influenced the decision and the count of outcomes used.
7. WHEN the AI_Fit_Agent records or updates a Recommendation_Outcome, THE AI_Fit_Agent SHALL mask the direct customer identifiers so that an individual customer cannot be re-identified from the Recommendation_Outcome.

### Requirement 5: Product Quality Issue Reporting

**User Story:** As a member of the ALDO product team, I want a report of shoes that are being returned for quality defects rather than fit, so that I can investigate and act on product quality problems.

#### Acceptance Criteria

1. WHEN the Product_Team requests a Quality_Issue_Report, THE AI_Fit_Agent SHALL retrieve Quality_Return_Signals and Units_Sold for all SKUs through an Agent_Tool.
2. THE AI_Fit_Agent SHALL classify a return as a Quality_Return_Signal only when its reason is one of "Defective", "Poor quality", "Arrived in poor condition", or "Manufacturer defect", and SHALL exclude fit-related and preference-related return reasons from the Quality_Issue_Report.
3. WHEN generating a Quality_Issue_Report, THE AI_Fit_Agent SHALL flag a SKU when its Quality_Return_Signal count is at least 5, OR when its Units_Sold is at least 30 and its Quality_Return_Rate is at least 5 percent.
4. WHEN the AI_Fit_Agent flags a SKU in the Quality_Issue_Report, THE AI_Fit_Agent SHALL include the SKU, its Quality_Return_Signal count, its Units_Sold, its Quality_Return_Rate, and the count of returns per quality reason.
5. THE AI_Fit_Agent SHALL rank the Quality_Issue_Report by Quality_Return_Signal count in descending order, and SHALL present SKUs flagged solely by Quality_Return_Rate after count-flagged SKUs.
6. WHEN a SKU's Quality_Return_Signal count crosses the flag threshold during normal operation, THE AI_Fit_Agent SHALL record the SKU as a candidate for the next Quality_Issue_Report.
7. THE AI_Fit_Agent SHALL limit the Quality_Issue_Report to an informational report delivered to the Product_Team and SHALL NOT take any action that removes, delists, reprices, or otherwise modifies a product.
8. IF no SKU meets the flag criteria, THEN THE AI_Fit_Agent SHALL produce a Quality_Issue_Report that states no SKUs currently meet the quality-issue thresholds.
9. WHEN the AI_Fit_Agent produces a Quality_Issue_Report, THE AI_Fit_Agent SHALL exclude customer identifiers from the report so that individual customers are not personally identified.
10. THE AI_Fit_Agent SHALL compute Quality_Return_Signal counts and Units_Sold for the Quality_Return_Rate from the same OMS SKU key, and SHALL NOT combine quality-return counts with sales volume from a dataset that cannot be joined on SKU.

### Requirement 6: Alternative Style Recommendation

**User Story:** As an ALDO customer whose chosen shoe does not suit my occasion, I want the assistant to suggest shoes that do, so that I can still find a suitable product without searching manually.

#### Acceptance Criteria

1. WHEN the AI_Fit_Agent determines that a SKU does not match the customer's requested Occasion, THE AI_Fit_Agent SHALL retrieve from the Product_Catalog through an Agent_Tool all Alternative_Styles whose suited Occasion categories include the requested Occasion AND that are in stock.
2. WHEN the AI_Fit_Agent retrieves more than 3 in-stock Alternative_Styles for the requested Occasion, THE AI_Fit_Agent SHALL rank candidates by closest match to the browsed SKU's product category, then by smallest absolute price difference from the browsed SKU, and SHALL present only the top 3 ranked Alternative_Styles.
3. WHEN the AI_Fit_Agent retrieves between 1 and 3 in-stock Alternative_Styles for the requested Occasion, THE AI_Fit_Agent SHALL present all retrieved Alternative_Styles.
4. WHEN the AI_Fit_Agent presents an Alternative_Style, THE AI_Fit_Agent SHALL state the requested Occasion that the Alternative_Style satisfies.
5. IF no in-stock Product_Catalog entry has suited Occasion categories that include the requested Occasion, THEN THE AI_Fit_Agent SHALL inform the customer that no matching Alternative_Style is currently available and SHALL present no Alternative_Styles.

### Requirement 7: Agent Transparency and Explainability

**User Story:** As a hackathon judge, I want to see the agent's reasoning, decisions, and actions, so that I can evaluate how the assistant reaches its recommendations.

#### Acceptance Criteria

1. WHEN the AI_Fit_Agent produces a recommendation, THE AI_Fit_Agent SHALL generate a Reasoning_Trace that records, in execution order, each reasoning step, each named Agent_Tool invoked, the data retrieved, and the resulting decision.
2. WHEN the AI_Fit_Agent produces a size or occasion recommendation, THE AI_Fit_Agent SHALL present a plain-language explanation that cites at least one historical signal supporting the recommendation.
3. WHEN the AI_Fit_Agent produces a recommendation, THE AI_Fit_Agent SHALL present the associated Confidence_Level, which is one of "high", "medium", or "low", to the customer.
4. WHEN the AI_Fit_Agent runs a demonstration scenario, THE AI_Fit_Agent SHALL produce a size recommendation, an occasion match result, and a visible Reasoning_Trace within that scenario.
5. WHEN the AI_Fit_Agent includes customer data in a Reasoning_Trace, THE AI_Fit_Agent SHALL mask direct customer identifiers so that individual customers are not personally identified.
6. IF an Agent_Tool fails while the AI_Fit_Agent is producing a recommendation, THEN THE AI_Fit_Agent SHALL record the failed Agent_Tool and the unavailable data in the Reasoning_Trace and SHALL still produce a visible Reasoning_Trace.

### Requirement 8: Graceful Handling of Sparse Data and Missing Profiles

**User Story:** As an ALDO customer whose product or profile has little historical data, I want the assistant to still give useful and honest guidance, so that I am not blocked or misled.

#### Acceptance Criteria

1. IF no Customer_Profile exists for the customer, THEN THE AI_Fit_Agent SHALL request the Stated_Usual_Size and requested Occasion directly from the customer before producing a recommendation.
2. IF the customer does not provide a Stated_Usual_Size when requested, THEN THE AI_Fit_Agent SHALL withhold any size recommendation, report a Confidence_Level of "low", and inform the customer that a usual size is required to produce a recommendation.
3. IF a Customer_Profile contains a Stated_Usual_Size but no Occasion, THEN THE AI_Fit_Agent SHALL request the Occasion from the customer and SHALL proceed with the size recommendation using the available Stated_Usual_Size.
4. IF a SKU has zero Fit_Return_Signals and zero Exchange_Signals, THEN THE AI_Fit_Agent SHALL recommend the Stated_Usual_Size, report a Confidence_Level of "low", and state that no fit history is available for the product.
5. IF the Product_Catalog contains no Occasion data for a SKU, THEN THE AI_Fit_Agent SHALL inform the customer that occasion suitability cannot be determined for the product.
6. IF a requested SKU is absent from the Product_Catalog, THEN THE AI_Fit_Agent SHALL inform the customer that the product could not be found and SHALL request a valid SKU.
7. IF an Agent_Tool fails to return data or does not respond within 10 seconds, THEN THE AI_Fit_Agent SHALL retry the Agent_Tool at most 2 times, and if all attempts fail, SHALL inform the customer that the recommendation is incomplete and SHALL identify which data was unavailable while preserving any recommendation data already produced.

### Requirement 9: AWS Bedrock Agent Integration

**User Story:** As the engineering team, I want AI Fit implemented as a Bedrock-hosted agent using Claude Opus with data-access tools, so that recommendations come from genuine agentic reasoning over the available data.

#### Acceptance Criteria

1. THE AI_Fit_Agent SHALL use the Claude Opus model hosted on the Bedrock_Runtime as the reasoning model.
2. THE AI_Fit_Agent SHALL access each data source through a dedicated Agent_Tool rather than embedding the data in the model prompt.
3. THE AI_Fit_Agent SHALL derive every size and occasion recommendation from data retrieved through an Agent_Tool.
4. IF the Bedrock_Runtime returns a non-throttling invocation error to the AI_Fit_Agent, THEN THE AI_Fit_Agent SHALL return a message that a recommendation could not be generated.
5. IF the Bedrock_Runtime returns a throttling error to the AI_Fit_Agent, THEN THE AI_Fit_Agent SHALL retry the invocation at most 2 times before returning a message that a recommendation could not be generated.
6. WHEN an Agent_Tool invocation does not return within 10 seconds, THE AI_Fit_Agent SHALL treat the invocation as failed.
7. IF the model returns a size or occasion recommendation without having invoked the Agent_Tool required for that recommendation, THEN THE AI_Fit_Agent SHALL reject the recommendation and SHALL NOT present it to the customer.

### Requirement 10: Responsible AI — Fairness, Privacy, and Bias

**User Story:** As a hackathon judge assessing Responsible AI, I want the assistant to protect customer fairness and privacy and to avoid bias, so that recommendations are equitable and personal data is safeguarded.

#### Acceptance Criteria

1. THE AI_Fit_Agent SHALL base size and occasion recommendations only on SKU fit signals, exchange signals, product attributes, and the customer's stated size and Occasion.
2. THE AI_Fit_Agent SHALL exclude every Protected_Attribute from the inputs used to generate recommendations.
3. WHEN the AI_Fit_Agent includes customer data in a Reasoning_Trace, THE AI_Fit_Agent SHALL mask the direct customer identifiers customer_uid and cnsmr_prof_sid so that an individual customer cannot be re-identified from the Reasoning_Trace.
4. IF two SKUs present identical fit and exchange signals and are given the same Stated_Usual_Size, THEN THE AI_Fit_Agent SHALL produce identical size recommendations for both SKUs on repeated invocations.
5. THE AI_Fit_Agent MAY use a product's gender category as a product attribute while continuing to exclude the customer's personal gender attribute from recommendation inputs.
6. IF a Protected_Attribute is present in the input set provided to the AI_Fit_Agent, THEN THE AI_Fit_Agent SHALL discard the Protected_Attribute before generating a recommendation.
7. THE AI_Fit_Agent MAY use the Sizing_System, Customer_Fit_Profile, Comfort_Style_Priority, and Foot_Measurement as legitimate fit inputs, and SHALL NOT treat the Sizing_System as the customer's personal gender Protected_Attribute.

### Requirement 11: Responsible AI — Financial Authority and Human Oversight

**User Story:** As an ALDO stakeholder, I want the assistant's authority bounded and subject to human oversight, so that the assistant advises customers without taking financial actions on its own.

#### Acceptance Criteria

1. THE AI_Fit_Agent SHALL limit its actions to presenting size recommendations, occasion assessments, and Alternative_Style suggestions, and SHALL NOT execute any action that modifies order state, pricing, or payment.
2. IF a recommendation would involve a refund, a price change, a discount, or an order modification, THEN THE AI_Fit_Agent SHALL route the action to a Human_Reviewer rather than executing it, and SHALL inform the customer that the request has been forwarded for human review.
3. IF a customer explicitly requests a discount, refund, or order modification directly from the AI_Fit_Agent, THEN THE AI_Fit_Agent SHALL decline to perform the action, SHALL state that it has no financial authority, and SHALL route the request to a Human_Reviewer.
4. WHEN the AI_Fit_Agent assigns a Confidence_Level of "low" to a recommendation, THE AI_Fit_Agent SHALL present the recommendation as advisory and SHALL state that the final decision rests with the customer.
5. WHEN the AI_Fit_Agent produces any recommendation, THE AI_Fit_Agent SHALL make the associated Reasoning_Trace available to a Human_Reviewer.
6. WHEN the AI_Fit_Agent routes an action to a Human_Reviewer, THE AI_Fit_Agent SHALL create an audit log entry recording the triggering request, the routed action type, and a timestamp.
