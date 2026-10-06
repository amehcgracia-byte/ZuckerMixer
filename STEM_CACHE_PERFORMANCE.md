# Incremental stem scanning

Detection scans up to four stems concurrently, bounded by CPU count, with one
10-second audio block per worker. Individual envelopes are atomically written;
the combined detection cache is published only after all scans succeed.
Source audio and rendering DSP are unchanged.

Each stem key includes resolved path, size, nanosecond modification time,
sample rate, channels, physical/timeline frames, offset, algorithm version,
and session window. The window preserves the previous detection time grid.
Adding a stem within the same window scans only that stem; changing session
bounds or alignment rebuilds affected entries. Existing installations populate
the new cache on their first scan.

Synthetic local benchmark: four mono float WAVs, 60 seconds each at 44.1 kHz.
Serial scanning: 0.685 s; parallel scanning plus cache writes: 0.406 s;
cache-only rebuild: 0.023 s. Envelopes were exactly equal. This small benchmark
with warm filesystem data does not predict whole-project or USB performance.
External mechanical drives can slow down with competing reads.

Concurrent renders remain disabled: workers share mutable pipeline
configuration/progress and multiply memory and native numerical threads.
Safe parallel rendering needs process isolation, a memory budget and measured
throughput. Full local audio mirroring remains disabled: it duplicates large
sessions and needs capacity checks and eviction. Current rendering blocks are
30 seconds, not five minutes.
