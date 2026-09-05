#!/usr/bin/env python3
"""Free-tier arithmetic for the hosted canvas relay.

Every number in canvas/README.md comes from this script, so a limit change is
one edit here and one re-run there. The plan limits are copied from the two
pricing pages named below, with the date they were read; the usage model is a
handful of stated assumptions, not a measurement. Stdlib only.
"""

from __future__ import annotations

import argparse

READ_ON = "2026-09-04"
SOURCES = (
    "https://developers.cloudflare.com/durable-objects/platform/pricing/",
    "https://developers.cloudflare.com/workers/platform/pricing/",
)

# Free plan, per day unless noted, as read on READ_ON.
LIMITS = {
    "worker_requests": 100_000,          # Workers Free: 100,000 requests / day
    "do_requests": 100_000,              # Durable Objects Free: 100,000 requests / day
    "do_duration_gbs": 13_000,           # Durable Objects Free: 13,000 GB-s / day
    "rows_written": 100_000,             # SQLite Free: 100,000 rows written / day
    "rows_read": 5_000_000,              # SQLite Free: 5 million rows read / day
}
STORAGE_GB = 5                           # SQLite Free: 5 GB total, not a daily figure

# Usage model. Each assumption is named so it can be argued with.
INBOX_PULLS_PER_HOUR = 12                # a sync roughly every five minutes
VIEWER_REQUESTS_PER_HOUR = 2             # a ticket and a stream open; the page itself is a free asset
VIEWER_PINGS_PER_HOUR = 120              # one keepalive every 30 s
WS_MESSAGE_RATIO = 20                    # 20 incoming WebSocket messages bill as one request
ROWS_WRITTEN_PER_BATCH = 4               # log row, rseq counter, last_seen + bucket, an index entry
ROWS_WRITTEN_PER_PULL = 1                # last_seen
ROWS_READ_PER_BATCH = 12                 # meta, the agent, the digest check, byte-bound sums
ROWS_READ_PER_VIEWER_CONNECT = 200       # the snapshot and the tails
SECONDS_AWAKE_PER_REQUEST = 0.02         # wall time in the object per request, 128 MB
OBJECT_GB = 0.125


def usage(agents: int, hours: float, batches_per_minute: float, viewers: int) -> dict:
    batches = agents * hours * 60 * batches_per_minute
    pulls = agents * hours * INBOX_PULLS_PER_HOUR
    viewer_http = viewers * hours * VIEWER_REQUESTS_PER_HOUR
    pings = viewers * hours * VIEWER_PINGS_PER_HOUR
    api = batches + pulls + viewer_http
    # Keepalives are answered by the runtime without waking the object, but
    # they are counted here at the published ratio so the estimate errs high.
    do_requests = api + pings / WS_MESSAGE_RATIO
    return {
        "worker_requests": api,
        "do_requests": do_requests,
        "do_duration_gbs": do_requests * SECONDS_AWAKE_PER_REQUEST * OBJECT_GB,
        "rows_written": batches * ROWS_WRITTEN_PER_BATCH + pulls * ROWS_WRITTEN_PER_PULL + viewer_http,
        "rows_read": batches * ROWS_READ_PER_BATCH + pulls * 3 + viewers * hours * ROWS_READ_PER_VIEWER_CONNECT,
    }


def share_of_free_day(u: dict) -> float:
    return max(u[k] / LIMITS[k] for k in LIMITS)


def fmt(n: float) -> str:
    return f"{n:,.0f}"


def print_limits() -> None:
    print(f"Cloudflare free plan limits, read {READ_ON} from:")
    for s in SOURCES:
        print(f"  {s}")
    print()
    print("| limit | free plan, per day |")
    print("|---|---|")
    print(f"| Worker requests | {fmt(LIMITS['worker_requests'])} |")
    print(f"| Durable Object requests | {fmt(LIMITS['do_requests'])} (20 incoming WebSocket messages = 1) |")
    print(f"| Durable Object duration | {fmt(LIMITS['do_duration_gbs'])} GB-s (hibernating objects bill nothing) |")
    print(f"| SQLite rows written | {fmt(LIMITS['rows_written'])} |")
    print(f"| SQLite rows read | {fmt(LIMITS['rows_read'])} |")
    print(f"| SQLite storage | {STORAGE_GB} GB total |")
    print("| static assets | free, unlimited |")
    print()


def print_row(agents, hours, bpm, viewers) -> None:
    u = usage(agents, hours, bpm, viewers)
    share = share_of_free_day(u)
    binding = max(LIMITS, key=lambda k: u[k] / LIMITS[k])
    print(f"| {agents} | {hours:g} | {bpm:g} | {viewers} | {fmt(u['worker_requests'])} | {fmt(u['do_requests'])} | "
          f"{u['do_duration_gbs']:,.1f} | {fmt(u['rows_written'])} | {share * 100:,.0f}% ({binding}) |")


def print_table(rows) -> None:
    print("| agents | hours | batches/min/agent | viewers | Worker req | DO req | DO GB-s | rows written | share of a free day |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print_row(*r)
    print()
    print("Assumptions: an inbox pull every 5 min per agent; a viewer opens one stream and one ticket per hour and pings")
    print(f"every 30 s (counted at the {WS_MESSAGE_RATIO}:1 ratio although the runtime answers them without waking the room);")
    print(f"{ROWS_WRITTEN_PER_BATCH} rows written per batch; {SECONDS_AWAKE_PER_REQUEST * 1000:g} ms awake per request at 128 MB.")
    print("The share is the tightest limit; 100% means one such day exhausts the free plan.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agents", type=int, default=None)
    ap.add_argument("--hours", type=float, default=8)
    ap.add_argument("--batches-per-minute", type=float, default=3, help="per agent; a busy session flushes every 20 s")
    ap.add_argument("--viewers", type=int, default=3)
    args = ap.parse_args()
    print_limits()
    if args.agents is not None:
        print_table([(args.agents, args.hours, args.batches_per_minute, args.viewers)])
    else:
        print_table([
            (1, 8, 1, 1),
            (3, 8, 3, 3),
            (5, 8, 3, 5),
            (5, 8, 18, 5),
            (12, 8, 3, 25),
            (12, 24, 3, 25),
        ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
