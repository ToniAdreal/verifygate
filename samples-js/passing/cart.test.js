"use strict";
/* Unit tests for the cart sample — run by Node's built-in test runner
 * (`node --test`), zero dependencies. Spec check #1. */
const test = require("node:test");
const assert = require("node:assert/strict");
const { subtotal, total } = require("./cart.js");

const BASKET = [
  { price: 10, qty: 2 },
  { price: 5.5, qty: 1 },
];

test("subtotal sums price * qty", () => {
  assert.equal(subtotal(BASKET), 25.5);
  assert.equal(subtotal([]), 0);
});

test("total applies the tax rate", () => {
  assert.equal(total(BASKET, 0.2), 30.6);
});

test("total rounds to cents", () => {
  assert.equal(total([{ price: 9.99, qty: 3 }], 0.07), 32.07);
});

test("total with zero tax equals the subtotal", () => {
  assert.equal(total(BASKET, 0), 25.5);
});
