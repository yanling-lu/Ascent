/*
 * DOM-level tests for the AI Fit Demo UI fit-insight trigger + recommendation
 * rendering (spec task 7.3).
 *
 * RUN:
 *   node tests/js/test_fit_insight.js
 *   (from the repo root: /Users/yanling.lu39/Desktop/ALDO _Hackathon/Ascent)
 *
 * No npm install and no network are required. The project has no JS test
 * infrastructure, so this is a self-contained pure-Node harness: it loads
 * ai_fit_demo_ui/static/app.js (an IIFE exposing `window.AiFitDemo`) into a
 * hand-rolled fake `document` / `window` / `fetch` (see dom_harness.js), drives
 * `renderAll(config)`, `selectSize(...)`, and `requestFitInsight()`, and asserts
 * on the fake DOM. A controllable fetch stub (with a deferred promise for the
 * in-flight observation) lets each case shape the /api/recommendation response.
 *
 * Covers (design "UI / integration tests"):
 *   Req 2.5 no-size guard: activating fit-insight with no size prompts
 *           "please select a size" in #size-hint and issues NO request.
 *   Req 2.4 pending: while the request is in flight, #fit-pending shows and the
 *           prior #fit-result is cleared/greyed (is-stale / aria-busy) so it is
 *           not read as current.
 *   Req 3.1-3.5 success render: a 200 ok:true renders recommended_size,
 *           confidence, explanation, supporting_signal_count (evidence),
 *           advisory framing when advisory:true, each warning + note as
 *           .fit-warning, and a highlighted mismatch when recommended_size
 *           !== selectedSize.
 *   Req 8.1 failure: an ok:false response renders .fit-result__failure and shows
 *           NO size/confidence.
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
// draining a couple of ticks ensures the result/pending state is updated before
// we assert.
function flush() {
  return Promise.resolve()
    .then(function () {})
    .then(function () {})
    .then(function () {});
}

/* --------------------------------------------------------------------- */
/* Case 1: fit insight is anchored on the customer's usual size (Req 2.5) */
/* --------------------------------------------------------------------- */

// The fit insight is computed from the customer's SAVED usual size (from
// /api/config), not from the grid cell the shopper taps to buy. So it runs even
// with no grid selection, and the guard only fires when the config carries no
// usual size AND no grid size is selected.
async function testUsualSizeAnchor() {
  console.log("== fit insight anchored on usual size (Req 2.5) ==");

  // --- With a usual size in config: the insight runs without a grid pick. ---
  var doc = harness.buildFixtureDocument();
  var calls = { recommendation: 0, lastBody: null };
  var fetchStub = function (url, options) {
    var u = String(url);
    if (u.indexOf("/api/recommendation") !== -1) {
      calls.recommendation += 1;
      try {
        calls.lastBody = JSON.parse(options.body);
      } catch (e) {
        calls.lastBody = null;
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: function () {
          return Promise.resolve({
            recommendation: {
              ok: true, recommended_size: 8, confidence: "high",
              advisory: false, explanation: "ok", supporting_signal_count: 5,
              warnings: [], note: null,
              trace: { steps: [], validator_corrections: [], failed_tools: [] },
              failure_reason: null,
            },
            occasion_match: { status: "match", matched_occasions: ["work"], message: "" },
          });
        },
      });
    }
    return Promise.resolve({
      ok: true, status: 200,
      json: function () { return Promise.resolve(makeConfig()); },
    });
  };

  var api = loadApp(doc, fetchStub).AiFitDemo;
  check("app.js exposes window.AiFitDemo", !!api);
  if (!api) {
    return;
  }
  api.renderAll(makeConfig());

  eq("no grid size selected", api.UIState.selectedSize, null);

  // The IIFE auto-runs bootstrap() (readyState "complete"), which fires one
  // insight on load. Measure increments from here so the test is robust to that.
  await flush();
  var callsBefore = calls.recommendation;

  await api.requestFitInsight();
  await flush();

  eq(
    "insight runs from the saved usual size even with no grid pick (Req 2.5)",
    calls.recommendation,
    callsBefore + 1
  );
  // The request's size is the customer's usual size (8 in makeConfig), NOT a
  // grid cell. This is what keeps "Fit for this style" fixed on the agent's
  // recommendation regardless of which buy-size is tapped.
  check(
    "the request uses the customer's usual size as stated_usual_size (Req 2.5)",
    calls.lastBody && Number(calls.lastBody.size) === 8,
    JSON.stringify(calls.lastBody)
  );

  // Tapping a different grid size does NOT re-run the insight.
  var cell10 = doc
    .getElementById("size-grid")
    .querySelectorAll('.size-cell[data-size="10"]')[0];
  var afterInsight = calls.recommendation;
  if (cell10) {
    cell10.click();
    await flush();
  }
  eq(
    "clicking a grid size does not re-run the fit insight (Req 2.5)",
    calls.recommendation,
    afterInsight
  );

  // --- With NO usual size and no selection: the guard fires, no request. ---
  var doc2 = harness.buildFixtureDocument();
  var calls2 = { recommendation: 0 };
  var stub2 = function (url) {
    if (String(url).indexOf("/api/recommendation") !== -1) {
      calls2.recommendation += 1;
    }
    return Promise.resolve({
      ok: true, status: 200,
      json: function () {
        var c = makeConfig();
        c.customer_summary.usual_shoe_size = null; // no saved usual size
        return Promise.resolve(c);
      },
    });
  };
  var api2 = loadApp(doc2, stub2).AiFitDemo;
  var noUsual = makeConfig();
  noUsual.customer_summary.usual_shoe_size = null;
  api2.renderAll(noUsual);
  var before2 = calls2.recommendation;
  await api2.requestFitInsight();
  await flush();
  var hint2 = doc2.getElementById("size-hint");
  check(
    "guard prompts to select a size when no usual size + no pick (Req 2.5)",
    hint2.textContent.toLowerCase().indexOf("select a size") !== -1,
    JSON.stringify(hint2.textContent)
  );
  eq(
    "guard issues NO request when no usual size + no pick (Req 2.5)",
    calls2.recommendation,
    before2
  );
}

/* --------------------------------------------------------------------- */
/* Case 2: pending state hides the prior result (Req 2.4)                */
/* --------------------------------------------------------------------- */

async function testPendingHidesPrior() {
  console.log("\n== pending hides the prior result (Req 2.4) ==");
  var doc = harness.buildFixtureDocument();

  // A deferred we resolve manually so we can observe the in-flight state before
  // the response settles.
  var deferred = {};
  deferred.promise = new Promise(function (resolve) {
    deferred.resolve = resolve;
  });

  var fetchStub = function (url) {
    var u = String(url);
    if (u.indexOf("/api/recommendation") !== -1) {
      return deferred.promise;
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function () {
        return Promise.resolve(makeConfig());
      },
    });
  };

  var api = loadApp(doc, fetchStub).AiFitDemo;
  if (!api) {
    check("app.js exposes window.AiFitDemo", false);
    return;
  }
  api.renderAll(makeConfig());

  // Seed a prior result into #fit-result so we can prove it is cleared/greyed
  // while the next request is in flight.
  var result = doc.getElementById("fit-result");
  result.appendChild(api.elem("div", { text: "PRIOR-RESULT-TEXT" }));
  check(
    "a prior result is present before the next request",
    result.textContent.indexOf("PRIOR-RESULT-TEXT") !== -1
  );

  // index.html authors `#fit-pending` with the `hidden` attribute; the fake
  // fixture does not replicate that initial markup, so set it here to mirror the
  // real starting state before firing the request.
  var pending = doc.getElementById("fit-pending");
  pending.hidden = true;
  eq("pending indicator starts hidden", pending.hidden, true);

  // Select a size so the request is actually issued, then fire it. We do NOT
  // await here: the deferred keeps the request in flight so we can observe it.
  api.selectSize("8");
  var inFlight = api.requestFitInsight();
  await flush();

  /* ---- in-flight assertions (Req 2.4) ---- */
  eq("UIState.pending is true while in flight (Req 2.4)", api.UIState.pending, true);
  eq("#fit-pending is shown while in flight (Req 2.4)", pending.hidden, false);
  check(
    "the prior result is cleared while in flight (Req 2.4)",
    result.textContent.indexOf("PRIOR-RESULT-TEXT") === -1,
    JSON.stringify(result.textContent)
  );
  check(
    "the prior result is greyed (is-stale) while in flight (Req 2.4)",
    result.classList.contains("is-stale")
  );
  eq(
    "the prior result is marked aria-busy while in flight (Req 2.4)",
    result.getAttribute("aria-busy"),
    "true"
  );

  // Now settle the request with an ok recommendation and confirm pending clears.
  deferred.resolve({
    ok: true,
    status: 200,
    json: function () {
      return Promise.resolve({
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
      });
    },
  });
  await inFlight;
  await flush();

  eq("UIState.pending clears after settle (Req 2.4)", api.UIState.pending, false);
  eq("#fit-pending hidden after settle (Req 2.4)", pending.hidden, true);
  check(
    "result is no longer greyed after settle (Req 2.4)",
    !result.classList.contains("is-stale")
  );
  eq(
    "aria-busy removed after settle (Req 2.4)",
    result.getAttribute("aria-busy"),
    null
  );
}

/* --------------------------------------------------------------------- */
/* Case 3: success render (Req 3.1-3.5)                                  */
/* --------------------------------------------------------------------- */

async function testSuccessRender() {
  console.log("\n== success render (Req 3.1-3.5) ==");
  var doc = harness.buildFixtureDocument();

  // Recommended size 9 differs from the selected size 8 -> mismatch highlight.
  var body = {
    recommendation: {
      ok: true,
      recommended_size: 9,
      confidence: "medium",
      advisory: true,
      explanation: "Based on your usual size, 9 gives a roomier toe box.",
      supporting_signal_count: 7,
      warnings: ["Runs narrow for a wide foot."],
      note: "Consider sizing up half a size for thick socks.",
      trace: { steps: [], validator_corrections: [], failed_tools: [] },
      failure_reason: null,
    },
    occasion_match: { status: "match", matched_occasions: ["work"], message: "" },
  };

  var fetchStub = function (url) {
    var u = String(url);
    if (u.indexOf("/api/recommendation") !== -1) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: function () {
          return Promise.resolve(body);
        },
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function () {
        return Promise.resolve(makeConfig());
      },
    });
  };

  var api = loadApp(doc, fetchStub).AiFitDemo;
  if (!api) {
    check("app.js exposes window.AiFitDemo", false);
    return;
  }
  api.renderAll(makeConfig());

  // Select size 8 (differs from recommended 9) and request.
  api.selectSize("8");
  await api.requestFitInsight();
  await flush();

  var result = doc.getElementById("fit-result");

  /* ---- Req 3.1: recommended size, confidence, explanation ---- */
  var sizeNode = result.querySelectorAll(".fit-result__size")[0];
  check("recommended size node rendered (Req 3.1)", !!sizeNode);
  // Note: in the mismatch case the size node appends the mismatch <span> as a
  // child, so the harness surfaces the child's text for the node's textContent.
  // The recommended value is asserted directly in the no-mismatch control case
  // below (where the size node has no child span); here we assert the node and
  // the mismatch highlight that names the selected size (Req 3.5).

  var confNode = result.querySelectorAll(".fit-result__confidence")[0];
  check(
    "confidence rendered with the level (Req 3.1)",
    !!confNode && confNode.textContent.indexOf("medium") !== -1,
    confNode ? JSON.stringify(confNode.textContent) : "no confidence node"
  );

  var explNode = result.querySelectorAll(".fit-result__explanation")[0];
  check(
    "plain-language explanation rendered (Req 3.1)",
    !!explNode && explNode.textContent === body.recommendation.explanation,
    explNode ? JSON.stringify(explNode.textContent) : "no explanation node"
  );

  /* ---- Req 3.2: advisory framing when advisory===true ---- */
  var advisoryNode = result.querySelectorAll(".fit-result__advisory")[0];
  check(
    "advisory framing present when advisory===true (Req 3.2)",
    !!advisoryNode,
    advisoryNode ? advisoryNode.textContent : "no advisory node"
  );

  /* ---- Req 3.3: supporting_signal_count as evidence ---- */
  var evidenceNode = result.querySelectorAll(".fit-result__evidence")[0];
  check(
    "evidence shows supporting_signal_count (Req 3.3)",
    !!evidenceNode && evidenceNode.textContent.indexOf("7") !== -1,
    evidenceNode ? JSON.stringify(evidenceNode.textContent) : "no evidence node"
  );

  /* ---- Req 3.4: each warning + note as a Fit_Warning ---- */
  var warningNodes = result.querySelectorAll(".fit-warning");
  eq(
    "one Fit_Warning per warning + the note (Req 3.4)",
    warningNodes.length,
    2
  );
  var warningText = warningNodes
    .map(function (n) {
      return n.textContent;
    })
    .join("\n");
  check(
    "the warning entry is rendered as a Fit_Warning (Req 3.4)",
    warningText.indexOf("Runs narrow for a wide foot.") !== -1,
    JSON.stringify(warningText)
  );
  check(
    "the note is rendered as a Fit_Warning (Req 3.4)",
    warningText.indexOf("Consider sizing up half a size for thick socks.") !== -1,
    JSON.stringify(warningText)
  );

  /* ---- Req 3.5: mismatch highlighted when recommended !== selected ---- */
  var mismatchNode = result.querySelectorAll(".fit-result__mismatch")[0];
  check(
    "mismatch highlighted when recommended size differs from selected (Req 3.5)",
    !!mismatchNode,
    mismatchNode ? JSON.stringify(mismatchNode.textContent) : "no mismatch node"
  );
  check(
    "mismatch names the selected size (Req 3.5)",
    mismatchNode && mismatchNode.textContent.indexOf("8") !== -1,
    mismatchNode ? JSON.stringify(mismatchNode.textContent) : "no mismatch node"
  );

  /* ---- control: no mismatch when recommended === selected ---- */
  var doc2 = harness.buildFixtureDocument();
  var matchBody = JSON.parse(JSON.stringify(body));
  matchBody.recommendation.recommended_size = 8; // equals the selection
  matchBody.recommendation.warnings = [];
  matchBody.recommendation.note = null;
  var fetchStub2 = function (url) {
    var u = String(url);
    if (u.indexOf("/api/recommendation") !== -1) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: function () {
          return Promise.resolve(matchBody);
        },
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function () {
        return Promise.resolve(makeConfig());
      },
    });
  };
  var api2 = loadApp(doc2, fetchStub2).AiFitDemo;
  api2.renderAll(makeConfig());
  api2.selectSize("8");
  await api2.requestFitInsight();
  await flush();
  var result2 = doc2.getElementById("fit-result");
  eq(
    "no mismatch highlight when recommended size equals selected (Req 3.5)",
    result2.querySelectorAll(".fit-result__mismatch").length,
    0
  );
  // With no mismatch span appended, the size node's own label text is intact,
  // so this is where we assert the recommended value renders (Req 3.1).
  var sizeNode2 = result2.querySelectorAll(".fit-result__size")[0];
  check(
    "recommended size shows the recommended value (Req 3.1)",
    !!sizeNode2 && sizeNode2.textContent.indexOf("8") !== -1,
    sizeNode2 ? JSON.stringify(sizeNode2.textContent) : "no size node"
  );
}

/* --------------------------------------------------------------------- */
/* Case 4: failure render (Req 8.1)                                      */
/* --------------------------------------------------------------------- */

async function testFailureRender() {
  console.log("\n== failure render ok:false (Req 8.1) ==");
  var doc = harness.buildFixtureDocument();

  var body = {
    recommendation: {
      ok: false,
      recommended_size: null,
      confidence: null,
      advisory: false,
      explanation: null,
      supporting_signal_count: null,
      warnings: [],
      note: null,
      trace: { steps: [], validator_corrections: [], failed_tools: [] },
      failure_reason: "We don't have enough fit history for this style yet.",
    },
    occasion_match: { status: "no_match", matched_occasions: [], message: "" },
  };

  var fetchStub = function (url) {
    var u = String(url);
    if (u.indexOf("/api/recommendation") !== -1) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: function () {
          return Promise.resolve(body);
        },
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function () {
        return Promise.resolve(makeConfig());
      },
    });
  };

  var api = loadApp(doc, fetchStub).AiFitDemo;
  if (!api) {
    check("app.js exposes window.AiFitDemo", false);
    return;
  }
  api.renderAll(makeConfig());

  api.selectSize("8");
  await api.requestFitInsight();
  await flush();

  var result = doc.getElementById("fit-result");

  /* ---- Req 8.1: failure message shown ---- */
  var failureNode = result.querySelectorAll(".fit-result__failure")[0];
  check(
    "ok:false renders the customer-facing failure message (Req 8.1)",
    !!failureNode &&
      failureNode.textContent.indexOf(
        "We don't have enough fit history for this style yet."
      ) !== -1,
    failureNode ? JSON.stringify(failureNode.textContent) : "no failure node"
  );

  /* ---- Req 8.1: NO fabricated size/confidence ---- */
  eq(
    "no recommended size rendered on failure (Req 8.1)",
    result.querySelectorAll(".fit-result__size").length,
    0
  );
  eq(
    "no confidence rendered on failure (Req 8.1)",
    result.querySelectorAll(".fit-result__confidence").length,
    0
  );
  eq(
    "no evidence rendered on failure (Req 8.1)",
    result.querySelectorAll(".fit-result__evidence").length,
    0
  );
}

/* --------------------------------------------------------------------- */
/* Run all cases                                                         */
/* --------------------------------------------------------------------- */

async function run() {
  await testUsualSizeAnchor();
  await testPendingHidesPrior();
  await testSuccessRender();
  await testFailureRender();

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
