import { describe, it, expect } from "vitest";
import { toggleError, togglePending, type ToggleAck } from "./toggleStatus";

const ack = (state: "pending" | "accepted" | "rejected" | "unknown", error: string | null = null) => ({
  state,
  error,
});

describe("toggleError", () => {
  it("surfaces the engine's own reason on a rejected arm", () => {
    // The exact string PYRAMID refuses with, which no operator had ever seen (#257).
    const reason =
      "no identifiable bracket-tagged protective stop on this position — Pyramid needs one to compute R";
    expect(toggleError(ack("rejected", reason))).toBe(reason);
  });

  it("still says something when the engine rejects without a reason", () => {
    // The regression this exists to prevent: a refusal that renders as nothing at all.
    expect(toggleError(ack("rejected"))).toBe("the engine refused this");
  });

  it("does not claim failure when the ack was merely lost", () => {
    // `unknown` means the command may well have applied — sending the operator to the blotter is correct,
    // implying it failed is not.
    const msg = toggleError(ack("unknown"));
    expect(msg).toContain("blotter");
    expect(msg).not.toContain("refused");
  });

  it("distinguishes a transport failure from an engine refusal", () => {
    const msg = toggleError(ack("pending"), { isError: true, error: new Error("Failed to fetch") });
    expect(msg).toContain("could not reach the engine");
    expect(msg).toContain("Failed to fetch");
  });

  it("prefers the transport failure when both are present", () => {
    const msg = toggleError(ack("rejected", "engine said no"), { isError: true, error: "network down" });
    expect(msg).toContain("network down");
  });

  it("surfaces a failure from EITHER mutation behind the toggle", () => {
    // A toggle is backed by two mutations (attach and cancel). The first version picked one with
    // `attach.isError ? attach : cancel`, so a failure in the unpicked one could go unreported.
    const ok = { isError: false, error: null };
    expect(toggleError(ack("pending"), ok, { isError: true, error: "cancel blew up" })).toContain(
      "cancel blew up",
    );
    expect(toggleError(ack("pending"), { isError: true, error: "attach blew up" }, ok)).toContain(
      "attach blew up",
    );
  });

  it("does not let a healthy mutation mask a rejected ack", () => {
    // The regression the High was really about: the engine's refusal must survive being passed alongside
    // mutations that are perfectly fine.
    const ok = { isError: false, error: null };
    expect(toggleError(ack("rejected", "needs a protective stop"), ok, ok)).toBe("needs a protective stop");
  });

  it("reports nothing while pending or once accepted", () => {
    expect(toggleError(ack("pending"))).toBeNull();
    expect(toggleError(ack("accepted"))).toBeNull();
    expect(toggleError(ack("pending"), { isError: false, error: null })).toBeNull();
  });
});

describe("togglePending", () => {
  it("is pending while a mutation is in flight", () => {
    expect(togglePending(null, ack("pending"), { isPending: true })).toBe(true);
  });

  it("is pending while an issued command has not been acked", () => {
    expect(togglePending("cmd-1", ack("pending"), { isPending: false })).toBe(true);
  });

  it("unlocks once the engine has actually answered", () => {
    for (const state of ["accepted", "rejected"] as const) {
      expect(togglePending("cmd-1", ack(state), { isPending: false }), state).toBe(false);
    }
  });

  it("stays LOCKED on a lost ack, because the command may still be in flight", () => {
    // `unknown` is useCommandStatus's own 8s timeout, not an engine verdict. Unlocking there lets a second
    // press arm a manager that the first press already armed — how FIG collected duplicates on 2026-08-11.
    expect(togglePending("cmd-1", ack("unknown"), { isPending: false })).toBe(true);
  });

  it("is NOT pending before any command was issued", () => {
    expect(togglePending(null, ack("pending"), { isPending: false })).toBe(false);
  });

  it("considers every mutation it is given", () => {
    expect(togglePending(null, ack("pending"), { isPending: false }, { isPending: true })).toBe(true);
  });
});

// ==================================================================================================
// A LOST ACK IS NOT A FAILURE WHEN THE DURABLE STATE SAYS IT ARMED (2026-08-21, reported by the operator).
//
// "arming peak remains a problem. clicking it gives a timeout. reopening shows it is there."
//
// Reproduced from the live manager table: peak_watch on MRVL.XNAS went ARMED -> APPLIED at
// 18:07:13.500Z / .529Z — 29ms apart. The arm was never in doubt. What the operator saw was the UI
// giving up on it:
//
//   useCommandStatus(peakCommandId)      <- DEFAULT 8s, where flatten passes CANCEL_THEN_ACT_TIMEOUT_MS (30s)
//   togglePending: "unknown" counts as locked        -> spinner stays
//   toggleError:   "unknown" -> "no answer from the engine — check the blotter before retrying"
//
// and `/managers`, which holds the durable answer, refetches on a 15s interval. So a SUCCESSFUL arm
// renders as an error for up to ~23 seconds before the durable read clears it.
//
// The component already knows the answer — `activePeak` is derived from the manager list on the same
// screen. The toggle simply was not allowed to look at it. These pin that a lost ack must defer to
// durable state rather than outrank it.
// ==================================================================================================
describe("a lost ack against durable manager state", () => {
  const lost: ToggleAck = { state: "unknown", error: null };

  it("says nothing is wrong when the manager is durably armed", () => {
    // The fixture's own property first: this ack alone DOES produce an error, so the assertion below
    // cannot pass by accident on an input that was never going to report anything.
    expect(toggleError(lost, { isError: false, error: null })).not.toBeNull();

    expect(toggleError(lost, { isError: false, error: null }, { durablyOn: true })).toBeNull();
  });

  it("still reports a lost ack when durable state does NOT show it armed", () => {
    // The silencing direction. A lost ack with nothing to corroborate it is exactly the case the
    // original message was written for, and it must survive.
    expect(toggleError(lost, { isError: false, error: null }, { durablyOn: false })).toMatch(/blotter/);
  });

  it("keeps a REJECTED ack an error even when durable state shows something armed", () => {
    // A refusal is information (#256/#257 — PYRAMID's rejections were invisible for its whole life).
    // Durable state must not be able to swallow one: a stale row from a prior cycle would otherwise
    // erase a fresh refusal.
    const refused: ToggleAck = { state: "rejected", error: "needs a protective stop" };
    expect(toggleError(refused, { isError: false, error: null }, { durablyOn: true }))
      .toBe("needs a protective stop");
  });

  it("unlocks the toggle once durable state confirms the arm", () => {
    // The spinner half. `unknown` counts as locked so an operator cannot double-arm on a lost ack —
    // but once the manager list confirms it, holding the lock is just a stuck control.
    expect(togglePending("cmd-1", lost, { isPending: false })).toBe(true);
    expect(togglePending("cmd-1", lost, { isPending: false }, { durablyOn: true })).toBe(false);
  });

  it("keeps the toggle locked while the ack is genuinely still pending", () => {
    // Durable state must not unlock a command that has not resolved yet — that would let a second
    // press land while the first is in flight.
    const pending: ToggleAck = { state: "pending", error: null };
    expect(togglePending("cmd-1", pending, { isPending: false }, { durablyOn: true })).toBe(true);
  });
});
