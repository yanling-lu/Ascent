/*
 * DOM-level tests for the AI Fit Demo UI error + retry resilience (spec task 10.1).
 *
 * RUN:
 *   node tests/js/test_error_retry.js
 *   (from the repo root: /Users/yanling.lu39/Desktop/ALDO _Hackathon/Ascent)
 *
 * No npm install and no network are required. The project has no JS test
 * infrastructure, so this is a self-contained pure-Node harness: it loads
 * ai_fit_demo_ui/static/app.js (an IIFE exposing `window.AiFitDemo`) into a
 * hand-rolled fake `document` / `window` / `fetch` (see dom_harness.js), drives
 * `renderAll(config)` and `requestFitInsight()`, and asserts on the fake DOM.
 *
 * Covers (design "UI / integration tests"):
 *   Req 8.2 a failed/unreachable request shows a NON-BLOCKING error with Retry,
 *           and Retry re-issues the request and clears the error on success.
 *   Req 8.4 the page stays usable during a failed request: size selection still
 *           records a size and the add-to-bag handler still runs.
 */
"use strict";

var fs = require("fs");
var path = require("path");
var vm = require("vm");
var harness = require("./dom_harness");

/* --------------------------------------------------------------------- */
/* Tiny assertion helpers                                                */
/* --------------------------------------------------------------------- */

var failures = [];
var passed = 0;

function check(label, cond, detail) {
  if (cond) {
    passed += 1;
    console.log("  ok  - " + label);
  } else {
    failures.push(label + (detail ? " :: " + detail : ""));
    console.log("  FAIL- " + label + (detail ? " :: " + detail : ""));
  }
}

function eq(label, actual, expected) {
  check(
    label,
    actual === expected,
    "expected " + JSON.stringify(expected) + ", got " + JSON.stringify(actual)
  );
}

/* --------------------------------------------------------------------- */
/* Config fixture (mirrors /api/config shape)                            */
/* --------------------------------------------------------------------- */

function makeConfig() {
  return {
    product: {
      sku: "SYN-1007",
      name: "Grandbar Dress Oxford",
      price_usd: 120.0,
      color_options: ["Black", "Cognac"],
      image_path: "product.jpg",
      offered_sizes: [7, 7.5, 8, 8.5, 9, 10],
    },
    customer_summary: {
      usual_shoe_size: 8,
      comfort_style_priority: "roomy toe box",
    },
    disclaimer: "Unofficial hackathon prototype. Not an ALDO storefront.",
    demo_steps: ["Select size 7", "Add to bag", "Expand activity"],
  };
}

// A successful /api/recommendation body: ok recommendation + occasion match.
function makeRecommendationBody() {
  return {
    recommendation: {
      ok: true,
      recommended_size: 8,
      confidence: "high",
      advisory: false,
      explanation: "Size 8 should fit well.",
      supporting_signal_count: 12,
      warnings: [],
      note: null,
      trace: { steps: [], validator_corrections: [], failed_tools: [] },
      failure_reason: null,
    },
    occasion_match: { status: "match", matched_occasions: ["work"], message: "" },
  };
}

/* --------------------------------------------------------------------- */
/* Controllable fetch stub                                               */
/* --------------------------------------------------------------------- */

// A fetch stub whose behavior for /api/recommendation is driven by `state.mode`:
//   "reject"  -> network error (Promise rejects)   -> error banner (Req 8.2)
//   "non-ok"  -> resolves with a non-2xx response   -> error banner (Req 8.2)
//   "ok"      -> resolves with a 200 recommendation -> clears the error
// Also counts how many recommendation POSTs were issued so we can assert Retry
// actually re-issues the request. /api/config always succeeds so the harness
// bootstrap (if it runs) stays inert.
function makeFetchStub(state) {
  return function (url, options) {
    var u = String(url);
    if (u.indexOf("/api/recommendation") !== -1) {
      state.recommendationCalls += 1;
      if (state.mode === "reject") {
        return Promise.reject(new Error("network down"));
      }
      if (state.mode === "non-ok") {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: function () {
            return Promise.resolve({ error: "unavailable", retryable: true });
          },
        });
      }
      // ok
      return Promise.resolve({
        ok: true,
        status: 200,
        json: function () {
          return Promise.resolve(makeRecommendationBody());
        },
      });
    }
    // /api/config and anything else: succeed inertly.
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function () {
        return Promise.resolve(makeConfig());
      },
    });
  };
}

/* --------------------------------------------------------------------- */
/* Load app.js into the fake DOM via a vm sandbox                        */
/* --------------------------------------------------------------------- */

function loadApp(doc, fetchStub) {
  var windowObj = {};
  var sandbox = {
    document: doc,
    window: windowObj,
    console: console,
    fetch: fetchStub,
    Promise: Promise,
    Number: Number,
    isFinite: isFinite,
    String: String,
    Object: Object,
    Array: Array,
    JSON: JSON,
    setTimeout: setTimeout,
  };
  windowObj.document = doc;
  sandbox.globalThis = sandbox;

  var appPath = path.resolve(
    __dirname,
    "..",
    "..",
    "ai_fit_demo_ui",
    "static",
    "app.js"
  );
  var code = fs.readFileSync(appPath, "utf8");
  vm.runInNewContext(code, sandbox, { filename: appPath });
  return windowObj;
}

/* --------------------------------------------------------------------- */
/* Promise flush: drain the microtask queue so .then/.catch chains settle */
/* --------------------------------------------------------------------- */

// requestFitInsight() returns a Promise whose .then/.catch run as microtasks;
// awaiting it (and a trailing tick) ensures the error banner / state are updated
// before we assert.
function flush() {
  return Promise.resolve().then(function () {
    return Promise.resolve();
  });
}

/* --------------------------------------------------------------------- */
/* Test run                                                              */
/* --------------------------------------------------------------------- */

async function runWithMode(primaryMode) {
  var doc = harness.buildFixtureDocument();
  var state = { mode: primaryMode, recommendationCalls: 0 };
  var windowObj = loadApp(doc, makeFetchStub(state));
  var api = windowObj.AiFitDemo;
  check("app.js exposes window.AiFitDemo", !!api);
  if (!api) {
    return;
  }

  // index.html authors `#error-banner` with the `hidden` attribute; the fake
  // fixture does not replicate that initial markup, so set it here to mirror the
  // real starting state before rendering/asserting.
  doc.getElementById("error-banner").hidden = true;

  // Render the shell and the error-banner wiring from a known config.
  api.renderAll(makeConfig());

  var banner = doc.getElementById("error-banner");
  var message = doc.getElementById("error-message");
  var retry = doc.getElementById("error-retry");

  eq(
    "error banner is hidden before any failure (" + primaryMode + ")",
    banner.hidden,
    true
  );

  // Select a size so the fit request is actually issued (not blocked by the
  // no-size guard).
  api.selectSize("8");
  eq("size selected before failing request", api.UIState.selectedSize, 8);

  // Trigger the fit-insight request, which fails in this mode.
  await api.requestFitInsight();
  await flush();

  /* ---- Req 8.2: failed request shows a non-blocking error + Retry ---- */
  eq(
    "failed request unhides the error banner (Req 8.2, " + primaryMode + ")",
    banner.hidden,
    false
  );
  check(
    "error banner shows a message (Req 8.2)",
    message.textContent != null && message.textContent.trim() !== "",
    JSON.stringify(message.textContent)
  );
  check(
    "a Retry control is present and visible (Req 8.2)",
    retry != null && retry.hidden !== true,
    retry ? "hidden=" + retry.hidden : "no retry element"
  );
  check(
    "UIState.lastError is set after failure (Req 8.2)",
    api.UIState.lastError != null && String(api.UIState.lastError) !== "",
    JSON.stringify(api.UIState.lastError)
  );
  // The banner is non-blocking: it is position:fixed in styles.css and does not
  // disable the PDP controls. The usability checks below (Req 8.4) prove the
  // page is still interactive while the banner is showing.

  /* ---- Req 8.4: page stays usable during the failed request ---------- */
  // Size selection still records a new size.
  api.selectSize("9");
  eq(
    "size selection still works after a failed request (Req 8.4)",
    api.UIState.selectedSize,
    9
  );
  var grid = doc.getElementById("size-grid");
  var cell9 = grid.querySelectorAll('.size-cell[data-size="9"]')[0];
  check("size cell 9 exists", !!cell9);
  cell9.click();
  eq(
    "clicking a size cell still records it after failure (Req 8.4)",
    api.UIState.selectedSize,
    9
  );
  eq(
    "clicked size cell reflects selection after failure (Req 8.4)",
    cell9.getAttribute("aria-pressed"),
    "true"
  );

  // Add-to-bag handler still runs after a failed request: clicking it opens the
  // confirmation surface (page stays usable), and proceeding bags the product.
  var addBtn = doc.getElementById("add-to-bag");
  addBtn.click();
  var confirm = doc.getElementById("add-to-bag-warning");
  eq(
    "add-to-bag handler still runs after a failed request (Req 8.4)",
    confirm.hidden,
    false
  );
  var proceedBtn =
    (function () {
      var btns = confirm.querySelectorAll("button");
      for (var i = 0; i < btns.length; i++) {
        var t = btns[i].textContent;
        if (t === "Add to Bag" || t === "Add anyway") return btns[i];
      }
      return null;
    })();
  check("confirm surface offers a proceed button after failure (Req 8.4)", !!proceedBtn);
  proceedBtn.click();
  eq(
    "proceeding bags the product after a failed request (Req 8.4)",
    addBtn.textContent,
    "Added to Bag"
  );
  eq(
    "add-to-bag marked pressed after a failed request (Req 8.4)",
    addBtn.getAttribute("aria-pressed"),
    "true"
  );

  /* ---- Req 8.2: Retry re-issues the request and clears the error ----- */
  var callsBeforeRetry = state.recommendationCalls;
  // Flip the stub to succeed, then click Retry.
  state.mode = "ok";
  retry.click();
  await flush();
  await flush();

  check(
    "Retry re-issues the recommendation request (Req 8.2, " + primaryMode + ")",
    state.recommendationCalls === callsBeforeRetry + 1,
    "calls before=" + callsBeforeRetry + " after=" + state.recommendationCalls
  );
  eq(
    "successful retry clears (hides) the error banner (Req 8.2)",
    banner.hidden,
    true
  );
  eq(
    "successful retry clears UIState.lastError (Req 8.2)",
    api.UIState.lastError,
    null
  );
  // The recovered recommendation renders into the fit result.
  var result = doc.getElementById("fit-result");
  check(
    "recovered recommendation renders after retry (Req 8.2)",
    result.textContent.indexOf("8") !== -1,
    JSON.stringify(result.textContent)
  );
}

async function run() {
  console.log("== network error (Promise rejects) ==");
  await runWithMode("reject");
  console.log("\n== non-ok response (503) ==");
  await runWithMode("non-ok");

  console.log(
    "\n" + passed + " checks passed, " + failures.length + " failed."
  );
  if (failures.length) {
    console.error("\nFAILURES:");
    failures.forEach(function (f) {
      console.error("  - " + f);
    });
    process.exit(1);
  }
}

run();
