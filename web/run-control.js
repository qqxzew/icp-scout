"use strict";

// Starting a run, and reporting the state of the one that is going.
//
// This lived inside app.js until the week became the landing screen and
// grew a run button of its own. Both screens need the same behaviour -
// press, poll, say how long it has been going, say what happened - and a
// second copy of it is the copy nobody updates. week.js already carries a
// comment about exactly that failure, from when the production-mode
// labels were duplicated and quietly stopped matching.
//
// What differs between the two screens is only where to go when a run
// finishes: the brief screen has to open the week it just produced, the
// week screen is already looking at it and only has to re-read it. That
// is the one thing the caller passes in.
//
//   mountRunControl({ button, title, sub, since, sinceValue, onFinished })
//
// Everything but `button` may be null - the caller decides how much of
// the state it has room to show.

function mountRunControl(parts) {
  const button = parts.button;
  if (!button) return null;

  const title = parts.title || null;
  const sub = parts.sub || null;
  const since = parts.since || null;
  const sinceValue = parts.sinceValue || null;
  const onFinished = parts.onFinished || null;
  // The label to fall back to, read off the page rather than written
  // here: the two screens word it differently ("Spustit běh" against
  // "Spustit nový běh") and neither wording belongs in this file.
  const idleTitle = title ? title.textContent : "";
  const idleSub = sub ? sub.textContent : "";

  // Polled while a run is going. Kept in a variable so a second press, or
  // a reload landing on a run already in progress, does not start a second
  // loop asking the same question twice a second.
  let watching = null;
  let startedAt = null;
  let lastRunAt = null;

  button.addEventListener("click", startRun);

  async function startRun() {
    if (button.dataset.busy === "1") return;
    setRunning("Spouštím…");

    try {
      const response = await fetch("/api/run", { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      applyState(await response.json());
    } catch (error) {
      failRun("Běh se nepodařilo spustit — běží API?");
      console.error("Could not start the run", error);
    }
  }

  // The subprocess reports nothing back on its own, so the page asks. Two
  // seconds: a run takes minutes, and the only thing a shorter interval
  // buys is a busier log.
  function watch() {
    if (watching) return;
    watching = setInterval(refreshRun, 2000);
  }

  function stopWatching() {
    clearInterval(watching);
    watching = null;
  }

  async function refreshRun() {
    try {
      const response = await fetch("/api/run");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      applyState(await response.json());
    } catch (error) {
      // A failed poll is not a failed run - the server may be restarting
      // under --reload. Keep the interval and try again.
      console.error("Could not read the run state", error);
    }
  }

  function applyState(state) {
    renderSince(state.last_run);

    if (state.running) {
      startedAt = state.started_at ? new Date(state.started_at) : startedAt || new Date();
      setRunning(runningFor());
      watch();
      return;
    }

    stopWatching();

    // Nothing was started from this page and nothing is going: the idle
    // screen, whatever happened before it.
    if (button.dataset.busy !== "1") {
      setIdle();
      return;
    }

    if (state.returncode === 0) {
      setRunning("Hotovo — otevírám týden…");
      if (onFinished) onFinished();
      return;
    }

    // The code itself is not shown: a killed process reports 4294967295 on
    // Windows, which tells the salesperson nothing the log does not say
    // better.
    failRun("Běh selhal — podrobnosti v /api/run/log");
  }

  function setRunning(label) {
    button.dataset.busy = "1";
    // Cleared here and not only in setIdle(): starting again after a
    // failed run has to stop looking like the failure it is replacing.
    button.dataset.failed = "0";
    button.setAttribute("aria-busy", "true");
    if (title) title.textContent = "Běh probíhá";
    if (sub) sub.textContent = label;
  }

  function setIdle() {
    button.dataset.busy = "0";
    button.removeAttribute("aria-busy");
    button.dataset.failed = "0";
    if (title) title.textContent = idleTitle;
    if (sub) sub.textContent = idleSub;
  }

  function failRun(message) {
    stopWatching();
    button.dataset.busy = "0";
    button.dataset.failed = "1";
    button.removeAttribute("aria-busy");
    if (title) title.textContent = idleTitle;
    if (sub) sub.textContent = message;
  }

  function runningFor() {
    if (!startedAt) return "Probíhá…";
    const seconds = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
    if (seconds < 60) return `Probíhá ${seconds} s`;
    const minutes = Math.floor(seconds / 60);
    return `Probíhá ${minutes} min ${seconds % 60} s`;
  }

  /* time since the last run ---------------------------------------------- */

  function renderSince(lastRun) {
    lastRunAt = lastRun && lastRun.generated_at ? new Date(lastRun.generated_at) : null;
    drawSince();
  }

  // Redrawn on a timer of its own, because the number changes while
  // nothing else on the page does. A minute is the smallest unit shown, so
  // a minute is often enough to never be visibly stale.
  setInterval(drawSince, 30000);

  function drawSince() {
    if (!since || !sinceValue) return;
    if (!lastRunAt || Number.isNaN(lastRunAt.getTime())) {
      since.hidden = true;
      return;
    }
    since.hidden = false;
    sinceValue.textContent = elapsed(lastRunAt);
    since.title = `Poslední dokončený běh: ${lastRunAt.toLocaleString("cs-CZ")}`;
  }

  // "před" takes the instrumental, so it is dnem/dny here and not the
  // nominative den/dny/dní a counter would use. One is dnem, everything
  // above it is dny. Hours and minutes are abbreviated, which sidesteps
  // the same question for them.
  function elapsed(when) {
    const total = Math.max(0, Math.floor((Date.now() - when) / 1000));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);

    if (total < 60) return "právě teď";

    const parts = [];
    if (days) parts.push(`${days} ${days === 1 ? "dnem" : "dny"}`);
    if (hours) parts.push(`${hours} h`);
    // Minutes are noise next to days, and the timer is a freshness cue,
    // not a stopwatch.
    if (minutes && !days) parts.push(`${minutes} min`);
    return `před ${parts.join(" ")}`;
  }

  return { refresh: refreshRun };
}
