import type { RunEvent } from "../types";

/** Text paints at most every 40 ms; every semantic event flushes preceding text. */
export class RunEventBatch {
  private pending: RunEvent[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;
  constructor(private publish: (events: RunEvent[]) => void, private delay = 40) {}

  push(event: RunEvent) {
    if (event.kind !== "assistant_stream") {
      this.flush(); this.publish([event]);
      return;
    }
    this.pending.push(event);
    if (this.pending.length >= 256) this.flush();
    else if (this.timer === undefined) this.timer = setTimeout(() => this.flush(), this.delay);
  }

  flush() {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    const events = this.pending;
    this.pending = [];
    if (events.length) this.publish(events);
  }

  cancel() {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined; this.pending = [];
  }
}
