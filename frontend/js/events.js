/**
 * Live job feed: consumes `GET /api/events` (server-sent events) and falls
 * back to `GET /api/jobs` polling when SSE is unavailable or keeps failing.
 *
 * The backend emits, once per second the job set changes:
 *   event: jobs
 *   data: [ <GenerationJob>, ... ]
 *
 * Reconnection policy:
 *  - Uses the native EventSource where available.
 *  - On connection error it closes the socket and reschedules with bounded
 *    exponential backoff (1s → 2s → 4s, capped at 10s).
 *  - After MAX_SSE_ATTEMPTS consecutive failures it switches to HTTP polling
 *    (2s cadence) and keeps periodically re-attempting SSE (every 20s) so
 *    live updates resume as soon as the backend is reachable again.
 *  - `onStatus` reports `live | reconnecting | polling | offline` so the UI
 *    can surface the feed state honestly.
 */

import { listJobs } from "./api.js";

const MAX_SSE_ATTEMPTS = 4;
const BASE_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 10000;
const POLL_INTERVAL_MS = 2000;
const SSE_REVIVAL_MS = 20000;

/**
 * @typedef {"live"|"reconnecting"|"polling"|"offline"} FeedStatus
 */

/**
 * @param {object} opts
 * @param {import("./config.js").LvsConfig} opts.config
 * @param {(jobs: import("./api.js").GenerationJob[]) => void} opts.onJobs
 * @param {(status: FeedStatus) => void} [opts.onStatus]
 * @returns {{stop: () => void}}
 */
export function createJobFeed({ config, onJobs, onStatus }) {
  /** @type {FeedStatus} */ let status = "offline";
  /** @type {"sse"|"polling"} */ let phase = "sse";
  let stopped = false;
  let es = null;
  let sseFailures = 0;
  let pollTimer = null;
  let retryTimer = null;
  let revivalTimer = null;
  let pollErrorCount = 0;

  function setStatus(next) {
    if (next === status) return;
    status = next;
    if (onStatus) onStatus(status);
  }

  function emitJobs(jobs) {
    if (Array.isArray(jobs)) onJobs(jobs);
  }

  function closeEs() {
    if (es) {
      es.close();
      es = null;
    }
  }

  function clearRevival() {
    if (revivalTimer) {
      clearTimeout(revivalTimer);
      revivalTimer = null;
    }
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  /**
   * Polling fallback (phase "polling"): a 2s poll chain plus an independent
   * 20s revival chain that keeps re-attempting SSE until it succeeds.
   *
   * Timer-chain invariants while polling:
   *  - `pollTimer` is the id of the one pending poll tick, or null while a
   *    tick is actually running. A tick that fires while the feed has (even
   *    momentarily) switched to the "sse" phase clears the id instead of
   *    rescheduling, so a subsequent startPolling() never mistakes a dead
   *    chain for a live one.
   *  - `revivalTimer` is re-armed unconditionally after every revival
   *    attempt (scheduleRevival), so the retry is periodic and a failed
   *    revival always leaves exactly two live chains: the poll tick and the
   *    next revival.
   */
  function startPolling() {
    phase = "polling";
    setStatus("polling");
    if (!pollTimer) {
      const tick = async () => {
        if (stopped || phase !== "polling") {
          pollTimer = null;
          return;
        }
        try {
          const res = await listJobs(config);
          // The feed state may have flipped while the request was in flight
          // (an SSE revival went live, or stop() ran): never emit stale
          // jobs, revert the status, or reschedule from a dead chain.
          if (stopped || phase !== "polling") {
            pollTimer = null;
            return;
          }
          pollErrorCount = 0;
          emitJobs(res.jobs || []);
          // A success proves the backend is reachable again: recover the
          // status from the temporary "offline" report back to "polling".
          setStatus("polling");
        } catch {
          pollErrorCount += 1;
          if (pollErrorCount >= 3 && phase === "polling") setStatus("offline");
        }
        pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
      };
      tick();
    }
    if (!revivalTimer) scheduleRevival();
  }

  /**
   * Arm the next SSE revival probe. Re-armed after every attempt so the
   * retry stays periodic until SSE is live again (jobs listener clears it
   * via stopRevivalFallback) or the feed is stopped.
   */
  function scheduleRevival() {
    clearRevival();
    revivalTimer = setTimeout(() => {
      revivalTimer = null;
      if (!stopped && phase === "polling") {
        attemptSse(true);
        scheduleRevival();
      }
    }, SSE_REVIVAL_MS);
  }

  function stopRevivalFallback() {
    stopPolling();
    clearRevival();
  }

  /**
   * Open (or re-open) the SSE connection.
   * @param {boolean} [revival] — true when probing from the polling fallback;
   *   a failed probe just stays in polling (the fallback is already running),
   *   without burning through the reconnect budget.
   */
  function attemptSse(revival = false) {
    if (stopped) return;
    if (typeof EventSource === "undefined") {
      startPolling();
      return;
    }
    phase = "sse";
    const url = `${config.apiBase}/api/events`;
    es = new EventSource(url);
    es.addEventListener("jobs", (ev) => {
      sseFailures = 0;
      if (phase === "polling") stopRevivalFallback();
      phase = "sse";
      setStatus("live");
      try {
        emitJobs(JSON.parse(ev.data));
      } catch {
        /* ignore a malformed frame */
      }
    });
    es.onerror = () => {
      closeEs();
      sseFailures += 1;
      if (revival || sseFailures >= MAX_SSE_ATTEMPTS) {
        // A failed revival (or SSE exhausting its retries) re-enters polling.
        // startPolling() is idempotent per chain, so exactly one live poll
        // chain and one live revival chain survive any interleaving.
        startPolling();
        return;
      }
      setStatus("reconnecting");
      const delay = Math.min(BASE_BACKOFF_MS * 2 ** (sseFailures - 1), MAX_BACKOFF_MS);
      retryTimer = setTimeout(attemptSse, delay);
    };
  }

  function stop() {
    stopped = true;
    closeEs();
    stopPolling();
    clearRevival();
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    setStatus("offline");
  }

  attemptSse();
  return { stop };
}
