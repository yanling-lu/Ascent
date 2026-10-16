/*
 * DOM-level tests for the AI Fit Demo UI PDP shell (spec task 6.3).
 *
 * RUN:
 *   node tests/js/test_pdp_shell.js
 *   (from the repo root: /Users/yanling.lu39/Desktop/ALDO _Hackathon/Ascent)
 *
 * No npm install and no network are required. The project has no JS test
 * infrastructure, so this is a self-contained pure-Node harness: it loads
 * ai_fit_demo_ui/static/app.js (an IIFE exposing `window.AiFitDemo`) into a
 * hand-rolled fake `document` / `window` / `fetch` (see dom_harness.js), drives
 * `renderAll(config)` and `selectSize(...)`, and asserts on the fake DOM.
 *
 * Covers (design "UI / integration tests"):
 *   Req 1.1 PDP renders name/price from config
 *   Req 1.2 size grid is built from product.offered_sizes as selectable controls
 *   Req 1.3 the Prototype_Disclaimer renders
 *   Req 1.4 the Demo_Guide steps render and the hide control hides the guide
 *   Req 1.5 no Protected_Attribute appears anywhere in the rendered DOM
 *   Req 2.1 selecting a size records it into UIState.selectedSize
 *   Req 2.2 the customer fit summary shows only usual size + comfort priority
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
/* Load app.js into the fake DOM via a vm sandbox                        */
/* --------------------------------------------------------------------- */

// A config that mirrors the real /api/config shape. It deliberately carries
// ONLY usual_shoe_size + comfort_style_priority in the customer summary, as the
// service forwards (Req 1.5, 2.2). The protectedProbe values below are planted
// nowhere in the config — the scan asserts they never surface.
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
    disclaimer:
      "Unofficial hackathon prototype. Not an ALDO storefront.",
    demo_steps: [
      "Select size 7",
      "Add to bag",
      "See a fit warning",
      "Find alternatives",
      "Expand activity",
    ],
  };
}

// Load app.js in a sandbox whose globals are the fake document/window/fetch.
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

// Collect the strings a shopper can actually perceive: rendered text and the
// *data-bearing* attributes/props that app.js populates from server JSON (src,
// alt, data-* values). It deliberately EXCLUDES the structural `id` and `class`
// attributes — those are authored constants from index.html / app.js (e.g.
// "product-image", "error-message"), not demo-customer data, and matching raw
// substrings against them ("im-age", "mess-age") is a false positive. The
// leakage requirement (Req 1.5) is about the demo customer's protected data
// never reaching the DOM, which is what the rendered text + bound data carries.
function collectDomStrings(node, acc) {
  if (!(node instanceof harness.FakeElement)) {
    return acc;
  }
  // Own text (not the aggregate, to avoid double counting children text).
  if (node._text) {
    acc.push(String(node._text));
  }
  // Data-bearing attribute values only (skip id/class structural constants).
  Object.keys(node._attrs).forEach(function (name) {
    if (name === "id" || name === "class") {
      return;
    }
    acc.push(String(node._attrs[name]));
  });
  // Direct props app.js may set from config/server data.
  if (node.src != null) {
    acc.push(String(node.src));
  }
  if (node.alt != null) {
    acc.push(String(node.alt));
  }
  // dataset values.
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
  // The bootstrap's auto-fetch resolves to a *different* object; we drive
  // renderAll() directly with a known config, so the stub just needs to not
  // throw. Reject-safe: return the same config so bootstrap, if it runs, is inert.
  var config = makeConfig();
  var fetchStub = function () {
    return Promise.resolve({
      ok: true,
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

  // Render the shell deterministically from a known config (Req 1.1-1.5, 2.2).
  api.renderAll(config);

  /* ---- Req 1.1: PDP name/price render -------------------------------- */
  eq(
    "product name renders from config (Req 1.1)",
    doc.getElementById("product-name").textContent,
    "Grandbar Dress Oxford"
  );
  eq(
    "product price renders formatted (Req 1.1)",
    doc.getElementById("product-price").textContent,
    "$120.00"
  );

  /* ---- Req 1.2: size grid built from offered_sizes ------------------- */
  var grid = doc.getElementById("size-grid");
  var cells = grid.querySelectorAll(".size-cell");
  eq(
    "size grid has one cell per offered size (Req 1.2)",
    cells.length,
    config.product.offered_sizes.length
  );
  var cellLabels = cells.map(function (c) {
    return c.textContent;
  });
  var expectedLabels = config.product.offered_sizes.map(function (s) {
    return String(s).trim();
  });
  eq(
    "size grid labels match offered_sizes exactly (Req 1.2)",
    JSON.stringify(cellLabels),
    JSON.stringify(expectedLabels)
  );
  var allButtons = cells.every(function (c) {
    return c.tagName === "BUTTON" && c.getAttribute("data-size") != null;
  });
  check("size cells are selectable controls (buttons) (Req 1.2)", allButtons);

  /* ---- Req 2.1: selecting a size records it -------------------------- */
  eq(
    "no size selected before click (Req 2.1)",
    api.UIState.selectedSize,
    null
  );
  // Click the "7.5" cell.
  var targetCell = cells.filter(function (c) {
    return c.getAttribute("data-size") === "7.5";
  })[0];
  check("size cell for 7.5 exists", !!targetCell);
  targetCell.click();
  eq(
    "clicking a size records it into UIState.selectedSize (Req 2.1)",
    api.UIState.selectedSize,
    7.5
  );
  eq(
    "selected cell is marked aria-pressed=true (Req 2.1)",
    targetCell.getAttribute("aria-pressed"),
    "true"
  );
  var otherPressed = cells.filter(function (c) {
    return c !== targetCell && c.getAttribute("aria-pressed") === "true";
  });
  eq(
    "only one size cell is pressed at a time (Req 2.1)",
    otherPressed.length,
    0
  );

  // selectSize via the exposed API too (used by later tasks).
  api.selectSize("9");
  eq(
    "selectSize(label) records the new size (Req 2.1)",
    api.UIState.selectedSize,
    9
  );

  /* ---- Req 1.3: disclaimer renders ----------------------------------- */
  eq(
    "disclaimer text renders from config (Req 1.3)",
    doc.getElementById("disclaimer-text").textContent,
    config.disclaimer
  );

  /* ---- Req 1.4: demo guide renders + hide control works -------------- */
  var stepsList = doc.getElementById("demo-guide-steps");
  var stepItems = stepsList.querySelectorAll("li");
  eq(
    "demo guide renders one item per step (Req 1.4)",
    stepItems.length,
    config.demo_steps.length
  );
  eq(
    "demo guide step text matches config (Req 1.4)",
    JSON.stringify(
      stepItems.map(function (li) {
        return li.textContent;
      })
    ),
    JSON.stringify(config.demo_steps)
  );
  var guide = doc.getElementById("demo-guide");
  eq("demo guide is visible before hide (Req 1.4)", guide.hidden, false);
  doc.getElementById("demo-guide-hide").click();
  eq(
    "hide control hides the Demo_Guide (Req 1.4)",
    guide.hidden,
    true
  );

  /* ---- Req 2.2: customer summary shows only the two allowed fields --- */
  var summary = doc.getElementById("fit-customer-summary");
  var summaryText = summary.textContent;
  check(
    "customer summary shows the usual size cell (Req 2.2)",
    summaryText.indexOf("Your usual size") !== -1 && summaryText.indexOf("8") !== -1,
    summaryText
  );
  check(
    "customer summary shows the fit-for-this-style cell (Req 2.2)",
    summaryText.indexOf("Fit for this style") !== -1 &&
      summaryText.indexOf("Not yet confirmed") !== -1,
    summaryText
  );
  check(
    "customer summary shows comfort priority (Req 2.2)",
    summaryText.indexOf("Comfort priority") !== -1 &&
      summaryText.indexOf("roomy toe box") !== -1,
    summaryText
  );
  // Exactly the two comparison cells — nothing extra leaked in.
  var cellsSummary = summary.querySelectorAll(".fit-cell");
  eq("customer summary has exactly two comparison cells (Req 2.2)", cellsSummary.length, 2);

  /* ---- Req 1.5: no Protected_Attribute anywhere in the DOM ----------- */
  // Protected attributes per the ai-fit spec: age, gender, ethnicity,
  // nationality, location. We scan every rendered text node and attribute value
  // and assert none of these keys (or obvious example values) appear.
  var domStrings = collectDomStrings(doc.body, []);
  var haystack = domStrings.join("\n").toLowerCase();
  var protectedKeys = ["age", "gender", "ethnicity", "nationality", "location"];
  protectedKeys.forEach(function (key) {
    // Whole-word match so legitimate rendered words that merely contain a key
    // as a substring (e.g. "image" containing "age") are not false positives.
    var wordRe = new RegExp("\\b" + key + "\\b");
    var hit = haystack.match(wordRe);
    check(
      'no protected key "' + key + '" in rendered DOM (Req 1.5)',
      hit === null,
      hit ? "matched word in rendered content" : undefined
    );
  });

  // Also prove the scanner is live: inject a protected value into config, render
  // into a fresh DOM, and confirm the renderers never surface it (they only read
  // usual_shoe_size + comfort_style_priority).
  var doc2 = harness.buildFixtureDocument();
  var windowObj2 = loadApp(doc2, fetchStub);
  var api2 = windowObj2.AiFitDemo;
  var tainted = makeConfig();
  tainted.customer_summary.age = 34;
  tainted.customer_summary.gender = "female";
  tainted.customer_summary.ethnicity = "SECRET-ETHNICITY";
  tainted.customer_summary.nationality = "SECRET-NATION";
  tainted.customer_summary.location = "SECRET-CITY";
  api2.renderAll(tainted);
  var tainted2 = collectDomStrings(doc2.body, []).join("\n");
  ["SECRET-ETHNICITY", "SECRET-NATION", "SECRET-CITY", "female"].forEach(
    function (val) {
      check(
        'protected value "' + val + '" is NOT rendered even when present in config (Req 1.5, 2.2)',
        tainted2.indexOf(val) === -1
      );
    }
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
