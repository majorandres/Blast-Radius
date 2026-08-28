// Day 3 acceptance: the full loop driven through the browser, not through curl.
// Clicks Inject, waits for the detector to name a domain, clicks Reveal, and
// captures the scored result.
//
// It starts from a genuinely quiet system on purpose. An earlier version saw
// the *previous* run's incident still on screen and reported "diagnosis in 0s",
// which measured nothing -- the same stale-state trap the pytest fixture hit.
import { chromium } from "playwright";

const URL = process.env.APP_URL || "http://frontend:5173/";
const OUT = "/out";
const log = (...a) => console.log("[smoke]", ...a);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
await page.goto(URL, { waitUntil: "networkidle" });

// --- clear whatever the last run left behind ---
const stop = page.getByRole("button", { name: /^Stop$/ });
if (await stop.count()) {
  log("stopping a run that is still live");
  await stop.click();
  await page.waitForTimeout(3000);
}

const inject = page.getByRole("button", { name: /Inject blind fault/ });
await inject.waitFor({ state: "visible" });
if (await inject.isDisabled()) {
  log("a finished run is still unrevealed; revealing it to clear the slate");
  await page.getByRole("button", { name: /^Reveal/ }).click();
  await page.waitForTimeout(3000);
}

log("waiting for the incident to close and the system to go quiet");
await page.getByText("No active incident").waitFor({ timeout: 240000 });
await page.screenshot({ path: `${OUT}/loop-1-healthy.png`, fullPage: true });
log("system is quiet");

// --- the loop ---
log("clicking Inject");
const injectedAt = Date.now();
await inject.click();

await page.locator(".verdict-headline .domain").filter({ hasText: "promo-provider" })
  .waitFor({ timeout: 200000 });
const diagnosisSeconds = Math.round((Date.now() - injectedAt) / 1000);
log(`domain named after ${diagnosisSeconds}s`);

// The blast-radius claim needs enough abnormal traces behind it; until then the
// card honestly says so, and asserting against that text would be asserting
// against a placeholder.
await page.locator(".summary").filter({ hasText: "explains the concentration" })
  .waitFor({ timeout: 120000 });
const blastRadiusSeconds = Math.round((Date.now() - injectedAt) / 1000);

const summary = (await page.locator(".summary").innerText()).replace(/\s+/g, " ");
log(`blast radius after ${blastRadiusSeconds}s:`, summary);
await page.screenshot({ path: `${OUT}/loop-2-diagnosed.png`, fullPage: true });

log("clicking Reveal");
await page.getByRole("button", { name: /^Reveal/ }).click();
await page.locator(".result h3").waitFor({ timeout: 30000 });

const verdict = (await page.locator(".result h3").innerText()).trim();
const detail = (await page.locator(".result .detail").innerText()).replace(/\s+/g, " ");
log("RESULT:", verdict);
await page.screenshot({ path: `${OUT}/loop-3-revealed.png`, fullPage: true });

console.log(JSON.stringify({ diagnosisSeconds, blastRadiusSeconds, verdict, detail, summary }, null, 2));

// Revealing scores the run; it does not end it. Without this the dispatcher
// keeps holding the fault for the rest of its window and every later check
// runs against a degraded system.
const stopAfter = page.getByRole("button", { name: /^Stop$/ });
if (await stopAfter.count()) {
  log("stopping the run so the system returns to healthy");
  await stopAfter.click();
  await page.waitForTimeout(2000);
}

await browser.close();
if (!verdict.includes("CORRECT") || verdict.includes("INCORRECT")) {
  console.error("[smoke] FAILED: expected CORRECT");
  process.exit(1);
}
log("PASS");
process.exit(0);
