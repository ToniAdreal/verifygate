"use strict";
/* Standalone reproduction script for the cart tax fix (spec check #2). */
const assert = require("node:assert/strict");
const { total } = require("./cart.js");

const result = total([{ price: 10, qty: 2 }], 0.2);
assert.equal(result, 24, `cart repro failed: got ${result}`);
console.log("repro OK:", result);
