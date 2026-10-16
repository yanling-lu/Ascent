/*
 * Curated demo alternatives (presentation-only).
 *
 * For the demo, when the shopper picks an occasion (Everyday / Special occasion),
 * the alternatives region shows these hand-picked ALDO styles instead of the
 * agent's /api/alternatives output. This is a DISPLAY choice for the scripted
 * demo; the fit recommendation itself remains agent-backed. Product photos are
 * real ALDO images served from /static, and each card links to the live ALDO
 * product page.
 *
 * Exposed on window.AiFitDemoAlternatives so app.js can pick it up when present
 * and fall back to the live agent endpoint when it is not.
 */
(function () {
  "use strict";

  var ALDO = "https://www.aldoshoes.com/en-ca";

  var PRODUCTS = {
    Kaylin: {
      name: "Kaylin",
      reason: "Similar look with a lower heel. Pointed toe may still feel snug.",
      img: "/static/alt_kaylin.jpg",
      url: ALDO + "/products/kaylin-dark-red",
      cta: "View on ALDO \u2197",
    },
    Stellare: {
      name: "Stellare",
      reason: "Open toe for more toe room, plus a stable block heel.",
      img: "/static/alt_stellare.jpg",
      url: ALDO + "/products/stellare-dark-red",
      cta: "Browse on ALDO \u2197",
      badge: "Best Seller",
    },
    Kiaria: {
      name: "Kiaria",
      reason: "Elegant slingback look. Pointed toe may still feel snug.",
      img: "/static/alt_kiaria.jpg",
      url: ALDO + "/products/kiaria-dark-red",
      cta: "Browse on ALDO \u2197",
    },
    Livianna: {
      name: "Livianna",
      reason: "Closest to the classic pump look. Pointed toe may still feel snug.",
      img: "/static/alt_livianna.jpg",
      url: ALDO + "/products/livianna-black",
      cta: "Browse on ALDO \u2197",
    },
  };

  // Occasion -> curated pairing + section copy, mirroring the demo mock.
  var OCCASIONS = {
    "everyday wear": {
      title: "For everyday wear",
      intro: "Kaylin and Stellare: two options for everyday wear.",
      picks: ["Kaylin", "Stellare"],
    },
    "special occasion": {
      title: "For a special occasion",
      intro: "Kiaria and Livianna: two elegant alternatives.",
      picks: ["Kiaria", "Livianna"],
    },
  };

  window.AiFitDemoAlternatives = {
    products: PRODUCTS,
    occasions: OCCASIONS,
    forOccasion: function (occasion) {
      return OCCASIONS[occasion] || null;
    },
  };
})();
