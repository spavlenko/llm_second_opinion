import { appendFileSync } from "node:fs";
import type { Event } from "./contracts.js";

// Omit applied to each member of a union separately, so `type` still narrows.
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

/** An event as callers supply it; the writer stamps the rest. */
export type EventInput = DistributiveOmit<Event, "schema_version" | "seq" | "ts">;

/** Appends events to events.jsonl, stamping schema_version, seq and ts. */
export class EventWriter {
  private seq = 0;

  constructor(
    private readonly path: string,
    private readonly now: () => number = () => Date.now() / 1000,
  ) {}

  emit(input: EventInput): Event {
    const event = { schema_version: "1", seq: this.seq++, ts: this.now(), ...input } as Event;
    appendFileSync(this.path, JSON.stringify(event) + "\n");
    return event;
  }
}
