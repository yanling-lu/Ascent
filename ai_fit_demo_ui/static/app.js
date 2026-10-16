/*
 * AI Fit Demo UI — app.js (task 6.2: bootstrap).
 *
 * This is the Demo_UI bootstrap. On load it fetches `/api/config` and renders
 * the simulated PDP, the size grid, the Prototype_Disclaimer, the Demo_Guide
 * (with a working hide control), and the customer fit summary (usual size +
 * comfort priority only — never a Protected_Attribute). It records a clicked
 * size into `UIState.selectedSize`.
 *
 * The browser is a pure renderer of server-supplied JSON. It NEVER computes a
 * fit recommendation, occasion match, or alternatives in the browser — all
 * judgment comes from the `ai_fit` agent via the Recommendation_Service.
 *
 * Later tasks extend this file rather than replace it:
 *   - 7.x  "Get fit insight" POST + recommendation rendering
 *   - 8.x  add-to-bag warning flow, alternatives, occasion match
 *   - 9.x  collapsible masked activity (trace) rendering
 *   - 10   non-blocking error banner + retry
 *
 * To keep those tasks able to build on the bootstrap, the shared client state
 * (`UIState`) and the DOM render helpers are exposed on `window.AiFitDemo`.
 *
 * Vanilla JS only; no build step.
 */
(function () {
  "use strict";

  /* --------------------------------------------------------------------- */
  /* Client state (design "Client state (browser)" -> UIState)             */
  /* --------------------------------------------------------------------- */

  /**
   * The single client-side state object. The JS only reflects this state into
   * the DOM; it never derives fit judgment. Later tasks populate the
   * `last*` / `pending` / `lastError` / `activityExpanded` fields.
   *
   * @typedef {Object} UIState
   * @property {number|null} selectedSize      Stated_Usual_Size for the next fit request (Req 2.1).
   * @property {Object|null} config            The `/api/config` response (DemoUIConfig).
   * @property {Object|null} lastRecommendation Recommendation sub-object (Req 3), filled by task 7.
   * @property {Object|null} lastOccasionMatch  Occasion-match dict (Req 5.4), filled by task 8.
   * @property {Object|null} lastAlternatives   find_alternative_styles dict (Req 5), filled by task 8.
   * @property {boolean} pending               Fit request in flight (Req 2.4), toggled by task 7.
   * @property {string|null} lastError         Non-blocking error message (Req 8.2), set by task 10.
   * @property {boolean} activityExpanded      Trace toggle, default collapsed (Req 6.5), task 9.
   */
  var UIState = {
    selectedSize: null,
    selectedOccasion: null,
    config: null,
    lastRecommendation: null,
    lastOccasionMatch: null,
    lastAlternatives: null,
    pending: false,
    lastError: null,
    activityExpanded: false,
  };

  /* --------------------------------------------------------------------- */
  /* Small DOM helpers                                                     */
  /* --------------------------------------------------------------------- */

  /** Look up a stable element id defined in index.html. */
  function el(id) {
    return document.getElementById(id);
  }

  /** Remove all children of a node. */
  function clear(node) {
    if (!node) {
      return;
    }
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  /**
   * Create an element with optional class, text, and attributes. Text is set
   * via textContent so server-supplied strings are never interpreted as HTML.
   */
  function elem(tag, options) {
    var opts = options || {};
    var node = document.createElement(tag);
    if (opts.className) {
      node.className = opts.className;
    }
    if (opts.text != null) {
      node.textContent = String(opts.text);
    }
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (name) {
        node.setAttribute(name, opts.attrs[name]);
      });
    }
    return node;
  }

  /** Format a USD price. Falls back to the raw value if it is not a number. */
  function formatPrice(value) {
    var amount = Number(value);
    if (!isFinite(amount)) {
      return value == null ? "" : String(value);
    }
    return "$" + amount.toFixed(2);
  }

  /** Normalize a size value to a trimmed display string. */
  function sizeLabel(size) {
    return String(size).trim();
  }

  /* --------------------------------------------------------------------- */
  /* Config fetch                                                          */
  /* --------------------------------------------------------------------- */

  /**
   * Fetch the DemoUIConfig from the Recommendation_Service.
   * Shape: { product, customer_summary: {usual_shoe_size, comfort_style_priority},
   *          disclaimer, demo_steps }.
   */
  function fetchConfig() {
    return fetch("/api/config", {
      headers: { Accept: "application/json" },
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("config request failed: " + response.status);
      }
      return response.json();
    });
  }

  /* --------------------------------------------------------------------- */
  /* Render: Prototype_Disclaimer (Req 1.3)                                */
  /* --------------------------------------------------------------------- */

  function renderDisclaimer(config) {
    var textNode = el("disclaimer-text");
    if (textNode && config && config.disclaimer) {
      textNode.textContent = config.disclaimer;
    }
    // The banner is present in index.html on load; nothing is hidden here.
  }

  /* --------------------------------------------------------------------- */
  /* Render: Demo_Guide + working hide control (Req 1.4)                   */
  /* --------------------------------------------------------------------- */

  function renderDemoGuide(config) {
    var list = el("demo-guide-steps");
    var steps = (config && config.demo_steps) || [];
    if (list) {
      clear(list);
      steps.forEach(function (step) {
        list.appendChild(elem("li", { text: step }));
      });
    }

    // Working hide control: hides the whole Demo_Guide strip (Req 1.4).
    var hideBtn = el("demo-guide-hide");
    var guide = el("demo-guide");
    if (hideBtn && guide && !hideBtn.dataset.wired) {
      hideBtn.dataset.wired = "true";
      hideBtn.addEventListener("click", function () {
        guide.hidden = true;
      });
    }
  }

  /* --------------------------------------------------------------------- */
  /* Render: PDP (name, price, image, color swatches) (Req 1.1)            */
  /* --------------------------------------------------------------------- */

  function renderProduct(config) {
    var product = (config && config.product) || {};

    var nameNode = el("product-name");
    if (nameNode) {
      nameNode.textContent = product.name || "";
    }

    var priceNode = el("product-price");
    if (priceNode) {
      priceNode.textContent = formatPrice(product.price_usd);
    }

    var image = el("product-image");
    if (image) {
      if (product.image_path) {
        image.src = "/static/" + product.image_path;
        image.alt = product.name || "Product image";
      } else {
        image.removeAttribute("src");
        image.alt = "";
      }
    }

    renderColorSwatches(product.color_options || []);
  }

  function renderColorSwatches(colors) {
    var container = el("color-swatches");
    if (!container) {
      return;
    }
    clear(container);
    colors.forEach(function (color, index) {
      var swatch = elem("button", {
        className: "color-swatch",
        text: color,
        attrs: {
          type: "button",
          "aria-pressed": index === 0 ? "true" : "false",
        },
      });
      swatch.addEventListener("click", function () {
        var pressed = container.querySelectorAll('.color-swatch[aria-pressed="true"]');
        pressed.forEach(function (node) {
          node.setAttribute("aria-pressed", "false");
        });
        swatch.setAttribute("aria-pressed", "true");
      });
      container.appendChild(swatch);
    });
  }

  /* --------------------------------------------------------------------- */
  /* Render: size grid from product.offered_sizes (Req 1.2, 2.1)           */
  /* --------------------------------------------------------------------- */

  /**
   * Build the size grid as selectable controls from `product.offered_sizes`.
   * Clicking a cell records the size into `UIState.selectedSize` (Req 2.1) and
   * never computes fit in the browser.
   */
  function renderSizeGrid(config) {
    var grid = el("size-grid");
    if (!grid) {
      return;
    }
    var product = (config && config.product) || {};
    var sizes = product.offered_sizes || [];

    clear(grid);
    sizes.forEach(function (size) {
      var label = sizeLabel(size);
      var cell = elem("button", {
        className: "size-cell",
        text: label,
        attrs: {
          type: "button",
          "aria-pressed": "false",
          "data-size": label,
        },
      });
      cell.addEventListener("click", function () {
        selectSize(label);
      });
      grid.appendChild(cell);
    });
  }

  /**
   * Record a clicked size into UIState (Req 2.1). Reflects the selection into
   * the DOM only; it does not request or compute a recommendation.
   */
  function selectSize(label) {
    var numeric = Number(label);
    UIState.selectedSize = isFinite(numeric) ? numeric : label;

    var grid = el("size-grid");
    if (grid) {
      var cells = grid.querySelectorAll(".size-cell");
      cells.forEach(function (cell) {
        var isSelected = cell.getAttribute("data-size") === label;
        cell.setAttribute("aria-pressed", isSelected ? "true" : "false");
      });
    }

    // Clear any prior "please select a size" hint once a size is chosen.
    var hint = el("size-hint");
    if (hint) {
      hint.textContent = "";
    }

    // Note: selecting a grid size only records what the shopper intends to buy.
    // It does NOT re-run the fit check — "Fit for this style" stays the agent's
    // recommendation (based on the customer's saved usual size), which is loaded
    // once at startup and refreshed only when the occasion changes.
  }

  /* --------------------------------------------------------------------- */
  /* Render: customer fit summary (usual size + comfort priority) (Req 2.2)*/
  /* --------------------------------------------------------------------- */

  /**
   * Render the demo customer's saved fit summary. The service forwards ONLY
   * `usual_shoe_size` and `comfort_style_priority`; no Protected_Attribute is
   * ever present in the config, and this renderer reads only those two fields
   * (Req 1.5, 2.2).
   */
  function renderCustomerSummary(config) {
    var container = el("fit-customer-summary");
    if (!container) {
      return;
    }
    var summary = (config && config.customer_summary) || {};
    clear(container);

    // Two side-by-side cells: the customer's usual size (from their saved
    // profile) and the "fit for this style" cell. The second cell starts as a
    // placeholder ("Not yet confirmed") and is filled in by renderRecommendation
    // once the agent returns an evidence-backed size. The browser never computes
    // the recommended size itself.
    var grid = elem("div", { className: "fit-compare" });

    var usual = elem("div", { className: "fit-cell" });
    usual.appendChild(elem("span", { text: "Your usual size" }));
    usual.appendChild(
      elem("strong", {
        text:
          summary.usual_shoe_size != null
            ? sizeLabel(summary.usual_shoe_size)
            : "\u2014",
      })
    );
    grid.appendChild(usual);

    var fit = elem("div", { className: "fit-cell" });
    fit.appendChild(elem("span", { text: "Fit for this style" }));
    fit.appendChild(
      elem("strong", {
        className: "fit-cell__value fit-cell__value--pending",
        attrs: { id: "fit-suggested-size" },
        text: "Not yet confirmed",
      })
    );
    fit.appendChild(
      elem("small", {
        attrs: { id: "fit-size-note" },
        text: "Connect agent for sizing",
      })
    );
    grid.appendChild(fit);

    container.appendChild(grid);

    // Comfort priority, when present, shown as a quiet line under the cells.
    if (summary.comfort_style_priority != null) {
      container.appendChild(
        elem("p", {
          className: "fit-panel__priority",
          text: "Comfort priority: " + String(summary.comfort_style_priority),
        })
      );
    }
  }

  /**
   * Update the "Fit for this style" summary cell from a server Recommendation.
   * Shows the agent's recommended size + confidence when ok; resets to the
   * "Not yet confirmed" placeholder otherwise. The size shown is always the
   * server's value, never computed in the browser.
   */
  function updateFitCell(recommendation) {
    var value = el("fit-suggested-size");
    var note = el("fit-size-note");
    if (!value) {
      return;
    }
    if (recommendation && recommendation.ok && recommendation.recommended_size != null) {
      value.textContent = sizeLabel(recommendation.recommended_size);
      value.className = "fit-cell__value";
      if (note) {
        note.textContent = recommendation.confidence
          ? recommendation.confidence + " confidence"
          : "From the ALDO FIT agent";
      }
    } else {
      value.textContent = "Not yet confirmed";
      value.className = "fit-cell__value fit-cell__value--pending";
      if (note) {
        note.textContent = "Insufficient size evidence";
      }
    }
  }

  /* --------------------------------------------------------------------- */
  /* Non-blocking error banner + retry (Req 8.2, 8.4)                      */
  /* --------------------------------------------------------------------- */

  /**
   * Hide and clear the non-blocking error banner (Req 8.2). Clears both the
   * message text and the stored `UIState.lastError`, and detaches any retry
   * handler wired to `#error-retry` by marking the current retry inert. The
   * banner is `position: fixed` in styles.css, so it never blocks the page —
   * size selection and add-to-bag stay usable throughout (Req 8.4).
   */
  function hideError() {
    UIState.lastError = null;
    _currentRetry = null;
    var message = el("error-message");
    if (message) {
      message.textContent = "";
    }
    var banner = el("error-banner");
    if (banner) {
      banner.hidden = true;
    }
  }

  // The retry function for the most recent failure. `#error-retry` is wired once
  // (in wireErrorBanner) and dispatches to whatever this points at, so a single
  // listener serves every failed request. Null when there is nothing to retry.
  var _currentRetry = null;

  /**
   * Show the non-blocking error banner with a Retry action (Req 8.2). Fills
   * `#error-message` via textContent (server/err strings are never interpreted
   * as HTML), unhides `#error-banner`, records `UIState.lastError`, and stores
   * `retryFn` as the action `#error-retry` re-issues. The banner does not
   * disable or cover the PDP controls, so the page remains usable (Req 8.4).
   *
   * @param {string} message  Customer-facing error text.
   * @param {Function} [retryFn]  Re-issues the failed request when Retry is hit.
   */
  function showError(message, retryFn) {
    var text =
      message != null && String(message).trim() !== ""
        ? String(message)
        : "Something went wrong. Please try again.";
    UIState.lastError = text;
    _currentRetry = typeof retryFn === "function" ? retryFn : null;

    var messageNode = el("error-message");
    if (messageNode) {
      messageNode.textContent = text;
    }

    // Ensure the Retry control is wired even if renderAll has not run yet.
    wireErrorBanner();

    var retryBtn = el("error-retry");
    if (retryBtn) {
      // Hide the Retry control when there is nothing to re-issue.
      retryBtn.hidden = _currentRetry == null;
    }

    var banner = el("error-banner");
    if (banner) {
      banner.hidden = false;
    }
  }

  /**
   * Wire `#error-retry` once (Req 8.2). The single click handler re-issues the
   * most recent failed request by invoking `_currentRetry`. The banner is left
   * visible until the retried request resolves; a success path calls
   * {@link hideError}, so a passing retry clears the error.
   */
  function wireErrorBanner() {
    var retryBtn = el("error-retry");
    if (!retryBtn || retryBtn.dataset.wired) {
      return;
    }
    retryBtn.dataset.wired = "true";
    retryBtn.addEventListener("click", function () {
      var retry = _currentRetry;
      if (typeof retry === "function") {
        retry();
      }
    });
  }

  /* --------------------------------------------------------------------- */
  /* Fit insight: POST /api/recommendation + guard/pending (Req 2.3-2.5)   */
  /* --------------------------------------------------------------------- */

  /**
   * POST a recommendation request to the Recommendation_Service.
   *
   * Per the service contract, the browser sends only `{sku, size}`: the SKU comes
   * from the loaded config (`config.product.sku`) and the size from the current
   * selection. `customer_id` and `occasion` are intentionally omitted so the
   * server falls back to its demo defaults (the product block in `/api/config`
   * no longer carries them). Resolves to the parsed `{recommendation,
   * occasion_match}` body (Req 2.3).
   */
  function postRecommendation(sku, size, occasion) {
    var payload = { sku: sku, size: size };
    // Only send occasion when the shopper has chosen one; otherwise the server
    // falls back to the demo customer's default occasion.
    if (occasion != null && String(occasion).trim() !== "") {
      payload.occasion = occasion;
    }
    return fetch("/api/recommendation", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload),
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("recommendation request failed: " + response.status);
      }
      return response.json();
    });
  }

  /**
   * Reflect the pending flag into the DOM (Req 2.4). While a request is in
   * flight the pending indicator (`#fit-pending`) is shown and the prior result
   * (`#fit-result`) is hidden/greyed so it is never read as the current one.
   * When pending clears, the result region is un-greyed; its contents are
   * rendered by task 7.2.
   */
  function setPending(isPending) {
    UIState.pending = isPending;

    var pendingNode = el("fit-pending");
    if (pendingNode) {
      pendingNode.hidden = !isPending;
    }

    var result = el("fit-result");
    if (result) {
      if (isPending) {
        // Hide and grey the prior result so it is not read as current (Req 2.4).
        clear(result);
        result.classList.add("is-stale");
        result.setAttribute("aria-busy", "true");
      } else {
        result.classList.remove("is-stale");
        result.removeAttribute("aria-busy");
      }
    }
  }

  /**
   * Append each `warnings[]` entry and the optional `note` as a Fit_Warning
   * (Req 3.4). Each is a `.fit-warning` block set via textContent so the
   * server-supplied string is never interpreted as HTML.
   */
  function appendFitWarnings(container, recommendation) {
    var warnings = (recommendation && recommendation.warnings) || [];
    warnings.forEach(function (warning) {
      if (warning != null && String(warning).trim() !== "") {
        container.appendChild(
          elem("div", { className: "fit-warning", text: warning })
        );
      }
    });
    var note = recommendation && recommendation.note;
    if (note != null && String(note).trim() !== "") {
      container.appendChild(elem("div", { className: "fit-warning", text: note }));
    }
  }

  /**
   * Render the recommendation from server JSON into `#fit-result` (Req 3, 8.1).
   *
   * The browser NEVER computes fit; it only reflects `UIState.lastRecommendation`
   * (the server's Recommendation sub-object) into the DOM:
   *   - `ok === true`  -> recommended size, confidence, explanation, evidence
   *                       (supporting_signal_count), advisory framing when
   *                       `advisory === true`, Fit_Warnings for warnings/note,
   *                       and a highlighted mismatch when the recommended size
   *                       differs from the selected size (Req 3.1-3.5).
   *   - `ok === false` -> the customer-facing failure message from
   *                       `failure_reason` / `explanation`, and NO size/confidence
   *                       (Req 8.1).
   *
   * The occasion match (`#occasion-match`) is rendered by task 8.2, not here.
   */
  /**
   * Render the headline fit alert into `#fit-alert`, shown ABOVE the Get fit
   * insight bar. The headline is the agent's first warning (or note); a short
   * subtext names the selected size so the shopper sees why the check fired.
   * All text comes from the server-supplied recommendation.
   */
  function renderFitAlert(recommendation) {
    var alert = el("fit-alert");
    if (!alert) {
      return;
    }
    clear(alert);

    var messages = collectFitWarnings(recommendation);
    if (messages.length === 0) {
      return;
    }

    var row = elem("div", { className: "alert-heading" });
    row.appendChild(elem("span", { className: "alert-icon", attrs: { "aria-hidden": "true" }, text: "!" }));

    var body = elem("div");
    body.appendChild(elem("strong", { text: messages[0] }));
    if (UIState.selectedSize != null) {
      body.appendChild(
        elem("p", {
          text:
            "You selected size " +
            sizeLabel(UIState.selectedSize) +
            ". Tell us the occasion for a sharper recommendation.",
        })
      );
    }
    row.appendChild(body);
    alert.appendChild(row);
  }

  /**
   * Reveal the occasion question once a recommendation exists, and reflect the
   * currently selected occasion onto its pill buttons.
   */
  function showOccasionQuestion(show) {
    var block = el("fit-occasion");
    if (block) {
      block.hidden = !show;
    }
    var buttons = [el("occasion-everyday"), el("occasion-special")];
    buttons.forEach(function (btn) {
      if (!btn) {
        return;
      }
      var isSel = UIState.selectedOccasion === btn.getAttribute("data-occasion");
      btn.setAttribute("aria-pressed", isSel ? "true" : "false");
    });
  }

  function renderRecommendation() {
    var result = el("fit-result");
    if (!result) {
      return;
    }
    clear(result);

    var recommendation = UIState.lastRecommendation;

    // Reflect the agent's size into the "Fit for this style" summary cell.
    updateFitCell(recommendation);
    // Headline warning sits above the Get fit insight bar.
    renderFitAlert(recommendation);

    if (!recommendation) {
      showOccasionQuestion(false);
      return;
    }

    // ---- Failure case: no fabricated size/confidence (Req 8.1) ---------------
    if (!recommendation.ok) {
      showOccasionQuestion(false);
      var failureText =
        recommendation.failure_reason != null &&
        String(recommendation.failure_reason).trim() !== ""
          ? recommendation.failure_reason
          : recommendation.explanation;
      result.appendChild(
        elem("p", {
          className: "fit-result__failure",
          text:
            failureText != null && String(failureText).trim() !== ""
              ? failureText
              : "We couldn't generate a fit insight for this selection.",
        })
      );
      return;
    }

    // A valid recommendation is available: offer the occasion question.
    showOccasionQuestion(true);

    // ---- Success case (Req 3.1-3.5) -----------------------------------------

    // Recommended size (Req 3.1), with a mismatch highlight when it differs
    // from the selected size (Req 3.5). The comparison only reflects the
    // server-supplied size against the recorded selection; it computes no fit.
    if (recommendation.recommended_size != null) {
      var recommendedLabel = sizeLabel(recommendation.recommended_size);
      var sizeNode = elem("div", {
        className: "fit-result__size",
        text: "Recommended size " + recommendedLabel,
      });

      var selected = UIState.selectedSize;
      var isMismatch =
        selected != null && Number(recommendation.recommended_size) !== Number(selected);
      if (isMismatch) {
        sizeNode.appendChild(
          elem("span", {
            className: "fit-result__mismatch",
            text: " (differs from your selected size " + sizeLabel(selected) + ")",
          })
        );
      }
      result.appendChild(sizeNode);
    }

    // Confidence (Req 3.1).
    if (recommendation.confidence != null) {
      result.appendChild(
        elem("div", {
          className: "fit-result__confidence",
          text: "Confidence: " + recommendation.confidence,
        })
      );
    }

    // Advisory framing when advisory === true (Req 3.2).
    if (recommendation.advisory === true) {
      result.appendChild(
        elem("span", {
          className: "fit-result__advisory",
          text: "Advisory",
        })
      );
    }

    // Plain-language explanation (Req 3.1).
    if (
      recommendation.explanation != null &&
      String(recommendation.explanation).trim() !== ""
    ) {
      result.appendChild(
        elem("p", {
          className: "fit-result__explanation",
          text: recommendation.explanation,
        })
      );
    }

    // Supporting evidence amount (Req 3.3).
    if (recommendation.supporting_signal_count != null) {
      result.appendChild(
        elem("div", {
          className: "fit-result__evidence",
          text:
            "Based on " +
            recommendation.supporting_signal_count +
            " supporting signals",
        })
      );
    }

    // Fit_Warnings: each warning + the note (Req 3.4).
    appendFitWarnings(result, recommendation);
  }

  /**
   * Handle "Get fit insight" activation (Req 2.3-2.5).
   *
   * Guards that a size is selected; when none is, it prompts "please select a
   * size" in `#size-hint` and issues no request (Req 2.5). Otherwise it enters
   * the pending state (Req 2.4), POSTs the request, stores the response's
   * `recommendation` into `UIState.lastRecommendation` and `occasion_match` into
   * `UIState.lastOccasionMatch`, and hands off to the render hook (task 7.2).
   */
  function requestFitInsight() {
    var config = UIState.config || {};
    var product = config.product || {};
    var sku = product.sku;
    var occasion = UIState.selectedOccasion;

    // The agent recommends a size for THIS style from the customer's saved usual
    // size — not from whichever grid cell the shopper taps to buy. So "Fit for
    // this style" stays the agent's recommendation and does not move when the
    // shopper clicks size 10, etc. Fall back to the selected size only if the
    // profile has no usual size.
    var summary = (config && config.customer_summary) || {};
    var usual = summary.usual_shoe_size != null ? Number(summary.usual_shoe_size) : null;
    var size = usual != null && isFinite(usual) ? usual : UIState.selectedSize;

    if (size == null) {
      var hint = el("size-hint");
      if (hint) {
        hint.textContent = "Please select a size.";
      }
      return Promise.resolve();
    }

    setPending(true);

    return postRecommendation(sku, size, occasion)
      .then(function (data) {
        var payload = data || {};
        UIState.lastRecommendation = payload.recommendation || null;
        UIState.lastOccasionMatch = payload.occasion_match || null;
        // A successful request clears any prior non-blocking error (Req 8.2).
        hideError();
        setPending(false);
        renderRecommendation();
        renderOccasionMatch();
        renderActivity();
        // The fit insight is now shown; retract the "Get fit insight" bar so it
        // isn't a dangling control (the fit cell + alert carry the result).
        setFitActionsVisible(
          !(UIState.lastRecommendation && UIState.lastRecommendation.ok)
        );
        return payload;
      })
      .catch(function (err) {
        // Clear pending so the UI is not stuck, then surface a NON-BLOCKING
        // error banner whose Retry re-issues this same request (Req 8.2). The
        // banner never disables the PDP, so size selection + add-to-bag stay
        // usable throughout (Req 8.4).
        setPending(false);
        if (typeof console !== "undefined" && console.error) {
          console.error("Fit insight request failed:", err);
        }
        // showError records the customer-facing text on UIState.lastError.
        showError(
          "We couldn't get a fit insight. Please try again.",
          function () {
            return requestFitInsight();
          }
        );
      });
  }

  /** Wire the "Get fit insight" control to {@link requestFitInsight} (Req 2.3). */
  function wireFitInsight() {
    var button = el("get-fit-insight");
    if (button && !button.dataset.wired) {
      button.dataset.wired = "true";
      button.addEventListener("click", function () {
        requestFitInsight();
      });
    }
  }

  /** Show or hide the "Get fit insight" actions bar (retracts after a result). */
  function setFitActionsVisible(visible) {
    var actions = el("fit-actions");
    if (actions) {
      actions.hidden = !visible;
    }
  }

  /**
   * Wire the Everyday / Special occasion pills. Clicking one records the chosen
   * occasion into `UIState.selectedOccasion` and re-queries the ALDO FIT agent
   * so the recommendation reflects that use case. The browser only passes the
   * occasion to the agent; it never computes the fit itself.
   */
  function wireOccasionButtons() {
    [el("occasion-everyday"), el("occasion-special")].forEach(function (btn) {
      if (!btn || btn.dataset.wired) {
        return;
      }
      btn.dataset.wired = "true";
      btn.addEventListener("click", function () {
        UIState.selectedOccasion = btn.getAttribute("data-occasion");
        showOccasionQuestion(true);
        // Re-ask the agent so the fit recommendation reflects the occasion.
        requestFitInsight();
        // Show the curated demo alternatives for the chosen occasion.
        renderDemoAlternatives(UIState.selectedOccasion);
      });
    });
  }

  /**
   * Render the curated demo alternatives for an occasion (presentation-only).
   * Uses `window.AiFitDemoAlternatives` when present; if it is missing, falls
   * back to the live agent endpoint via {@link findAlternatives}. The fit
   * recommendation stays agent-backed either way.
   */
  function renderDemoAlternatives(occasion) {
    var data = window.AiFitDemoAlternatives;
    var region = el("alternatives");
    var list = el("alternatives-list");
    var empty = el("alternatives-empty");
    var titleNode = document.querySelector(".alternatives__title");

    if (!data || !region || !list) {
      // No curated set available — defer to the live agent alternatives.
      return findAlternatives();
    }

    var pick = data.forOccasion(occasion);
    if (!pick) {
      return findAlternatives();
    }

    if (titleNode) {
      titleNode.textContent = pick.title;
    }
    if (empty) {
      empty.textContent = pick.intro || "";
      empty.hidden = !pick.intro;
      empty.className = "alternatives__intro";
    }

    clear(list);
    (pick.picks || []).forEach(function (key) {
      var p = data.products[key];
      if (p) {
        list.appendChild(renderDemoAlternativeCard(p));
      }
    });

    region.hidden = false;
  }

  /**
   * Build one curated alternative card: product photo, name, reason, and a
   * link to the live ALDO product page. All text set via textContent.
   */
  function renderDemoAlternativeCard(p) {
    var card = elem("a", {
      className: "alt-card",
      attrs: { href: p.url, target: "_blank", rel: "noopener noreferrer" },
    });

    var media = elem("div", { className: "alt-card__media" });
    if (p.badge) {
      media.appendChild(elem("span", { className: "alt-card__badge", text: p.badge }));
    }
    var img = elem("img", {
      className: "alt-card__img",
      attrs: { src: p.img, alt: p.name + " \u2014 ALDO heel", loading: "eager" },
    });
    media.appendChild(img);
    card.appendChild(media);

    var body = elem("div", { className: "alt-card__body" });
    body.appendChild(elem("b", { className: "alternative-card__name", text: p.name }));
    body.appendChild(elem("p", { className: "alternative-card__reason", text: p.reason }));
    body.appendChild(elem("span", { className: "alt-card__cta", text: p.cta || "View on ALDO \u2197" }));
    card.appendChild(body);

    return card;
  }

  /* --------------------------------------------------------------------- */
  /* Add to Bag warning flow (Req 4.1-4.4)                                 */
  /* --------------------------------------------------------------------- */

  /**
   * Collect the Fit_Warning text present for the current selection. A warning
   * is "present" when the last recommendation carries a non-empty `warnings[]`
   * entry or a non-empty `note` (Req 4.2). Returns an array of trimmed strings
   * (empty when none). The browser derives no fit judgment here; it only reads
   * the server-supplied Recommendation already stored in UIState.
   */
  function collectFitWarnings(recommendation) {
    var messages = [];
    var rec = recommendation;
    if (!rec) {
      return messages;
    }
    var warnings = rec.warnings || [];
    warnings.forEach(function (warning) {
      if (warning != null && String(warning).trim() !== "") {
        messages.push(String(warning));
      }
    });
    if (rec.note != null && String(rec.note).trim() !== "") {
      messages.push(String(rec.note));
    }
    return messages;
  }

  /** Hide and empty the add-to-bag warning surface. */
  function hideAddToBagWarning() {
    var container = el("add-to-bag-warning");
    if (container) {
      clear(container);
      container.hidden = true;
    }
  }

  /**
   * Hook invoked when the shopper chooses "Find alternatives" from the add-to-bag
   * warning (Req 4.3). The actual alternatives fetch/render is task 8.2; this
   * bootstrap only provides a stub so the control is wired. Task 8.2 replaces
   * this by defining `window.AiFitDemo.findAlternatives`. If that richer hook is
   * present, it is called; otherwise the stub leaves a console note so the demo
   * does not break.
   */
  function findAlternativesStub() {
    if (typeof console !== "undefined" && console.info) {
      console.info("Find alternatives requested (handled by task 8.2).");
    }
  }

  /**
   * Mark the product as added to the bag (Req 4.4). Keeps the demo honest: it
   * only reflects state into the DOM and never alters the recommendation. The
   * warning surface is hidden once the shopper proceeds with the original
   * selection.
   */
  function markBagged() {
    hideAddToBagWarning();
    var button = el("add-to-bag");
    if (button) {
      button.textContent = "Added to Bag";
      button.setAttribute("aria-pressed", "true");
    }
  }

  /**
   * Append one confirmation line (✓ or neutral dot) to the add-to-bag surface.
   */
  function appendConfirmLine(container, ok, text) {
    var line = elem("div", {
      className: "confirm-line" + (ok ? " confirm-line--ok" : " confirm-line--note"),
    });
    line.appendChild(
      elem("span", {
        className: "confirm-line__mark",
        attrs: { "aria-hidden": "true" },
        text: ok ? "\u2713" : "\u2022",
      })
    );
    line.appendChild(elem("span", { text: text }));
    container.appendChild(line);
  }

  /**
   * Show the add-to-bag confirmation surface (Req 4.2-4.4). Instead of a fit
   * warning, this shows positive confirmation checks:
   *   - a ✓ when the agent's recommended size equals the shopper's selected
   *     (buy) size;
   *   - a ✓ when the occasion matches.
   * It keeps the "Find alternatives" / "Add anyway" controls so the shopper can
   * still explore options or proceed. All judgment comes from the server-supplied
   * recommendation + occasion match already in UIState.
   */
  function showAddToBagConfirm() {
    var container = el("add-to-bag-warning");
    if (!container) {
      return;
    }
    clear(container);

    var rec = UIState.lastRecommendation;
    var match = UIState.lastOccasionMatch;

    // Size-match check: recommended size vs the shopper's selected buy-size.
    var selected = UIState.selectedSize;
    var recommended =
      rec && rec.ok && rec.recommended_size != null ? Number(rec.recommended_size) : null;
    var sizeOk =
      recommended != null && selected != null && recommended === Number(selected);
    if (recommended != null) {
      appendConfirmLine(
        container,
        sizeOk,
        sizeOk
          ? "Size " + sizeLabel(selected) + " matches the recommended fit."
          : "Recommended size is " +
              sizeLabel(recommended) +
              (selected != null ? " (you selected " + sizeLabel(selected) + ")." : "."),
      );
    }

    // Occasion-match check.
    var matched = (match && match.matched_occasions) || [];
    var occasionOk = match && match.status === "match" && matched.length > 0;
    if (match) {
      appendConfirmLine(
        container,
        !!occasionOk,
        occasionOk
          ? "Matches your occasion: " + matched.join(", ") + "."
          : "No occasion match for this style.",
      );
    }

    var actions = elem("div", { className: "add-to-bag-warning__actions" });

    // A single full-width proceed button (no "Find alternatives"); reads
    // "Add to Bag" when the recommended size matches, else "Add anyway".
    var addAnywayBtn = elem("button", {
      className: "btn btn--primary",
      text: sizeOk ? "Add to Bag" : "Add anyway",
      attrs: { type: "button" },
    });
    addAnywayBtn.addEventListener("click", function () {
      markBagged();
    });
    actions.appendChild(addAnywayBtn);

    container.appendChild(actions);
    container.hidden = false;
  }

  /**
   * Handle "Add to Bag" activation (Req 4.1-4.4). Surfaces a confirmation panel
   * with size-match and occasion-match checks, plus the "Find alternatives" /
   * "Add anyway" controls. The browser derives no fit judgment; it only reflects
   * the server-supplied recommendation + occasion match.
   */
  function addToBag() {
    showAddToBagConfirm();
  }

  /** Wire the "Add to Bag" control to {@link addToBag} (Req 4.1). */
  function wireAddToBag() {
    var button = el("add-to-bag");
    if (button && !button.dataset.wired) {
      button.dataset.wired = "true";
      button.addEventListener("click", function () {
        addToBag();
      });
    }
  }

  /* --------------------------------------------------------------------- */
  /* Alternatives: POST /api/alternatives + render (Req 5.1-5.3, 5.5)      */
  /* --------------------------------------------------------------------- */

  /**
   * POST an alternatives request to the Recommendation_Service.
   *
   * Mirrors how {@link postRecommendation} works: the browser sends only `{sku}`
   * (from the loaded `config.product.sku`); `occasion` is intentionally omitted so
   * the server falls back to its demo default. Resolves to the parsed
   * find_alternative_styles dict `{requested_occasion, alternatives[],
   * none_available, message}` (Req 5.1). The three-item cap is applied by the
   * agent/tool, not the browser.
   */
  function postAlternatives(sku) {
    return fetch("/api/alternatives", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({ sku: sku }),
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("alternatives request failed: " + response.status);
      }
      return response.json();
    });
  }

  /**
   * Render a single alternative as a `.alternative-card` showing its name/
   * identifier, formatted price, and suited occasions (Req 5.2). Each dict has
   * `{sku, category, price_usd, occasions}`. All text is set via textContent so
   * server-supplied strings are never interpreted as HTML.
   */
  function renderAlternativeCard(alternative) {
    var alt = alternative || {};
    var card = elem("div", { className: "alternative-card" });

    // Name / identifier: prefer the human-readable category, else the SKU.
    var name =
      alt.category != null && String(alt.category).trim() !== ""
        ? String(alt.category)
        : alt.sku != null
        ? String(alt.sku)
        : "Alternative style";
    card.appendChild(elem("div", { className: "alternative-card__name", text: name }));

    // Formatted price (Req 5.2).
    if (alt.price_usd != null) {
      card.appendChild(
        elem("div", {
          className: "alternative-card__price",
          text: formatPrice(alt.price_usd),
        })
      );
    }

    // Suited occasions (Req 5.2).
    var occasions = alt.occasions || [];
    if (occasions.length > 0) {
      card.appendChild(
        elem("div", {
          className: "alternative-card__occasions",
          text: "Suited for: " + occasions.join(", "),
        })
      );
    }

    return card;
  }

  /**
   * Render the alternatives region from `UIState.lastAlternatives` (Req 5.1-5.3,
   * 5.5). Unhides `#alternatives`, fills `#alternatives-list` with at most three
   * `.alternative-card` entries (the tool already caps the list), and when
   * `none_available === true` shows the explicit "no alternatives currently
   * available" message in `#alternatives-empty`. The browser derives no fit
   * judgment here; it only reflects the server-supplied dict.
   */
  function renderAlternatives() {
    var region = el("alternatives");
    var list = el("alternatives-list");
    var empty = el("alternatives-empty");
    if (!region) {
      return;
    }

    var result = UIState.lastAlternatives || {};
    var alternatives = result.alternatives || [];

    region.hidden = false;

    if (list) {
      clear(list);
    }

    // None-available case: explicit message, no cards (Req 5.3).
    if (result.none_available === true || alternatives.length === 0) {
      if (empty) {
        empty.textContent =
          result.message != null && String(result.message).trim() !== ""
            ? String(result.message)
            : "No alternatives currently available.";
        empty.hidden = false;
      }
      return;
    }

    // Render each alternative, capped at three to match the agent (Req 5.5).
    if (empty) {
      empty.textContent = "";
      empty.hidden = true;
    }
    if (list) {
      alternatives.slice(0, 3).forEach(function (alternative) {
        list.appendChild(renderAlternativeCard(alternative));
      });
    }
  }

  /**
   * Fetch and render occasion-matching alternatives for the browsed SKU and the
   * demo occasion (Req 5.1). This is the richer hook the add-to-bag "Find
   * alternatives" control defers to (task 8.1 left a stub that calls
   * `window.AiFitDemo.findAlternatives` when present). It POSTs `/api/alternatives`,
   * stores the result into `UIState.lastAlternatives`, and renders it.
   */
  function findAlternatives() {
    var config = UIState.config || {};
    var product = config.product || {};
    var sku = product.sku;

    return postAlternatives(sku)
      .then(function (data) {
        UIState.lastAlternatives = data || null;
        // A successful request clears any prior non-blocking error (Req 8.2).
        hideError();
        renderAlternatives();
        return data;
      })
      .catch(function (err) {
        // Surface a NON-BLOCKING error banner whose Retry re-issues this same
        // alternatives request (Req 8.2); the page stays usable (Req 8.4).
        if (typeof console !== "undefined" && console.error) {
          console.error("Find alternatives request failed:", err);
        }
        showError(
          "We couldn't load alternative styles. Please try again.",
          function () {
            return findAlternatives();
          }
        );
      });
  }

  /* --------------------------------------------------------------------- */
  /* Occasion match render (Req 5.4)                                       */
  /* --------------------------------------------------------------------- */

  /**
   * Render the occasion-match result from `UIState.lastOccasionMatch` into
   * `#occasion-match` (Req 5.4), naming whether and which occasion matched using
   * the match dict's `.status` and `.matched_occasions` (and the server's
   * `.message` when present). All text is set via textContent. The browser
   * derives no occasion judgment; it only reflects the server-supplied dict.
   */
  function renderOccasionMatch() {
    var container = el("occasion-match");
    if (!container) {
      return;
    }
    clear(container);

    var match = UIState.lastOccasionMatch;
    if (!match) {
      return;
    }

    var matched = match.matched_occasions || [];
    var isMatch = match.status === "match" && matched.length > 0;

    // Headline: a green check when the occasion matches, naming which one.
    var status = elem("div", {
      className:
        "occasion-match__status" + (isMatch ? " occasion-match__status--ok" : ""),
    });
    if (isMatch) {
      status.appendChild(
        elem("span", { className: "occasion-match__check", attrs: { "aria-hidden": "true" }, text: "\u2713" })
      );
      status.appendChild(
        elem("span", { text: "Occasion match: " + matched.join(", ") })
      );
    } else {
      status.appendChild(elem("span", { text: "No occasion match for this style" }));
    }
    container.appendChild(status);

    // The server's own message, when present, adds the plain-language detail.
    if (match.message != null && String(match.message).trim() !== "") {
      container.appendChild(
        elem("p", {
          className: "occasion-match__message",
          text: match.message,
        })
      );
    }
  }

  /* --------------------------------------------------------------------- */
  /* Collapsible masked activity / trace (Req 6.1-6.5)                     */
  /* --------------------------------------------------------------------- */

  /**
   * The redaction placeholder the trace builder uses for masked identifiers
   * (`customer_uid` / `cnsmr_prof_sid`). The browser renders whatever the
   * server already masked VERBATIM; it NEVER resolves a masked value (Req 6.3).
   */
  var MASKED_PLACEHOLDER = "***MASKED***";

  /**
   * Append a `<dt>/<dd>` pair into a `<dl>` for a trace field. When the value is
   * the redaction placeholder (or otherwise matches it), the `<dd>` is marked
   * with `.activity-masked` and the placeholder is shown verbatim (Req 6.3).
   * Objects/arrays are stringified; the browser never resolves masked values.
   */
  function appendTraceField(dl, label, value) {
    if (value == null) {
      return;
    }
    var text;
    var isMasked = false;
    if (typeof value === "string") {
      text = value;
      isMasked = value === MASKED_PLACEHOLDER;
    } else if (typeof value === "object") {
      // Render the already-masked summary verbatim; detect a masked marker so a
      // redacted identifier nested in the summary is flagged, never resolved.
      try {
        text = JSON.stringify(value);
      } catch (e) {
        text = String(value);
      }
      isMasked = text.indexOf(MASKED_PLACEHOLDER) !== -1;
    } else {
      text = String(value);
    }

    dl.appendChild(elem("dt", { text: label }));
    dl.appendChild(
      elem("dd", {
        className: isMasked ? "activity-masked" : undefined,
        text: text,
      })
    );
  }

  /**
   * Render a single ordered reasoning step as an `.activity-step` block
   * (Req 6.2). Shows the `order`, `step_type`, the `tool_name` when present
   * (using `.activity-step__tool`), and any `data_summary` / `decision`. All
   * text is set via textContent; masked values inside `data_summary` are shown
   * verbatim and flagged, never resolved (Req 6.3).
   */
  function renderActivityStep(step) {
    var s = step || {};
    var block = elem("div", { className: "activity-step" });

    var headParts = [];
    if (s.order != null) {
      headParts.push("Step " + s.order);
    }
    if (s.step_type != null && String(s.step_type).trim() !== "") {
      headParts.push(String(s.step_type));
    }
    if (headParts.length > 0) {
      block.appendChild(
        elem("div", { className: "activity-step__head", text: headParts.join(" — ") })
      );
    }

    if (s.tool_name != null && String(s.tool_name).trim() !== "") {
      block.appendChild(
        elem("div", {
          className: "activity-step__tool",
          text: "Tool: " + s.tool_name,
        })
      );
    }

    var dl = elem("dl", { className: "activity-step__detail" });
    appendTraceField(dl, "Data", s.data_summary);
    appendTraceField(dl, "Decision", s.decision);
    if (dl.childNodes.length > 0) {
      block.appendChild(dl);
    }

    return block;
  }

  /**
   * Render the masked reasoning trace from `UIState.lastRecommendation.trace`
   * into `#activity-body` (Req 6.1-6.4). Shows the ordered `steps[]` (with the
   * tools invoked), the `validator_corrections[]`, and the `failed_tools[]`
   * rendered as "tool failed / data unavailable" (`.activity-failed`). Masked
   * identifier values are rendered verbatim as received; the browser only
   * reflects the already-masked trace from the server and never resolves
   * `customer_uid` / `cnsmr_prof_sid` (Req 6.3).
   */
  function renderActivity() {
    var body = el("activity-body");
    if (!body) {
      return;
    }
    clear(body);

    var recommendation = UIState.lastRecommendation;
    var trace = recommendation && recommendation.trace;
    if (!trace) {
      body.appendChild(
        elem("p", {
          className: "activity__empty",
          text: "No activity to show yet.",
        })
      );
      return;
    }

    // Ordered reasoning steps + tools invoked (Req 6.2).
    var steps = trace.steps || [];
    if (steps.length > 0) {
      var stepsSection = elem("div", { className: "activity-section" });
      stepsSection.appendChild(
        elem("h4", { className: "activity-section__title", text: "Steps" })
      );
      steps.forEach(function (step) {
        stepsSection.appendChild(renderActivityStep(step));
      });
      body.appendChild(stepsSection);
    }

    // Validator corrections (Req 6.2).
    var corrections = trace.validator_corrections || [];
    if (corrections.length > 0) {
      var corrSection = elem("div", { className: "activity-section" });
      corrSection.appendChild(
        elem("h4", {
          className: "activity-section__title",
          text: "Validator corrections",
        })
      );
      corrections.forEach(function (correction) {
        var c = correction || {};
        var dl = elem("dl", { className: "activity-correction" });
        appendTraceField(dl, "Invariant", c.invariant);
        appendTraceField(dl, "Proposed", c.proposed_value);
        appendTraceField(dl, "Corrected", c.corrected_value);
        corrSection.appendChild(dl);
      });
      body.appendChild(corrSection);
    }

    // Failed tools / unavailable data (Req 6.4).
    var failedTools = trace.failed_tools || [];
    if (failedTools.length > 0) {
      var failedSection = elem("div", { className: "activity-section" });
      failedSection.appendChild(
        elem("h4", {
          className: "activity-section__title",
          text: "Unavailable data",
        })
      );
      failedTools.forEach(function (failed) {
        var f = failed || {};
        var toolName =
          f.tool_name != null && String(f.tool_name).trim() !== ""
            ? String(f.tool_name)
            : "A tool";
        failedSection.appendChild(
          elem("div", {
            className: "activity-failed",
            text: toolName + ": tool failed / data unavailable",
          })
        );
      });
      body.appendChild(failedSection);
    }
  }

  /**
   * Wire the "activity" toggle (Req 6.1, 6.5). The control defaults to COLLAPSED
   * (`#activity-body` hidden, `aria-expanded="false"` in index.html). Clicking
   * flips `UIState.activityExpanded`, shows/hides `#activity-body`, and updates
   * `aria-expanded`. On expand it (re-)renders the current trace so the body is
   * populated even if the recommendation arrived while collapsed.
   */
  function wireActivityToggle() {
    var toggle = el("activity-toggle");
    if (!toggle || toggle.dataset.wired) {
      return;
    }
    toggle.dataset.wired = "true";
    toggle.addEventListener("click", function () {
      UIState.activityExpanded = !UIState.activityExpanded;
      var body = el("activity-body");
      if (body) {
        body.hidden = !UIState.activityExpanded;
      }
      toggle.setAttribute(
        "aria-expanded",
        UIState.activityExpanded ? "true" : "false"
      );
      if (UIState.activityExpanded) {
        renderActivity();
      }
    });
  }

  /* --------------------------------------------------------------------- */
  /* Bootstrap                                                             */
  /* --------------------------------------------------------------------- */

  /** Render every section from a loaded DemoUIConfig. */
  function renderAll(config) {
    UIState.config = config;
    renderDisclaimer(config);
    renderDemoGuide(config);
    renderProduct(config);
    renderSizeGrid(config);
    renderCustomerSummary(config);
    wireFitInsight();
    wireOccasionButtons();
    wireAddToBag();
    wireActivityToggle();
    wireErrorBanner();
    applyJudgeMode();
  }

  /**
   * The masked reasoning trace ("Why these picks?") is for JUDGING, not for
   * shoppers. It is hidden from the customer-facing view by default and only
   * revealed when the page is opened in judge mode (``?judge=1`` in the URL, or
   * ``window.AI_FIT_JUDGE === true``). The trace still renders when revealed, so
   * judges can expand it to inspect the agent's steps and masked identifiers.
   */
  function isJudgeMode() {
    if (typeof window !== "undefined" && window.AI_FIT_JUDGE === true) {
      return true;
    }
    try {
      var qs =
        (typeof window !== "undefined" &&
          window.location &&
          window.location.search) ||
        "";
      return /[?&]judge=1(&|$)/.test(qs);
    } catch (e) {
      return false;
    }
  }

  function applyJudgeMode() {
    var activity = el("activity");
    if (activity) {
      activity.hidden = !isJudgeMode();
    }
  }

  /**
   * Load `/api/config` and render the shell. On failure, surface a short hint
   * in the size area; the full non-blocking error banner + retry wiring is a
   * later task (10).
   */
  function bootstrap() {
    return fetchConfig()
      .then(function (config) {
        renderAll(config);
        // Compute the fit insight once, up front, from the customer's saved
        // usual size (not any grid cell). This populates "Fit for this style"
        // and the fit alert immediately; it is re-run only when the occasion
        // changes. Grid-size clicks never re-run it.
        requestFitInsight();
        return config;
      })
      .catch(function (err) {
        var hint = el("size-hint");
        if (hint) {
          hint.textContent = "Could not load product configuration.";
        }
        if (typeof console !== "undefined" && console.error) {
          console.error("Failed to load /api/config:", err);
        }
        // Non-blocking error banner whose Retry re-runs the bootstrap (Req 8.2).
        showError(
          "We couldn't load the product. Please try again.",
          function () {
            return bootstrap();
          }
        );
      });
  }

  // Expose state and render helpers so later tasks can extend the bootstrap
  // without re-implementing it.
  window.AiFitDemo = {
    UIState: UIState,
    el: el,
    clear: clear,
    elem: elem,
    formatPrice: formatPrice,
    sizeLabel: sizeLabel,
    fetchConfig: fetchConfig,
    selectSize: selectSize,
    renderDisclaimer: renderDisclaimer,
    renderDemoGuide: renderDemoGuide,
    renderProduct: renderProduct,
    renderSizeGrid: renderSizeGrid,
    renderCustomerSummary: renderCustomerSummary,
    updateFitCell: updateFitCell,
    postRecommendation: postRecommendation,
    setPending: setPending,
    appendFitWarnings: appendFitWarnings,
    renderRecommendation: renderRecommendation,
    requestFitInsight: requestFitInsight,
    wireFitInsight: wireFitInsight,
    setFitActionsVisible: setFitActionsVisible,
    wireOccasionButtons: wireOccasionButtons,
    renderFitAlert: renderFitAlert,
    showOccasionQuestion: showOccasionQuestion,
    renderDemoAlternatives: renderDemoAlternatives,
    renderDemoAlternativeCard: renderDemoAlternativeCard,
    isJudgeMode: isJudgeMode,
    applyJudgeMode: applyJudgeMode,
    collectFitWarnings: collectFitWarnings,
    hideAddToBagWarning: hideAddToBagWarning,
    showAddToBagConfirm: showAddToBagConfirm,
    appendConfirmLine: appendConfirmLine,
    markBagged: markBagged,
    addToBag: addToBag,
    wireAddToBag: wireAddToBag,
    postAlternatives: postAlternatives,
    renderAlternativeCard: renderAlternativeCard,
    renderAlternatives: renderAlternatives,
    findAlternatives: findAlternatives,
    renderOccasionMatch: renderOccasionMatch,
    showError: showError,
    hideError: hideError,
    wireErrorBanner: wireErrorBanner,
    appendTraceField: appendTraceField,
    renderActivityStep: renderActivityStep,
    renderActivity: renderActivity,
    wireActivityToggle: wireActivityToggle,
    renderAll: renderAll,
    bootstrap: bootstrap,
  };

  // Kick off once the DOM is ready. The script tag uses `defer`, so the DOM is
  // parsed by the time this runs; guard anyway for direct/early execution.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
