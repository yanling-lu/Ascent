/*
 * DOM-level tests for the AI Fit Demo UI masked activity trace (spec task 9.2).
 *
 * RUN:
 *   node tests/js/test_activity_trace.js
 *   (from the repo root: /Users/yanling.lu39/Desktop/ALDO _Hackathon/Ascent)
 *
 * No npm install and no network are required. The project has no JS test
 * infrastructure, so this is a self-contained pure-Node harness: it loads
 * ai_fit_demo_ui/static/app.js (an IIFE exposing `window.AiFitDemo`) into a
 * hand-rolled fake `document` / `window` / `fetch` (see dom_harness.js), drives
 * `renderAll(config)` + the activity toggle, and asserts on the fake DOM.
 *
 * Covers (design "UI / integration tests"):
 *   Req 6.5 the activity control DEFAULTS to COLLAPSED (`#activity-body` hidden,
 *           `#activity-toggle` aria-expanded="false").
 *   Req 6.1 the expandable control, on expand, shows the trace: `#activity-body`
 *           becomes visible and aria-expanded flips to "true".
 *   Req 6.2 the ordered steps, the tools invoked, and the validator corrections
 *           render.
 *   Req 6.3 a masked identifier (`customer_uid` / `cnsmr_prof_sid`) is shown as
 *           the redaction placeholder VERBATIM and flagged (.activity-masked);
 *           no resolved identifier value appears.
 *   Req 6.4 a failed tool / unavailable data renders as "tool failed / data
 *           unavailable" (.activity-failed).
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
/* Fixtures                                                              */
/* --------------------------------------------------------------------- */

// The redaction placeholder emitted by the trace builder for masked identifiers
// (ai_fit.trace.builder.MASK_PLACEHOLDER). The server masks before serializing;
// the browser renders it verbatim and NEVER resolves it (Req 6.3).
var MASK = "***MASKED***";

// Raw identifier values that must NEVER surface in the DOM. These are the kind of
// values the server would have redacted; the test asserts they are absent.
var RAW_CUSTOMER_UID = "CUST-12345";
var RAW_CNSMR_PROF_SID = "SID-98765";

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

// A masked trace mirroring the Recommendation.trace shape the service forwards
// field-for-field: ordered steps[] (order, step_type, tool_name, data_summary,
// decision), validator_corrections[], and failed_tools[]. The step data_summary
// carries the redaction placeholder exactly as the trace builder emitted it for
// customer_uid / cnsmr_prof_sid (Req 6.3) — the raw values are NOT present here.
function makeTrace() {
  return {
    steps: [
      {
        order: 1,
        step_type: "load_profile",
        tool_name: "get_customer_profile",
        data_summary: {
          customer_uid: MASK,
          cnsmr_prof_sid: MASK,
          usual_shoe_size: 7.5,
        },
        decision: "Loaded saved fit profile.",
      },
      {
        order: 2,
        step_type: "size_reasoning",
        tool_name: "size_model",
        data_summary: { signals: 12 },
        decision: "Recommend size 8 for a roomy toe box.",
      },
    ],
    validator_corrections: [
      {
        invariant: "no_size_change_from_width_warning",
        proposed_value: 9,
        corrected_value: 8,
      },
    ],
    failed_tools: [
      { tool_name: "occasion_history", reason: "timeout" },
    ],
    masked_fields: ["customer_uid", "cnsmr_prof_sid"],
  };
}

// A full /api/recommendation body carrying the masked trace.
function makeRecommendation() {
  return {
    ok: true,
    recommended_size: 8,
    confidence: "high",
    advisory: false,
    explanation: "Size 8 should fit well.",
    supporting_signal_count: 12,
    warnings: [],
    note: null,
    trace: makeTrace(),
    failure_reason: null,
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
/* Recursive DOM scan: collect all rendered text + attribute values      */
/* --------------------------------------------------------------------- */

function collectDomStrings(node, acc) {
  if (!(node instanceof harness.FakeElement)) {
    return acc;
  }
  if (node._text) {
    acc.push(String(node._text));
  }
  Object.keys(node._attrs).forEach(function (name) {
    if (name === "id" || name === "class") {
      return;
    }
    acc.push(String(node._attrs[name]));
  });
  Object.keys(node.dataset).forEach(function (k) {
    acc.push(String(node.dataset[k]));
  });
  node.childNodes.forEach(function (c) {
    collectDomStrings(c, acc);
  });
  return acc;
}

/* --------------------------------------------------------------------- */
/* Test run                                                              */
/* --------------------------------------------------------------------- */

function run() {
  var doc = harness.buildFixtureDocument();
  var fetchStub = function () {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function () {
        return Promise.resolve(makeConfig());
      },
    });
  };

  var windowObj = loadApp(doc, fetchStub);
  var api = windowObj.AiFitDemo;
  check("app.js exposes window.AiFitDemo", !!api);
  if (!api) {
    return;
  }

  // The masked trace is JUDGES-ONLY: it is hidden from shoppers and only
  // revealed in judge mode. These tests exercise the judge-visible trace, so
  // enable judge mode before rendering.
  windowObj.AI_FIT_JUDGE = true;

  // index.html authors `#activity` + `#activity-body` with the `hidden`
  // attribute and `#activity-toggle` with aria-expanded="false"; the fake
  // fixture does not replicate those initial attributes, so set them here to
  // mirror the real starting state before rendering/asserting.
  doc.getElementById("activity").hidden = true;
  doc.getElementById("activity-body").hidden = true;
  doc.getElementById("activity-toggle").setAttribute("aria-expanded", "false");

  // Render the shell (wires the activity toggle via renderAll -> wireActivityToggle).
  api.renderAll(makeConfig());

  var activity = doc.getElementById("activity");
  var body = doc.getElementById("activity-body");
  var toggle = doc.getElementById("activity-toggle");

  /* ---- Judges-only: the trace section is revealed in judge mode ------ */
  eq("activity trace is visible in judge mode", activity.hidden, false);

  /* ---- Req 6.5: default collapsed ------------------------------------ */
  eq("activity body is hidden by default (Req 6.5)", body.hidden, true);
  eq(
    "activity toggle aria-expanded is 'false' by default (Req 6.5)",
    toggle.getAttribute("aria-expanded"),
    "false"
  );
  eq(
    "UIState.activityExpanded is false by default (Req 6.5)",
    api.UIState.activityExpanded,
    false
  );

  /* ---- Set the recommendation trace onto UIState --------------------- */
  // A recommendation arrives (as task 7 would store it) carrying the masked
  // trace. The activity renders on expand (Req 6.1).
  api.UIState.lastRecommendation = makeRecommendation();

  /* ---- Req 6.1: expand shows the trace ------------------------------- */
  toggle.click();
  eq(
    "clicking the toggle expands UIState.activityExpanded (Req 6.1)",
    api.UIState.activityExpanded,
    true
  );
  eq("activity body becomes visible on expand (Req 6.1)", body.hidden, false);
  eq(
    "activity toggle aria-expanded flips to 'true' on expand (Req 6.1)",
    toggle.getAttribute("aria-expanded"),
    "true"
  );

  /* ---- Req 6.2: ordered steps + tools + validator corrections -------- */
  var steps = body.querySelectorAll(".activity-step");
  eq("renders one block per ordered step (Req 6.2)", steps.length, 2);

  // Steps render in order 1, 2 (the renderer walks trace.steps[] in sequence).
  var firstStepText = steps[0].textContent;
  var secondStepText = steps[1].textContent;
  check(
    "first step shows its order (Req 6.2)",
    firstStepText.indexOf("Step 1") !== -1,
    firstStepText
  );
  check(
    "second step shows its order (Req 6.2)",
    secondStepText.indexOf("Step 2") !== -1,
    secondStepText
  );

  // Tools invoked render per step (.activity-step__tool).
  var toolNodes = body.querySelectorAll(".activity-step__tool");
  eq("renders the tool invoked per step (Req 6.2)", toolNodes.length, 2);
  var toolText = toolNodes
    .map(function (n) {
      return n.textContent;
    })
    .join("\n");
  check(
    "first step names its tool get_customer_profile (Req 6.2)",
    toolText.indexOf("get_customer_profile") !== -1,
    toolText
  );
  check(
    "second step names its tool size_model (Req 6.2)",
    toolText.indexOf("size_model") !== -1,
    toolText
  );

  // Validator corrections render.
  var bodyText = body.textContent;
  check(
    "validator corrections section renders (Req 6.2)",
    bodyText.indexOf("Validator corrections") !== -1,
    bodyText
  );
  check(
    "a correction's invariant renders (Req 6.2)",
    bodyText.indexOf("no_size_change_from_width_warning") !== -1,
    bodyText
  );

  /* ---- Req 6.3: masked identifier shown verbatim + flagged ----------- */
  var maskedNodes = body.querySelectorAll(".activity-masked");
  check(
    "a masked trace field is flagged with .activity-masked (Req 6.3)",
    maskedNodes.length >= 1,
    "found " + maskedNodes.length
  );
  var maskedText = maskedNodes
    .map(function (n) {
      return n.textContent;
    })
    .join("\n");
  check(
    "the redaction placeholder is rendered VERBATIM (Req 6.3)",
    maskedText.indexOf(MASK) !== -1,
    maskedText
  );

  // No resolved identifier value appears anywhere in the rendered DOM (Req 6.3).
  var domStrings = collectDomStrings(doc.body, []);
  var haystack = domStrings.join("\n");
  check(
    "raw customer_uid value is NOT rendered (Req 6.3)",
    haystack.indexOf(RAW_CUSTOMER_UID) === -1
  );
  check(
    "raw cnsmr_prof_sid value is NOT rendered (Req 6.3)",
    haystack.indexOf(RAW_CNSMR_PROF_SID) === -1
  );

  /* ---- Req 6.4: failed tool / unavailable data ----------------------- */
  var failedNodes = body.querySelectorAll(".activity-failed");
  check(
    "a failed tool renders with .activity-failed (Req 6.4)",
    failedNodes.length >= 1,
    "found " + failedNodes.length
  );
  var failedText = failedNodes
    .map(function (n) {
      return n.textContent;
    })
    .join("\n");
  check(
    "failed tool is named (Req 6.4)",
    failedText.indexOf("occasion_history") !== -1,
    failedText
  );
  check(
    "failed tool reads 'tool failed / data unavailable' (Req 6.4)",
    failedText.indexOf("tool failed") !== -1 &&
      failedText.indexOf("data unavailable") !== -1,
    failedText
  );

  /* ---- Req 6.1/6.5: collapsing again hides the body ------------------ */
  toggle.click();
  eq(
    "clicking the toggle again collapses it (Req 6.1, 6.5)",
    api.UIState.activityExpanded,
    false
  );
  eq("activity body is hidden again after collapse (Req 6.5)", body.hidden, true);
  eq(
    "activity toggle aria-expanded is 'false' after collapse (Req 6.5)",
    toggle.getAttribute("aria-expanded"),
    "false"
  );

  /* ---- Judges-only: trace is HIDDEN from shoppers (no judge mode) ----- */
  var doc2 = harness.buildFixtureDocument();
  var win2 = loadApp(doc2, fetchStub); // no AI_FIT_JUDGE, no ?judge=1
  doc2.getElementById("activity").hidden = true;
  win2.AiFitDemo.renderAll(makeConfig());
  eq(
    "activity trace is hidden from shoppers when not in judge mode",
    doc2.getElementById("activity").hidden,
    true
  );

  /* ---- summary ------------------------------------------------------- */
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
