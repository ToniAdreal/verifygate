"use strict";
/* Tiny cart helpers (fictional demo library for the VerifyGate JS sample).
 *
 * This candidate is intentionally broken: total() ignores the tax rate
 * entirely and never rounds to cents, so the spec's checks must catch it.
 */

function subtotal(items) {
  return items.reduce((sum, item) => sum + item.price * item.qty, 0);
}

function total(items, taxRate) {
  return subtotal(items);
}

module.exports = { subtotal, total };
