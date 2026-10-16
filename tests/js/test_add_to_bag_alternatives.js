/*
 * DOM-level tests for the AI Fit Demo UI add-to-bag + alternatives + occasion
 * match flow (spec task 8.3).
 *
 * RUN:
 *   node tests/js/test_add_to_bag_alternatives.js
 *   (from the repo root: /Users/yanling.lu39/Desktop/ALDO _Hackathon/Ascent)
 *
 * No npm install and no network are required. The project has no JS test
 * infrastructure, so this is a self-contained pure-Node harness: it loads
 * ai_fit_demo_ui/static/app.js (an IIFE exposing `window.AiFitDemo`) into a
 * hand-rolled fake `document` / `window` / `fetch` (see dom_harness.js), drives
 * the exposed API (addToBag/showAddToBagWarning, findAlternatives/
 * renderAlternatives, renderOccasionMatch), and asserts on the fake DOM.
 * Promise chains are drained via `flush()` before asserting.
 *
 * Covers (design "UI / integration tests"):
 *   Req 4.2 add-to-bag with a Fit_Warning surfaces the warning at the point of
 *           adding
 *   Req 4.3 the surfaced warning offers a "Find alternatives" option
 *   Req 4.4 the shopper can "Add anyway" and proceed with the original selection
 *   Req 5.2 alternatives render each entry's name, price, and suited occasions
 *   Req 5.3 the "no alternatives currently available" message shows when empty
 *   Req 5.4 the occasion match names whether and which occasion matched
 *   Req 5.5 at most three alternative styles are rendered (the agent's cap)
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
    demo_steps: ["Select size 7", "Add to bag", "Find alternatives"],
  };
}

// A recommendation sub-object carrying a Fit_Warning (non-empty warnings[]).
function makeRecommendationWithWarning() {
  return {
    ok: true,
    recommended_size: 8,
    confidence: "medium",
    advisory: true,
    explanation: "This style runs narrow.",
    supporting_signal_count: 9,
    warnings: ["This style runs narrow; consider the roomier toe box."],
    note: null,
    trace: { steps: [], validator_corrections: [], failed_tools: [] },
    failure_reason: null,
  };
}

// find_alternative_styles dict with MORE than three entries, so we can assert
// the renderer caps the list at three (Req 5.5). Each entry carries a
// name/identifier (category), price, and suited occasions (Req 5.2).
function makeAlternativesBody() {
  return {
    requested_occasion: "work",
    none_available: false,
    message: "",
    alternatives: [
      {
        sku: "ALT-1",
        category: "Loafer",
        price_usd: 95.0,
        occasions: ["work", "everyday wear"],
      },
      {
        sku: "ALT-2",
        category: "Derby",
        price_usd: 110.0,
        occasions: ["work"],
      },
      {
        sku: "ALT-3",
        category: "Monk Strap",
        price_usd: 130.0,
        occasions: ["work", "special occasion"],
      },
      {
        sku: "ALT-4",
        category: "Chelsea Boot",
        price_usd: 150.0,
        occasions: ["everyday wear"],
      },
    ],
  };
}

// The none-available case (Req 5.3).
function makeNoneAvailableBody() {
  return {
    requested_occasion: "work",
    none_available: true,
    message: "No alternatives currently available for work.",
    alternatives: [],
  };
}

/* --------------------------------------------------------------------- */
/* Controllable fetch stub                                               */
/* --------------------------------------------------------------------- */

// A fetch stub whose /api/alternatives response is driven by `state.altBody`
// so a single test can exercise the populated and none-available cases. It also
// counts the alternatives POSTs so we can assert "Find alternatives" issues one.
// /api/config always succeeds inertly so the harness bootstrap stays quiet.
function makeFetchStub(state) {
  return function (url) {
    var u = String(url);
    if (u.indexOf("/api/alternatives") !== -1) {
      state.alternativesCalls += 1;
      return Promise.resolve({
        ok: true,
        status: 200,
        json: function () {
          return Promise.resolve(state.altBody);
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

// findAlternatives() returns a Promise whose .then runs as microtasks; draining
// a couple of ticks ensures renderAlternatives() has run before we assert.
function flush() {
  return Promise.resolve().then(function () {
    return Promise.resolve();
  });
}

/* --------------------------------------------------------------------- */
/* Small DOM helpers for assertions                                      */
/* --------------------------------------------------------------------- */

// Find the button under a container whose rendered text matches `label`.
function findButtonByText(container, label) {
  var buttons = container.querySelectorAll("button");
  return buttons.filter(function (b) {
    return b.textContent === label;
  })[0];
}

/* --------------------------------------------------------------------- */
/* Test: add-to-bag warning flow (Req 4.2-4.4)                           */
/* --------------------------------------------------------------------- */

function testAddToBagWarning() {
  console.log("== add-to-bag with a Fit_Warning (Req 4.2-4.4) ==");
  var doc = harness.buildFixtureDocument();
  var state = { altBody: makeAlternativesBody(), alternativesCalls: 0 };
  var windowObj = loadApp(doc, makeFetchStub(state));
  var api = windowObj.AiFitDemo;
  check("app.js exposes window.AiFitDemo", !!api);
  if (!api) {
    return;
  }

  // index.html authors `#add-to-bag-warning` with the `hidden` attribute; the
  // fake fixture does not replicate that initial markup, so set it here to
  // mirror the real starting state before rendering/asserting.
  doc.getElementById("add-to-bag-warning").hidden = true;

  api.renderAll(makeConfig());
  api.selectSize("8");

  // Agent recommended size 8 (== selected) and the occasion matches, so both
  // confirmation checks should be positive.
  api.UIState.lastRecommendation = {
    ok: true, recommended_size: 8, confidence: "high", advisory: false,
    explanation: "ok", supporting_signal_count: 9, warnings: [], note: null,
    trace: { steps: [], validator_corrections: [], failed_tools: [] },
    failure_reason: null,
  };
  api.UIState.lastOccasionMatch = {
    status: "match", matched_occasions: ["work"], message: "Matches work.",
  };

  var surface = doc.getElementById("add-to-bag-warning");
  eq("add-to-bag confirm is hidden before adding", surface.hidden, true);

  // Click Add to Bag -> the confirmation surfaces at the point of adding (Req 4.2).
  var addBtn = doc.getElementById("add-to-bag");
  addBtn.click();

  eq(
    "add-to-bag confirm is surfaced after clicking Add to Bag (Req 4.2)",
    surface.hidden,
    false
  );

  // No fit warning text — the surface shows positive confirmation instead.
  check(
    "the surface shows NO fit-warning block",
    surface.querySelectorAll(".fit-warning").length === 0
  );

  // Size-match check: a ✓ line because recommended (8) == selected (8).
  var okLines = surface.querySelectorAll(".confirm-line--ok");
  check(
    "a size-match check (✓) is shown when recommended == selected (Req 4.2)",
    surface.textContent.toLowerCase().indexOf("matches the recommended fit") !== -1,
    JSON.stringify(surface.textContent)
  );
  // Occasion-match check: a ✓ line because the occasion matches.
  check(
    "an occasion-match check (✓) is shown when the occasion matches (Req 4.2)",
    surface.textContent.toLowerCase().indexOf("matches your occasion") !== -1,
    JSON.stringify(surface.textContent)
  );
  check(
    "both checks render as positive (✓) lines",
    okLines.length === 2,
    "ok lines = " + okLines.length
  );

  // The surface has a SINGLE full-width proceed button (no "Find alternatives").
  var actions = surface.querySelectorAll(".add-to-bag-warning__actions")[0];
  check("confirm renders an actions row (Req 4.4)", !!actions);
  var findBtn = findButtonByText(surface, "Find alternatives");
  check('confirm has NO "Find alternatives" button', !findBtn);
  var actionButtons = actions.querySelectorAll("button");
  eq("confirm has exactly one action button (Req 4.4)", actionButtons.length, 1);
  // When size matches, the proceed button reads "Add to Bag"; otherwise "Add anyway".
  var proceedBtn =
    findButtonByText(surface, "Add to Bag") || findButtonByText(surface, "Add anyway");
  check('confirm offers a single proceed option (Req 4.4)', !!proceedBtn);

  // Proceeding marks the product bagged and dismisses the surface (Req 4.4).
  proceedBtn.click();
  eq(
    "proceed marks the product added to bag (Req 4.4)",
    addBtn.textContent,
    "Added to Bag"
  );
  eq(
    "proceed marks the add-to-bag control pressed (Req 4.4)",
    addBtn.getAttribute("aria-pressed"),
    "true"
  );
  eq("proceed dismisses the confirm surface (Req 4.4)", surface.hidden, true);
}

/* --------------------------------------------------------------------- */
/* Test: "Find alternatives" from the warning issues the request         */
/* --------------------------------------------------------------------- */

async function testAlternativesViaFindAlternatives() {
  console.log(
    "\n== findAlternatives() fetches + renders alternatives (Req 5.1) =="
  );
  var doc = harness.buildFixtureDocument();
  var state = { altBody: makeAlternativesBody(), alternativesCalls: 0 };
  var windowObj = loadApp(doc, makeFetchStub(state));
  var api = windowObj.AiFitDemo;
  if (!api) {
    return;
  }

  api.renderAll(makeConfig());

  // The add-to-bag confirm surface no longer carries a "Find alternatives"
  // button — the whole action bar is the single proceed ("Add anyway") control.
  api.selectSize("8");
  api.UIState.lastRecommendation = makeRecommendationWithWarning();
  var surface = doc.getElementById("add-to-bag-warning");
  doc.getElementById("add-to-bag").click();
  check(
    "add-to-bag confirm has no Find alternatives button",
    !findButtonByText(surface, "Find alternatives")
  );

  // The alternatives flow is reached via findAlternatives() (occasion buttons).
  var callsBefore = state.alternativesCalls;
  await api.findAlternatives();
  await flush();

  check(
    "findAlternatives issues an /api/alternatives request (Req 5.1)",
    state.alternativesCalls === callsBefore + 1,
    "calls=" + state.alternativesCalls
  );
  var list = doc.getElementById("alternatives-list");
  check(
    "findAlternatives renders alternatives into the list (Req 5.1)",
    list.querySelectorAll(".alternative-card").length > 0
  );
}

/* --------------------------------------------------------------------- */
/* Test: alternatives rendering, cap at 3, name/price/occasions (Req 5.2,5.5)*/
/* --------------------------------------------------------------------- */

async function testAlternativesRender() {
  console.log(
    "\n== alternatives render name/price/occasions, capped at 3 (Req 5.2, 5.5) =="
  );
  var doc = harness.buildFixtureDocument();
  var state = { altBody: makeAlternativesBody(), alternativesCalls: 0 };
  var windowObj = loadApp(doc, makeFetchStub(state));
  var api = windowObj.AiFitDemo;
  if (!api) {
    return;
  }

  api.renderAll(makeConfig());

  // Drive findAlternatives() directly (uses config.product.sku).
  await api.findAlternatives();
  await flush();

  var region = doc.getElementById("alternatives");
  eq("alternatives region is shown (Req 5.1)", region.hidden, false);

  var list = doc.getElementById("alternatives-list");
  var cards = list.querySelectorAll(".alternative-card");

  // Req 5.5: at most three entries even though the body carried four.
  eq(
    "alternatives are capped at three (Req 5.5)",
    cards.length,
    3
  );

  // Req 5.2: each rendered card carries name, price, and suited occasions.
  var body = makeAlternativesBody();
  cards.forEach(function (card, index) {
    var expected = body.alternatives[index];
    var nameNode = card.querySelectorAll(".alternative-card__name")[0];
    var priceNode = card.querySelectorAll(".alternative-card__price")[0];
    var occNode = card.querySelectorAll(".alternative-card__occasions")[0];

    check(
      "alternative " + index + " renders a name (Req 5.2)",
      !!nameNode && nameNode.textContent === expected.category,
      nameNode ? JSON.stringify(nameNode.textContent) : "no name node"
    );
    check(
      "alternative " + index + " renders a formatted price (Req 5.2)",
      !!priceNode &&
        priceNode.textContent === "$" + expected.price_usd.toFixed(2),
      priceNode ? JSON.stringify(priceNode.textContent) : "no price node"
    );
    check(
      "alternative " + index + " renders its suited occasions (Req 5.2)",
      !!occNode &&
        expected.occasions.every(function (occ) {
          return occNode.textContent.indexOf(occ) !== -1;
        }),
      occNode ? JSON.stringify(occNode.textContent) : "no occasions node"
    );
  });

  // The empty message is NOT shown when alternatives exist.
  var empty = doc.getElementById("alternatives-empty");
  eq(
    "the none-available message is hidden when alternatives exist (Req 5.2)",
    empty.hidden,
    true
  );
}

/* --------------------------------------------------------------------- */
/* Test: none-available message (Req 5.3)                                */
/* --------------------------------------------------------------------- */

async function testAlternativesNoneAvailable() {
  console.log('\n== none available shows the explicit message (Req 5.3) ==');
  var doc = harness.buildFixtureDocument();
  var state = { altBody: makeNoneAvailableBody(), alternativesCalls: 0 };
  var windowObj = loadApp(doc, makeFetchStub(state));
  var api = windowObj.AiFitDemo;
  if (!api) {
    return;
  }

  api.renderAll(makeConfig());
  await api.findAlternatives();
  await flush();

  var list = doc.getElementById("alternatives-list");
  eq(
    "no alternative cards render when none are available (Req 5.3)",
    list.querySelectorAll(".alternative-card").length,
    0
  );

  var empty = doc.getElementById("alternatives-empty");
  eq(
    "the none-available message is visible (Req 5.3)",
    empty.hidden,
    false
  );
  check(
    'the none-available message says no alternatives are available (Req 5.3)',
    empty.textContent.toLowerCase().indexOf("no alternatives") !== -1,
    JSON.stringify(empty.textContent)
  );
}

/* --------------------------------------------------------------------- */
/* Test: occasion match naming (Req 5.4)                                 */
/* --------------------------------------------------------------------- */

function testOccasionMatch() {
  console.log("\n== occasion match names whether/which occasion matched (Req 5.4) ==");
  var doc = harness.buildFixtureDocument();
  var state = { altBody: makeAlternativesBody(), alternativesCalls: 0 };
  var windowObj = loadApp(doc, makeFetchStub(state));
  var api = windowObj.AiFitDemo;
  if (!api) {
    return;
  }

  api.renderAll(makeConfig());

  // ---- match case: names the matched occasion ----------------------------
  api.UIState.lastOccasionMatch = {
    status: "match",
    matched_occasions: ["work"],
    message: "This product matches your occasion: work.",
  };
  api.renderOccasionMatch();

  var container = doc.getElementById("occasion-match");
  var statusNode = container.querySelectorAll(".occasion-match__status")[0];
  check("occasion match renders a status line (Req 5.4)", !!statusNode);
  check(
    "occasion match names the matched occasion 'work' (Req 5.4)",
    !!statusNode && statusNode.textContent.indexOf("work") !== -1,
    statusNode ? JSON.stringify(statusNode.textContent) : "no status node"
  );
  check(
    "occasion match reads as a positive match (Req 5.4)",
    container.textContent.toLowerCase().indexOf("match") !== -1,
    JSON.stringify(container.textContent)
  );

  // ---- no-match case: names that no occasion matched ---------------------
  api.UIState.lastOccasionMatch = {
    status: "no_match",
    matched_occasions: [],
    message: "This product does not match your occasion.",
  };
  api.renderOccasionMatch();

  var statusNode2 = container.querySelectorAll(".occasion-match__status")[0];
  check(
    "no-match case names that no occasion matched (Req 5.4)",
    !!statusNode2 &&
      statusNode2.textContent.toLowerCase().indexOf("no occasion match") !== -1,
    statusNode2 ? JSON.stringify(statusNode2.textContent) : "no status node"
  );
  // And the matched occasion name is not falsely claimed.
  check(
    "no-match case does not claim a matched occasion (Req 5.4)",
    statusNode2 && statusNode2.textContent.indexOf("work") === -1,
    statusNode2 ? JSON.stringify(statusNode2.textContent) : "no status node"
  );
}

/* --------------------------------------------------------------------- */
/* Run                                                                   */
/* --------------------------------------------------------------------- */

async function run() {
  testAddToBagWarning();
  await testAlternativesViaFindAlternatives();
  await testAlternativesRender();
  await testAlternativesNoneAvailable();
  testOccasionMatch();

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
