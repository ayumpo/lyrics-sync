import logging

import lyrics_sync as ls


# ------------------------------------------------------------------ pure helpers

def test_is_synced_basic_and_duration_guard():
    synced = "[00:01.00]a\n[00:05.50]b\n[00:09.00]c"
    assert ls.is_synced(synced)
    assert ls.is_synced(synced, duration=10)
    assert not ls.is_synced("[00:01.00]a\n[00:02.00]b")  # too few timed lines
    assert not ls.is_synced("just\nplain\ntext\nlyrics")
    assert not ls.is_synced("[00:01.00]a\n[00:05.00]b\n[03:00.00]c", duration=60)  # runs past the track


def test_timed_lines_formats():
    assert ls.timed_lines("[01:02.5]x\n[00:03:25]y\n[00:04]z") == [62.5, 3.25, 4.0]


def test_sylt_to_lrc():
    class Frame:
        format = 2
        text = [("first", 1000), ("second", 61530)]

    assert ls.sylt_to_lrc(Frame()) == "[00:01.00]first\n[01:01.53]second"


def test_title_and_artist_variants():
    assert ls.title_variants("The Blessing (feat. Kari Jobe)")[-1] == "The Blessing"
    assert ls.title_variants("Song - 2011 Remastered")[-1] == "Song"
    assert ls.title_variants("Plain") == ["Plain"]
    assert ls.artist_variants("Elevation Worship, Kari Jobe") == ["Elevation Worship, Kari Jobe", "Elevation Worship"]
    assert ls.artist_variants("Majo y Dan") == ["Majo y Dan"]  # "y" is not a separator


def test_similarity_ignores_accents_and_brackets():
    assert ls.similarity("Renuévame", "Renuevame (En Vivo)") == 1.0
    assert ls.similarity("Hosanna", "Graves Into Gardens") < 0.75


# ------------------------------------------------------------------ full runs against the mock API

def test_full_run_covers_every_path(library, mock_api, run, tmp_path):
    music, f = library
    api = mock_api("normal")
    stats, _ = run(api)

    assert stats.get("from_embedded") == 2      # SYLT frame + LRC text in a FLAC tag
    assert stats.get("from_lrclib") == 2        # exact match + search (upgrading a plain .lrc)
    assert stats.get("from_lrcmux") == 1        # fallback when LRCLIB only has a wrong-length version
    assert stats.get("instrumental") == 1
    assert stats.get("plain_only") == 1

    assert f["gig"].with_suffix(".lrc").read_text(encoding="utf-8").startswith("[00:01.00]")
    assert f["sana"].with_suffix(".lrc").read_text(encoding="utf-8").splitlines()[0] == "[00:01.00]Si mi pueblo"
    assert "Linea cuatro" in f["cambio"].with_suffix(".lrc").read_text(encoding="utf-8")
    # The Blessing must come from lrcmux *search* (25 s track), not the 300 s versions
    assert f["blessing"].with_suffix(".lrc").read_text(encoding="utf-8").strip().splitlines()[-1] == "[00:17.00]line 5"
    assert not f["interludio"].with_suffix(".lrc").exists()
    # plain .lrc was upgraded, with the original backed up
    assert ls.is_synced(f["renuevame"].with_suffix(".lrc").read_text(encoding="utf-8"))
    backup = tmp_path / "config/backup" / f["renuevame"].with_suffix(".lrc").relative_to(music)
    assert backup.read_text(encoding="utf-8").startswith("Renuévame Señor")
    # dot-folders and EXCLUDE folders are never touched
    assert not any(p for p in (music / ".trash").iterdir() if p.suffix == ".lrc")
    assert not (music / "@eaDir/thumb.lrc").exists()
    # misses report
    misses = (tmp_path / "config/misses.csv").read_text(encoding="utf-8")
    assert "Cancion Sin Tags" in misses and "plain_only" in misses
    # every request identifies the project
    assert all(ua.startswith(f"lyrics-sync/{ls.VERSION}") for _, _, ua in api.requests)


def test_second_run_makes_no_requests(library, mock_api, run):
    api = mock_api("normal")
    run(api)
    before = len(api.requests)
    stats, syncer = run(api)
    lookups = [r for r in api.requests[before:] if r[1].get("track_name") != ls.PREFLIGHT_TRACK[1]]
    assert lookups == []
    assert stats.get("already_synced") == 5
    assert stats.get("skipped_recent_miss") == 2


def test_dry_run_writes_nothing(library, mock_api, run, tmp_path):
    music, f = library
    stats, _ = run(mock_api("normal"), "--dry-run")
    assert stats.get("from_lrclib") == 2
    lrcs = sorted(p.name for p in music.rglob("*.lrc"))
    assert lrcs == ["02 - Renuévame.lrc"]  # only the pre-existing plain file
    assert not ls.is_synced(f["renuevame"].with_suffix(".lrc").read_text(encoding="utf-8"))
    # ...and leaves no state behind, so a real run afterwards does everything
    rows = __import__("sqlite3").connect(tmp_path / "config/lyrics-sync.db").execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert rows == 0
    assert not (tmp_path / "config/misses.csv").exists()
    stats, _ = run(mock_api("normal"))
    assert stats.get("from_lrclib") == 2 and stats.get("from_embedded") == 2


def test_lrclib_down_trips_breaker_and_lrcmux_continues(library, mock_api, run, caplog):
    caplog.set_level(logging.WARNING, logger="lyrics-sync")
    stats, syncer = run(mock_api("lrclib_down"))
    lrclib = next(p for p in syncer.providers if p.name == "lrclib")
    assert "lrclib is down/overloaded" in caplog.text
    assert not lrclib.available
    assert stats.get("from_lrcmux", 0) >= 2      # The Blessing + Graves Into Gardens via lrcmux search
    assert stats.get("from_embedded") == 2
    assert stats.get("deferred_provider_down", 0) >= 1  # not recorded as a miss: retried next pass


def test_blocked_provider_is_reported_not_silent(library, mock_api, run, caplog):
    caplog.set_level(logging.WARNING, logger="lyrics-sync")
    stats, syncer = run(mock_api("mux_blocked"))
    assert "HTTP 403" in caplog.text
    assert "lrcmux is down/overloaded" in caplog.text
    assert stats.get("from_lrclib") == 2


def test_write_plain_txt_option(library, mock_api, run):
    music, f = library
    run(mock_api("normal"), "--write-plain-txt")
    assert f["untagged"].with_suffix(".txt").read_text(encoding="utf-8").strip() == "solo texto"


def test_no_upgrade_plain_keeps_existing_file(library, mock_api, run):
    music, f = library
    stats, _ = run(mock_api("normal"), "--no-upgrade-plain")
    assert stats.get("plain_kept") == 1
    assert not ls.is_synced(f["renuevame"].with_suffix(".lrc").read_text(encoding="utf-8"))


def test_path_and_limit_flags(library, mock_api, run):
    music, f = library
    stats, _ = run(mock_api("normal"), "--path", "Funky")
    assert stats == {"from_embedded": 1}
    assert not f["gig"].with_suffix(".lrc").exists()
