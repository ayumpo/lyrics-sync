# Changelog

## 1.1.1 — 2026-10-31

- **Fix:** lrcmux sometimes marks vocal songs as "instrumental" when its sources have no lyrics.
  Those tracks were skipped for months. lrcmux's flag is now treated as an ordinary miss
  (re-checked on the normal schedule). LRCLIB's instrumental flag, which is set by people, is still
  honoured.
- Tracks already parked by this bug are re-checked automatically on the next pass after upgrading.
- `misses.csv` now labels tracks `plain_only` only when plain lyrics were actually found.

## 1.1.0 — 2026-10-31

- First public release: embedded, LRCLIB and lrcmux sources; duration-checked matching; plain `.lrc`
  upgrades with backups; remembered misses; circuit breaker for overloaded sources; Docker image for
  amd64/arm64.
