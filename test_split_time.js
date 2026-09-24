const assert = require("assert");
const { parseSplitOffsetSeconds, parseSplitList } = require("./static/app.js");

const songStart = 8 * 3600 + 48 * 60 + 25;
const splitOffset = parseSplitOffsetSeconds("12:10");

assert.strictEqual(splitOffset, 12 * 60 + 10);
assert.strictEqual(songStart + splitOffset, 9 * 3600 + 35);
assert.deepStrictEqual(parseSplitList("12:10"), [730]);
assert.deepStrictEqual(parseSplitList("12.10"), [730]);

console.log("split time parser ok");
