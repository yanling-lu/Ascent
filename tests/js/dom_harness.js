/*
 * Minimal hand-rolled DOM + window harness for DOM-level tests of
 * ai_fit_demo_ui/static/app.js.
 *
 * The project has NO JS test infrastructure (no package.json / node_modules) —
 * it is a Python/pytest project — but Node is available. app.js is written as an
 * IIFE that reads `document` / `window` / `fetch` off the global scope and
 * exposes its API on `window.AiFitDemo`. This harness builds just enough of a
 * fake DOM to drive and assert on that API, with no network install required.
 *
 * It implements only the surface app.js actually touches:
 *   document: getElementById, createElement, addEventListener, readyState
 *   element:  firstChild / childNodes / appendChild / removeChild,
 *             className, textContent, setAttribute / getAttribute /
 *             removeAttribute, addEventListener + click dispatch,
 *             querySelectorAll (tag, .class, [attr], [attr="v"], and
 *             combinations), classList (add/remove/contains), hidden, dataset,
 *             src / alt.
 *
 * See test_pdp_shell.js for the run command and the assertions.
 */
"use strict";

/* --------------------------------------------------------------------- */
/* Tiny selector support for querySelectorAll                            */
/* --------------------------------------------------------------------- */

// Parse a simple compound selector token like `button`, `.size-cell`,
// `[aria-pressed="true"]`, or `.color-swatch[aria-pressed="true"]` into a
// predicate. We intentionally support only what app.js uses.
function compileSelector(selector) {
  var sel = String(selector).trim();
  var tag = null;
  var classes = [];
  var attrs = []; // {name, value|null}

  var re = /([.#]?[\w-]+)|\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]/g;
  var m;
  while ((m = re.exec(sel)) !== null) {
    if (m[2]) {
      attrs.push({ name: m[2], value: m[3] != null ? m[3] : null });
    } else {
      var token = m[1];
      if (token[0] === ".") {
        classes.push(token.slice(1));
      } else if (token[0] === "#") {
        attrs.push({ name: "id", value: token.slice(1) });
      } else {
        tag = token.toLowerCase();
      }
    }
  }

  return function (node) {
    if (tag && node.tagName.toLowerCase() !== tag) {
      return false;
    }
    for (var i = 0; i < classes.length; i++) {
      if (!node.classList.contains(classes[i])) {
        return false;
      }
    }
    for (var j = 0; j < attrs.length; j++) {
      var a = attrs[j];
      var have = node.getAttribute(a.name);
      if (have == null) {
        return false;
      }
      if (a.value != null && String(have) !== a.value) {
        return false;
      }
    }
    return true;
  };
}

/* --------------------------------------------------------------------- */
/* classList                                                             */
/* --------------------------------------------------------------------- */

function makeClassList(node) {
  function tokens() {
    return (node.className || "").split(/\s+/).filter(Boolean);
  }
  return {
    add: function (name) {
      var t = tokens();
      if (t.indexOf(name) === -1) {
        t.push(name);
        node.className = t.join(" ");
      }
    },
    remove: function (name) {
      node.className = tokens()
        .filter(function (x) {
          return x !== name;
        })
        .join(" ");
    },
    contains: function (name) {
      return tokens().indexOf(name) !== -1;
    },
  };
}

/* --------------------------------------------------------------------- */
/* Fake element                                                          */
/* --------------------------------------------------------------------- */

function FakeElement(tagName) {
  this.tagName = String(tagName).toUpperCase();
  this.childNodes = [];
  this.parentNode = null;
  this._attrs = {};
  this._text = "";
  this._listeners = {};
  this.className = "";
  this.hidden = false;
  this.dataset = {};
  this.classList = makeClassList(this);
  // Common direct properties app.js assigns.
  this.src = undefined;
  this.alt = undefined;
}

Object.defineProperty(FakeElement.prototype, "firstChild", {
  get: function () {
    return this.childNodes.length ? this.childNodes[0] : null;
  },
});

Object.defineProperty(FakeElement.prototype, "textContent", {
  get: function () {
    if (this.childNodes.length) {
      return this.childNodes
        .map(function (c) {
          return c.textContent;
        })
        .join("");
    }
    return this._text;
  },
  set: function (value) {
    // Setting textContent clears children (standard DOM behavior).
    this.childNodes = [];
    this._text = value == null ? "" : String(value);
  },
});

FakeElement.prototype.appendChild = function (child) {
  if (child.parentNode) {
    child.parentNode.removeChild(child);
  }
  child.parentNode = this;
  this.childNodes.push(child);
  return child;
};

FakeElement.prototype.removeChild = function (child) {
  var idx = this.childNodes.indexOf(child);
  if (idx !== -1) {
    this.childNodes.splice(idx, 1);
    child.parentNode = null;
  }
  return child;
};

FakeElement.prototype.setAttribute = function (name, value) {
  this._attrs[name] = String(value);
  if (name === "id") {
    this.id = String(value);
  }
  if (name.indexOf("data-") === 0) {
    var key = name
      .slice(5)
      .replace(/-([a-z])/g, function (_, c) {
        return c.toUpperCase();
      });
    this.dataset[key] = String(value);
  }
};

FakeElement.prototype.getAttribute = function (name) {
  return Object.prototype.hasOwnProperty.call(this._attrs, name)
    ? this._attrs[name]
    : null;
};

FakeElement.prototype.removeAttribute = function (name) {
  delete this._attrs[name];
};

FakeElement.prototype.addEventListener = function (type, handler) {
  (this._listeners[type] = this._listeners[type] || []).push(handler);
};

// Depth-first collection of descendants matching the selector.
FakeElement.prototype.querySelectorAll = function (selector) {
  var pred = compileSelector(selector);
  var out = [];
  (function walk(node) {
    node.childNodes.forEach(function (child) {
      if (child instanceof FakeElement) {
        if (pred(child)) {
          out.push(child);
        }
        walk(child);
      }
    });
  })(this);
  // Give the result a forEach (NodeList-like); arrays already have it.
  return out;
};

FakeElement.prototype.dispatch = function (type) {
  var handlers = this._listeners[type] || [];
  var evt = { type: type, target: this };
  handlers.slice().forEach(function (h) {
    h.call(this, evt);
  }, this);
};

// Convenience used by tests.
FakeElement.prototype.click = function () {
  this.dispatch("click");
};

/* --------------------------------------------------------------------- */
/* Fake document                                                         */
/* --------------------------------------------------------------------- */

function FakeDocument() {
  this._byId = {};
  this.readyState = "complete";
  this._listeners = {};
  this.body = new FakeElement("body");
}

FakeDocument.prototype.createElement = function (tag) {
  return new FakeElement(tag);
};

FakeDocument.prototype.getElementById = function (id) {
  return Object.prototype.hasOwnProperty.call(this._byId, id)
    ? this._byId[id]
    : null;
};

FakeDocument.prototype.addEventListener = function (type, handler) {
  (this._listeners[type] = this._listeners[type] || []).push(handler);
};

// Register an element under an id so getElementById can find it.
FakeDocument.prototype.register = function (id, node) {
  node.setAttribute("id", id);
  this._byId[id] = node;
  return node;
};

/* --------------------------------------------------------------------- */
/* Build the fixture DOM that mirrors index.html's stable element ids    */
/* --------------------------------------------------------------------- */

function buildFixtureDocument() {
  var doc = new FakeDocument();

  function reg(id, tag, parent) {
    var node = new FakeElement(tag || "div");
    doc.register(id, node);
    if (parent) {
      parent.appendChild(node);
    }
    return node;
  }

  // Disclaimer banner
  var disclaimer = reg("prototype-disclaimer", "div", doc.body);
  reg("disclaimer-text", "span", disclaimer);

  // Demo guide
  var guide = reg("demo-guide", "section", doc.body);
  reg("demo-guide-hide", "button", guide);
  reg("demo-guide-steps", "ol", guide);

  // PDP
  var pdp = reg("pdp", "section", doc.body);
  reg("product-image", "img", pdp);
  reg("product-name", "h1", pdp);
  reg("product-price", "div", pdp);
  reg("color-swatches", "div", pdp);
  reg("size-grid", "div", pdp);
  reg("size-hint", "p", pdp);
  reg("add-to-bag", "button", pdp);
  reg("add-to-bag-warning", "div", pdp);

  // Fit panel
  var fit = reg("fit-panel", "aside", doc.body);
  reg("fit-customer-summary", "div", fit);
  reg("get-fit-insight", "button", fit);
  reg("fit-pending", "div", fit);
  reg("fit-result", "div", fit);
  reg("occasion-match", "div", fit);
  var activity = reg("activity", "section", fit);
  reg("activity-toggle", "button", activity);
  reg("activity-body", "div", activity);

  // Alternatives + error banner
  var alts = reg("alternatives", "section", doc.body);
  reg("alternatives-list", "div", alts);
  reg("alternatives-empty", "p", alts);
  var errBanner = reg("error-banner", "div", doc.body);
  reg("error-message", "span", errBanner);
  reg("error-retry", "button", errBanner);

  return doc;
}

module.exports = {
  FakeElement: FakeElement,
  FakeDocument: FakeDocument,
  buildFixtureDocument: buildFixtureDocument,
  compileSelector: compileSelector,
};
