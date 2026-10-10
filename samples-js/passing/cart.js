"use strict";
/* Tiny cart helpers (fictional demo library for the VerifyGate JS sample). */

function subtotal(items) {
  return items.reduce((sum, item) => sum + item.price * item.qty, 0);
}

function total(items, taxRate) {
  /* Apply the tax rate to the subtotal, rounded to cents. */
  return Math.round(subtotal(items) * (1 + taxRate) * 100) / 100;
}

module.exports = { subtotal, total };
