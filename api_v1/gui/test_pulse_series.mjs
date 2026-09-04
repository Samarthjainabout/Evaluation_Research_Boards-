import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import test from "node:test";

// Evaluate the actual chart functions without starting timers, network calls,
// or hardware controls. The section after this marker only binds the UI.
const source = readFileSync(new URL("./static/app.js", import.meta.url), "utf8");
const boundary = source.indexOf("\nels.runSelect.addEventListener");
assert.ok(boundary > 0);
const context = { document: { getElementById: () => null } };
runInNewContext(source.slice(0, boundary), context);
const series = (rows) => JSON.parse(JSON.stringify(context.buildPulseSeries(rows)));
const read = (stage, current = 10, col = 0) => ({
  operation: "read", stage, current_uA: current, cellAddress: { row: 5, col }, ok: true,
});
const pulse = (operation, col = 0) => ({
  operation, stage: `${operation}_pulse`, cellAddress: { row: 5, col },
  vcc_set_V: 2.5, vcc_wl_set_V: 1.5, ok: true,
});

test('live characterization uses the original GUI without an additional panel', () => {
  const html=readFileSync(new URL('./static/index.html', import.meta.url),'utf8');
  assert.ok(!html.includes('characterizationPanel'));
  assert.ok(html.includes('id="chart"'));
  assert.ok(html.includes('id="tickerText"'));
  assert.ok(source.includes('mergeCharacterizationFeed(data, await live.json())'));
});

test('compatibility feed shows current pilot and prevents competing GUI starts', () => {
  const data={state:{run:{id:'old',updated:1}},runs:[],runningCommands:[]};
  const feed={published:99,state:{run:{id:'pilot',updated:90,path:'runs/pilot'},
    characterization:{status:'running',cell:[0,0],ageSeconds:1}}};
  const merged=context.mergeCharacterizationFeed(data,feed,100);
  assert.equal(merged.state.run.id,'pilot');
  assert.equal(merged.runningCommands[0].canKill,false);
  assert.equal(merged.runningCommands[0].operation,'characterization');
  context.mergeCharacterizationFeed(data,feed,200);
  // The first merge has already supplied the characterization state; it remains readable.
  assert.equal(merged.state.run.id,'pilot');
});

test('stale compatibility snapshot is explicitly labeled, not presented as fresh', () => {
  const data={state:{run:{id:'old',updated:1}},runs:[],runningCommands:[]};
  const feed={published:1,state:{run:{id:'pilot',updated:2},characterization:{status:'running',cell:[0,0],ageSeconds:1}}};
  context.mergeCharacterizationFeed(data,feed,150);
  assert.match(data.state.characterization.error,/stale/);
});

test("cycle ticker shows last recorded rails with their actual operation", () => {
  const summary = {run: {id: "current"}, last: {...read("read"), vcc_set_V: 0.5, vcc_wl_set_V: 2.5}};
  const event = context.activeCommandEvent({operation: "cycle", row: 0, col: 0, runDir: "api_v1\\runs\\current"}, summary);
  assert.equal(event.vcc_set_V, 0.5);
  assert.match(context.formatApiEvent(event), /last recorded READ: Vcc 0.50 V \/ WL 2.50 V/);
});

test("ticker never borrows rails from a different run", () => {
  const event = context.activeCommandEvent({operation: "cycle", runDir: "runs/new"},
    {run: {id: "old"}, last: pulse("set")});
  assert.equal(event.vcc_set_V, undefined);
  assert.match(context.formatApiEvent(event), /voltage telemetry unavailable/);
});

test("live requested rails take precedence over last recorded rails", () => {
  const event = context.activeCommandEvent({operation: "cycle", runDir: "runs/current",
    activeVccSet_V: 2.5, activeVccWlSet_V: 0.44},
    {run: {id: "current"}, last: {...read("read"), vcc_set_V: 0.5, vcc_wl_set_V: 2.5}});
  assert.equal(event.vcc_set_V, 2.5);
  assert.equal(event.voltageSource, "requested");
});

test("old SET and new RESET each use one position per programming pulse", () => {
  const rows = [
    pulse("set"), read("level_read_after_set", 20),
    pulse("set"), read("level_read_after_set", 25),
    read("read_before_reset", 26), pulse("reset"), read("read_after_reset", 15),
    read("read_before_reset", 16), pulse("reset"), read("read_after_reset", 8),
  ];
  const original = JSON.stringify(rows);
  const out = series(rows);
  assert.deepEqual(out.map((point) => point.op), ["set", "set", "reset", "reset"]);
  assert.deepEqual(out.map((point) => point.current), [40, 50, 30, 16]);
  assert.deepEqual(out.map((point) => point.beforeCurrent), [null, null, 52, 32]);
  assert.equal(JSON.stringify(rows), original, "saved history must not be mutated");
});

test("new SET before-reads use the same compact grouping as RESET", () => {
  const out = series([read("read_before_set", 5), pulse("set"), read("read_after_set", 20)]);
  assert.equal(out.length, 1);
  assert.equal(out[0].beforeCurrent, 10);
  assert.equal(out[0].current, 40);
});

test("latest before-read remains visible until the pulse arrives", () => {
  const rows = [read("read_before_reset", 26)];
  assert.equal(series(rows)[0].current, 52);
  rows.push(pulse("reset"));
  const out = series(rows);
  assert.equal(out.length, 1);
  assert.equal(out[0].current, null, "before-read must not masquerade as after-read");
  assert.equal(out[0].beforeCurrent, 52);
});

test("pure reads and confirmation reads are retained", () => {
  assert.deepEqual(series([read("read", 4), read("read", 5)]).map((p) => p.current), [8, 10]);
  const rows = [read("read_before_set"), pulse("set"), read("read_after_set", 71),
    ...Array.from({ length: 10 }, () => read("confirm_read", 72))];
  const out = series(rows);
  assert.equal(out.length, 11);
  assert.equal(out.filter((p) => p.op === "read").length, 10);
});

test("before-read without a matching pulse is not hidden or paired backwards", () => {
  const rows = [pulse("set"), read("read_before_reset", 12), read("confirm_read", 13)];
  const out = series(rows);
  assert.ok(out.some((p) => p.op === "read" && p.current === 24));
  assert.equal(series([read("read_before_set"), pulse("reset")]).length, 2);
  assert.equal(series([read("read_before_reset", 10, 0), pulse("reset", 1)]).length, 2);
});

test("failed before-reads and failed pulses do not hide the before-read", () => {
  assert.equal(series([{ ...read("read_before_reset"), ok: false }, pulse("reset")]).length, 2);
  assert.equal(series([read("read_before_reset"), { ...pulse("reset"), ok: false }]).length, 2);
});

test("read from a different cell cannot fill a programming-pulse point", () => {
  const out = series([pulse("reset", 0), read("read", 10, 1)]);
  assert.equal(out.length, 2);
  assert.equal(out[0].current, null);
});

test("chart limit is applied after grouping; source rows are all preserved", () => {
  const rows = Array.from({ length: 90 }, (_, i) => [
    read("read_before_reset", i), pulse("reset"), read("read_after_reset", i + 1),
  ]).flat();
  const out = series(rows);
  assert.equal(out.length, 80);
  assert.equal(out[0].beforeCurrent, 20);
  assert.equal(out.at(-1).current, 180);
  assert.equal(rows.length, 270);
});
