#!/usr/bin/env python3
"""
Spotify to Google Sheets Sync Tool (Tracks, Albums, Artists)

Practical single-file script for:
- Authenticating to Spotify with OAuth
- Pulling saved tracks, saved albums, and followed artists
- Syncing into Google Sheets (raw + derived tabs)
- Supporting initial load, incremental update, dry run, and genre/mood enrichment

Modes:
    --initial-load      First run, create sheet + all tabs, write full data
    --update            Incremental sync (default when no mode is given)
    --dry-run           Preview changes only (no writes except optional log)
    --genre-enrich      Enable web genre enrichment fallback
    --rebuild-derived   Rebuild browse/helper tabs from raw only

Beginner friendly: read the short setup checklist at the bottom of this file.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

import requests
import pandas as pd
from dotenv import load_dotenv

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException

import gspread
from google.oauth2.service_account import Credentials


# -----------------------------
# Constants & Defaults
# -----------------------------

SCOPES_SPOTIFY = [
    "user-library-read",
    "user-follow-read",
    "user-read-recently-played",
]

DEFAULT_TOKEN_CACHE = ".spotify_token_cache.json"
DEFAULT_GENRE_CACHE = ".genre_cache.json"
DEFAULT_SYNC_STATE = ".sync_state.json"

DEFAULT_CONFIG = {
    "sheet_name": "Spotify Personal Library",
   
"sheet_id": "1BQVKH85sVqk-jezdbyvz6wkzc87uz6OAdl4N5FiXAxI",
    "default_run_mode": "update",
    "genre_enrichment_enabled": False,
    "genre_confidence_threshold": 0.6,
    "mood_confidence_threshold": 0.6,
    "missing_runs_threshold": 2,
    "token_cache_file": DEFAULT_TOKEN_CACHE,
    "genre_cache_file": DEFAULT_GENRE_CACHE,
    "sync_state_file": DEFAULT_SYNC_STATE,
    "tab_names": {
        "tracks_raw": "Tracks",
        "albums_raw": "Albums",
        "artists_raw": "Artists",
        "browse_tracks": "Browse_Tracks",
        "browse_albums": "Browse_Albums",
        "browse_artists": "Browse_Artists",
        "by_genre": "By_Genre",
        "by_genre_artists": "By_Genre_Artists",
        "by_genre_albums": "By_Genre_Albums",
        "artist_saved_albums": "Artist_Saved_Albums",
        "recently_played": "Recently_Played",
        "needs_review": "Needs_Review",
        "sync_log": "Sync_Log",
        "config": "Config",
        "manual_overrides": "Manual_Overrides",
    },
}


# -----------------------------
# Utility helpers
# -----------------------------


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts} UTC] {msg}")


def safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return cur if cur is not None else default


def ensure_columns(df, columns):
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df


def read_json_file(path, default_value):
    p = Path(path)
    if not p.exists():
        return default_value
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_value


def write_json_file(path, data):
    p = Path(path)
    try:
        with p.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"Warning: failed to write JSON file {path}: {e}")


def chunked(iterable, size):
    chunk = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


# -----------------------------
# Checkpoint helpers
# -----------------------------

# Checkpoints expire after this many seconds.
# - Recently played: always fresh (no checkpoint)
# - Saved tracks/albums/artists: refresh daily so new saves are picked up
CHECKPOINT_TTL = {
    "saved_tracks":     86400,   # 24 hours
    "saved_albums":     86400,   # 24 hours
    "followed_artists": 86400,   # 24 hours
    "artist_details_map": 604800, # 7 days (rarely changes)
}
CHECKPOINT_TTL_DEFAULT = 86400   # 24 hours for anything not listed


def save_checkpoint(path, data):
    wrapped = {
        "saved_at": time.time(),
        "data": data,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(wrapped, f, ensure_ascii=False)


def load_checkpoint(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            wrapped = json.load(f)
        # Support both old format (raw list/dict) and new format (wrapped with saved_at)
        if not isinstance(wrapped, dict) or "saved_at" not in wrapped:
            # Old format — treat as expired so we re-fetch fresh
            log(f"Checkpoint {path} is old format, will re-fetch.")
            return default
        age = time.time() - wrapped["saved_at"]
        # Determine TTL from filename
        name = os.path.basename(path).replace(".json", "")
        ttl = CHECKPOINT_TTL.get(name, CHECKPOINT_TTL_DEFAULT)
        if age > ttl:
            log(f"Checkpoint {path} expired ({int(age/3600)}h old, TTL {int(ttl/3600)}h), will re-fetch.")
            return default
        return wrapped["data"]
    except Exception:
        return default


# -----------------------------
# Env & CLI
# -----------------------------


def load_env_and_args():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Sync personal Spotify library (tracks, albums, artists) into Google Sheets."
    )
    parser.add_argument("--initial-load", action="store_true", help="Run initial load")
    parser.add_argument("--update", action="store_true", help="Run incremental update (default)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--genre-enrich", action="store_true", help="Enable web genre enrichment")
    parser.add_argument("--rebuild-derived", action="store_true", help="Rebuild derived tabs only")
    parser.add_argument("--config-sheet-name", type=str, help="Override target sheet name at runtime")
    parser.add_argument("--config-sheet-id", type=str, help="Override existing sheet ID at runtime")

    args = parser.parse_args()
    return args


# -----------------------------
# Spotify Auth & Fetch
# -----------------------------


def authenticate_spotify(config):
    client_id = os.getenv("SPOTIPY_CLIENT_ID")
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
    redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI", "http://localhost:8888/callback")

    if not client_id or not client_secret:
        log("ERROR: Missing SPOTIPY_CLIENT_ID or SPOTIPY_CLIENT_SECRET in .env")
        sys.exit(1)

    cache_path = config.get("token_cache_file", DEFAULT_TOKEN_CACHE)

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=" ".join(SCOPES_SPOTIFY),
        cache_path=cache_path,
        open_browser=True,
        show_dialog=False,
    )

    try:
        spotify = spotipy.Spotify(auth_manager=auth_manager)
        _ = spotify.current_user()  # sanity check
        log("Spotify authentication successful.")
        return spotify
    except Exception as e:
        log(f"ERROR: Spotify authentication failed: {e}")
        sys.exit(1)


def fetch_saved_tracks(sp, limit=50):
    ckpt_path = "checkpoints/saved_tracks.json"
    cached = load_checkpoint(ckpt_path)
    if cached is not None:
        log(f"Loaded saved tracks from checkpoint ({len(cached)} items).")
        return cached

    log("Fetching saved tracks from Spotify...")
    items = []
    offset = 0
    while True:
        try:
            results = sp.current_user_saved_tracks(limit=limit, offset=offset)
        except Exception as e:
            log(f"Error fetching saved tracks at offset {offset}: {e}")
            break
        batch = results.get("items", [])
        if not batch:
            break
        items.extend(batch)
        offset += len(batch)
        if results.get("next") is None:
            break
        time.sleep(0.1)
    log(f"Fetched {len(items)} saved tracks.")
    save_checkpoint(ckpt_path, items)
    return items


def fetch_saved_albums(sp, limit=50):
    ckpt_path = "checkpoints/saved_albums.json"
    cached = load_checkpoint(ckpt_path)
    if cached is not None:
        log(f"Loaded saved albums from checkpoint ({len(cached)} items).")
        return cached

    log("Fetching saved albums from Spotify...")
    items = []
    offset = 0
    while True:
        try:
            results = sp.current_user_saved_albums(limit=limit, offset=offset)
        except Exception as e:
            log(f"Error fetching saved albums at offset {offset}: {e}")
            break
        batch = results.get("items", [])
        if not batch:
            break
        items.extend(batch)
        offset += len(batch)
        if results.get("next") is None:
            break
        time.sleep(0.1)
    log(f"Fetched {len(items)} saved albums.")
    save_checkpoint(ckpt_path, items)
    return items


def fetch_followed_artists(sp, limit=50):
    ckpt_path = "checkpoints/followed_artists.json"
    cached = load_checkpoint(ckpt_path)
    if cached is not None:
        log(f"Loaded followed artists from checkpoint ({len(cached)} items).")
        return cached

    log("Fetching followed artists from Spotify...")
    items = []
    after = None
    while True:
        try:
            results = sp.current_user_followed_artists(limit=limit, after=after)
        except Exception as e:
            log(f"Error fetching followed artists: {e}")
            break
        artists_obj = results.get("artists", {})
        batch = artists_obj.get("items", [])
        if not batch:
            break
        items.extend(batch)
        cursors = artists_obj.get("cursors") or {}
        after = cursors.get("after")
        if not after:
            break
        time.sleep(0.1)
    log(f"Fetched {len(items)} followed artists.")
    save_checkpoint(ckpt_path, items)
    return items


def fetch_recently_played(sp, limit=50):
    """Fetch recently played tracks — always fresh, no checkpoint (changes every listen)."""
    log("Fetching recently played tracks from Spotify...")
    items = []
    try:
        results = sp.current_user_recently_played(limit=limit)
        items = results.get("items", [])
        log(f"Fetched {len(items)} recently played tracks.")
    except Exception as e:
        log(f"Error fetching recently played: {e}")
    return items


def build_recently_played_dataframe(recently_played, artist_details_map):
    rows = []
    seen = set()
    for item in recently_played:
        played_at = item.get("played_at", "")
        track = item.get("track") or {}
        track_id = track.get("id")
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        track_name = track.get("name", "")
        track_url = safe_get(track, "external_urls", "spotify", default="")
        album = track.get("album") or {}
        album_name = album.get("name", "")
        album_url = safe_get(album, "external_urls", "spotify", default="")
        images = album.get("images", []) or []
        image_url = images[0].get("url", "") if images else ""
        artists = track.get("artists", []) or []
        primary_artist = artists[0] if artists else {}
        primary_artist_name = primary_artist.get("name", "")
        primary_artist_id = primary_artist.get("id", "")
        artist_obj = artist_details_map.get(primary_artist_id, {})
        artist_url = safe_get(artist_obj, "external_urls", "spotify", default="")
        # Construct URL from ID if not available from artist_details_map
        if not artist_url and primary_artist_id:
            artist_url = f"https://open.spotify.com/artist/{primary_artist_id}"
        rows.append({
            "played_at": played_at,
            "track_name": track_name,
            "track_url": track_url,
            "primary_artist_name": primary_artist_name,
            "primary_artist_id": primary_artist_id,
            "artist_url": artist_url,
            "album_name": album_name,
            "album_url": album_url,
            "image_url": image_url,
        })
    return pd.DataFrame(rows)


def write_recently_played_tab(sh, config, recently_played_df):
    tab_name = config["tab_names"]["recently_played"]
    ws = get_or_create_worksheet(sh, tab_name, rows=200, cols=20)
    clear_and_set_dataframe(ws, recently_played_df)


def fetch_artist_details(sp, artist_ids):
    ckpt_path = "checkpoints/artist_details_map.json"
    cached = load_checkpoint(ckpt_path)
    if cached is not None:
        log(f"Loaded artist details from checkpoint ({len(cached)} entries).")
        return cached

    details = {}
    artist_ids = list({a for a in artist_ids if a})

    for chunk in chunked(artist_ids, 50):
        try:
            results = sp.artists(chunk)
            for a in results.get("artists", []):
                if a and a.get("id"):
                    details[a["id"]] = a
            time.sleep(0.25)
        except SpotifyException as e:
            if e.http_status == 429:
                retry_after = None
                if getattr(e, "headers", None):
                    retry_after = e.headers.get("Retry-After") or e.headers.get("retry-after")
                log(f"Spotify rate limit hit while fetching artist details. Retry-After={retry_after}")
                break
            else:
                log(f"Spotify error fetching artist details chunk: {e}")
        except Exception as e:
            log(f"Error fetching artist details chunk: {e}")

    save_checkpoint(ckpt_path, details)
    return details


# -----------------------------
# Web Genre Enrichment
# -----------------------------


# ---------------------------------------------------------------------------
# FINAL 21-BUCKET GENRE CONSOLIDATION SYSTEM
#
# Buckets (display order):
#   Rock, Blues, Acoustic Blues, Folk, Country, Americana, Acoustic,
#   Hip Hop, Metal, Jazz, R&B / Soul, Neo Soul, Funk, Electronic, Pop,
#   Reggae, Dance, Experimental, Latin, Classical, World
#
# Design rules:
#   - Every key maps to exactly one canonical bucket label.
#   - More-specific buckets (Acoustic Blues, Neo Soul, Funk) are listed
#     BEFORE broader ones (Blues, R&B / Soul) so the first match wins
#     when we iterate in order.
#   - consolidate_genres() adds bucket labels alongside existing tags;
#     it does NOT strip the original micro-genre tags so raw data is preserved.
# ---------------------------------------------------------------------------

# Ordered list of (micro_genre_lowercase, canonical_bucket) pairs.
# ORDER MATTERS — more specific entries must come before general ones.
GENRE_BUCKET_MAP = [
    # ── Acoustic Blues (before Blues) ──────────────────────────────────────
    ("acoustic blues", "Acoustic Blues"),
    ("country blues", "Acoustic Blues"),
    ("delta blues", "Acoustic Blues"),
    ("piedmont blues", "Acoustic Blues"),
    ("folk blues", "Acoustic Blues"),
    ("primitive blues", "Acoustic Blues"),
    ("pre-war blues", "Acoustic Blues"),
    ("hill country blues", "Acoustic Blues"),

    # ── Blues ──────────────────────────────────────────────────────────────
    ("blues", "Blues"),
    ("electric blues", "Blues"),
    ("chicago blues", "Blues"),
    ("texas blues", "Blues"),
    ("blues rock", "Blues"),
    ("west coast blues", "Blues"),
    ("swamp blues", "Blues"),
    ("boogie woogie", "Blues"),
    ("rhythm and blues", "Blues"),
    ("jump blues", "Blues"),
    ("soul blues", "Blues"),

    # ── Neo Soul (before R&B / Soul) ───────────────────────────────────────
    ("neo soul", "Neo Soul"),
    ("neo-soul", "Neo Soul"),

    # ── Funk (before R&B / Soul) ───────────────────────────────────────────
    ("funk", "Funk"),
    ("p-funk", "Funk"),
    ("g-funk", "Funk"),
    ("funk rock", "Funk"),
    ("funk metal", "Funk"),
    ("deep funk", "Funk"),
    ("go-go", "Funk"),
    ("electro funk", "Funk"),

    # ── R&B / Soul ─────────────────────────────────────────────────────────
    ("r&b", "R&B / Soul"),
    ("rnb", "R&B / Soul"),
    ("soul", "R&B / Soul"),
    ("contemporary r&b", "R&B / Soul"),
    ("quiet storm", "R&B / Soul"),
    ("new jack swing", "R&B / Soul"),
    ("motown", "R&B / Soul"),
    ("northern soul", "R&B / Soul"),
    ("gospel", "R&B / Soul"),
    ("doo-wop", "R&B / Soul"),
    ("soul music", "R&B / Soul"),

    # ── Classic Rock (before Rock) ─────────────────────────────────────────
    ("classic rock", "Classic Rock"),
    ("album rock", "Classic Rock"),
    ("arena rock", "Classic Rock"),
    ("heartland rock", "Classic Rock"),
    ("mellow gold", "Classic Rock"),
    ("soft rock", "Classic Rock"),
    ("glam rock", "Classic Rock"),
    ("70s rock", "Classic Rock"),
    ("70s", "Classic Rock"),
    ("1970s", "Classic Rock"),
    ("rock de los 70", "Classic Rock"),
    ("early rock and roll", "Classic Rock"),
    ("rockabilly", "Classic Rock"),

    # ── Americana (before Folk and Country) ────────────────────────────────
    ("americana", "Americana"),
    ("roots rock", "Americana"),
    ("alt-country", "Americana"),
    ("alternative country", "Americana"),
    ("outlaw country", "Americana"),
    ("red dirt", "Americana"),
    ("texas country", "Americana"),
    ("southern rock", "Americana"),
    ("swamp rock", "Americana"),
    ("neofolk", "Americana"),

    # ── Country (before Folk) ──────────────────────────────────────────────
    ("country", "Country"),
    ("country pop", "Country"),
    ("country rock", "Country"),
    ("bluegrass", "Country"),
    ("honky tonk", "Country"),
    ("nashville sound", "Country"),
    ("bro-country", "Country"),
    ("new country", "Country"),
    ("progressive country", "Country"),

    # ── Acoustic (before Folk) ─────────────────────────────────────────────
    ("singer-songwriter", "Acoustic"),
    ("acoustic", "Acoustic"),
    ("folk pop", "Acoustic"),
    ("chamber folk", "Acoustic"),
    ("indie folk", "Acoustic"),  # also Folk but Acoustic wins
    ("anti-folk", "Acoustic"),
    ("lo-fi folk", "Acoustic"),

    # ── Folk ───────────────────────────────────────────────────────────────
    ("folk", "Folk"),
    ("folk rock", "Folk"),
    ("traditional folk", "Folk"),
    ("british folk", "Folk"),
    ("celtic", "Folk"),
    ("irish folk", "Folk"),
    ("appalachian", "Folk"),
    ("old time", "Folk"),
    ("new weird america", "Folk"),

    # ── Rock ───────────────────────────────────────────────────────────────
    ("rock", "Rock"),
    ("indie rock", "Rock"),
    ("alternative rock", "Rock"),
    ("post-rock", "Rock"),
    ("hard rock", "Rock"),
    ("progressive rock", "Rock"),
    ("psychedelic rock", "Rock"),
    ("garage rock", "Rock"),
    ("art rock", "Rock"),
    ("punk rock", "Rock"),
    ("punk", "Rock"),
    ("math rock", "Rock"),
    ("noise rock", "Rock"),
    ("surf rock", "Rock"),
    ("britpop", "Rock"),
    ("grunge", "Rock"),
    ("shoegaze", "Rock"),
    ("post-grunge", "Rock"),
    ("emo", "Rock"),
    ("screamo", "Rock"),
    ("new wave", "Rock"),
    ("post-punk", "Rock"),
    ("gothic rock", "Rock"),
    ("industrial rock", "Rock"),
    ("power pop", "Rock"),
    ("jangle pop", "Rock"),
    ("college rock", "Rock"),
    ("lo-fi rock", "Rock"),
    ("indie", "Rock"),
    ("alternative", "Rock"),
    ("dream pop", "Rock"),
    ("noise pop", "Rock"),
    ("space rock", "Rock"),
    ("krautrock", "Rock"),
    ("psychedelic", "Rock"),
    ("stoner rock", "Rock"),
    ("heavy psych", "Rock"),
    ("freakbeat", "Rock"),
    ("jam band", "Rock"),
    ("post-hardcore", "Rock"),

    # ── Hip Hop ────────────────────────────────────────────────────────────
    ("hip hop", "Hip Hop"),
    ("hip-hop", "Hip Hop"),
    ("hiphop", "Hip Hop"),
    ("rap", "Hip Hop"),
    ("trap", "Hip Hop"),
    ("conscious hip hop", "Hip Hop"),
    ("east coast hip hop", "Hip Hop"),
    ("west coast hip hop", "Hip Hop"),
    ("southern hip hop", "Hip Hop"),
    ("gangsta rap", "Hip Hop"),
    ("alternative hip hop", "Hip Hop"),
    ("underground hip hop", "Hip Hop"),
    ("boom bap", "Hip Hop"),
    ("cloud rap", "Hip Hop"),
    ("mumble rap", "Hip Hop"),
    ("drill", "Hip Hop"),
    ("crunk", "Hip Hop"),
    ("hyphy", "Hip Hop"),
    ("lo-fi hip hop", "Hip Hop"),
    ("jazz rap", "Hip Hop"),
    ("dirty south", "Hip Hop"),
    ("southern rap", "Hip Hop"),
    ("chopped and screwed", "Hip Hop"),
    ("phonk", "Hip Hop"),

    # ── Metal ──────────────────────────────────────────────────────────────
    ("metal", "Metal"),
    ("heavy metal", "Metal"),
    ("thrash metal", "Metal"),
    ("death metal", "Metal"),
    ("black metal", "Metal"),
    ("doom metal", "Metal"),
    ("power metal", "Metal"),
    ("progressive metal", "Metal"),
    ("nu-metal", "Metal"),
    ("metalcore", "Metal"),
    ("deathcore", "Metal"),
    ("symphonic metal", "Metal"),
    ("folk metal", "Metal"),
    ("viking metal", "Metal"),
    ("sludge metal", "Metal"),
    ("stoner metal", "Metal"),
    ("post-metal", "Metal"),
    ("djent", "Metal"),
    ("speed metal", "Metal"),
    ("glam metal", "Metal"),
    ("grindcore", "Metal"),
    ("industrial metal", "Metal"),
    ("gothic metal", "Metal"),

    # ── Jazz ───────────────────────────────────────────────────────────────
    ("jazz", "Jazz"),
    ("bebop", "Jazz"),
    ("cool jazz", "Jazz"),
    ("hard bop", "Jazz"),
    ("free jazz", "Jazz"),
    ("jazz fusion", "Jazz"),
    ("smooth jazz", "Jazz"),
    ("acid jazz", "Jazz"),
    ("nu jazz", "Jazz"),
    ("swing", "Jazz"),
    ("big band", "Jazz"),
    ("dixieland", "Jazz"),
    ("modal jazz", "Jazz"),
    ("post-bop", "Jazz"),
    ("soul jazz", "Jazz"),
    ("latin jazz", "Jazz"),
    ("bossa nova", "Jazz"),
    ("samba", "Jazz"),
    ("fusion", "Jazz"),

    # ── Electronic ─────────────────────────────────────────────────────────
    ("electronic", "Electronic"),
    ("electronica", "Electronic"),
    ("ambient", "Electronic"),
    ("idm", "Electronic"),
    ("glitch", "Electronic"),
    ("downtempo", "Electronic"),
    ("chillwave", "Electronic"),
    ("vaporwave", "Electronic"),
    ("synthwave", "Electronic"),
    ("retrowave", "Electronic"),
    ("darkwave", "Electronic"),
    ("industrial", "Electronic"),
    ("ebm", "Electronic"),
    ("experimental electronic", "Electronic"),
    ("trip hop", "Electronic"),
    ("lo-fi", "Electronic"),
    ("future bass", "Electronic"),
    ("bass music", "Electronic"),
    ("noise", "Electronic"),
    ("drone", "Electronic"),
    ("musique concrete", "Electronic"),
    ("chillout", "Electronic"),
    ("lounge", "Electronic"),
    ("new age", "Electronic"),

    # ── Pop ────────────────────────────────────────────────────────────────
    ("pop", "Pop"),
    ("indie pop", "Pop"),
    ("synth-pop", "Pop"),
    ("electropop", "Pop"),
    ("art pop", "Pop"),
    ("baroque pop", "Pop"),
    ("chamber pop", "Pop"),
    ("bubblegum pop", "Pop"),
    ("teen pop", "Pop"),
    ("dance-pop", "Pop"),
    ("k-pop", "Pop"),
    ("j-pop", "Pop"),
    ("c-pop", "Pop"),
    ("hyperpop", "Pop"),
    ("sophisti-pop", "Pop"),
    ("adult contemporary", "Pop"),
    ("easy listening", "Pop"),

    # ── Reggae ─────────────────────────────────────────────────────────────
    ("reggae", "Reggae"),
    ("dancehall", "Reggae"),
    ("ska", "Reggae"),
    ("dub", "Reggae"),
    ("roots reggae", "Reggae"),
    ("rocksteady", "Reggae"),
    ("lovers rock", "Reggae"),
    ("acoustic reggae", "Reggae"),
    ("reggae fusion", "Reggae"),

    # ── Dance ──────────────────────────────────────────────────────────────
    ("dance", "Dance"),
    ("house", "Dance"),
    ("deep house", "Dance"),
    ("tech house", "Dance"),
    ("progressive house", "Dance"),
    ("electro house", "Dance"),
    ("future house", "Dance"),
    ("tropical house", "Dance"),
    ("afro house", "Dance"),
    ("techno", "Dance"),
    ("detroit techno", "Dance"),
    ("minimal techno", "Dance"),
    ("acid techno", "Dance"),
    ("industrial techno", "Dance"),
    ("trance", "Dance"),
    ("progressive trance", "Dance"),
    ("psytrance", "Dance"),
    ("drum and bass", "Dance"),
    ("jungle", "Dance"),
    ("breakbeat", "Dance"),
    ("dubstep", "Dance"),
    ("brostep", "Dance"),
    ("uk garage", "Dance"),
    ("2-step", "Dance"),
    ("edm", "Dance"),
    ("club", "Dance"),
    ("disco", "Dance"),
    ("nu-disco", "Dance"),
    ("electro", "Dance"),
    ("big room", "Dance"),
    ("hardstyle", "Dance"),
    ("footwork", "Dance"),
    ("jersey club", "Dance"),
    ("afrobeats", "Dance"),

    # ── Experimental ───────────────────────────────────────────────────────
    ("experimental", "Experimental"),
    ("avant-garde", "Experimental"),
    ("avantgarde", "Experimental"),
    ("free improvisation", "Experimental"),
    ("noise music", "Experimental"),
    ("sound art", "Experimental"),
    ("spoken word", "Experimental"),
    ("art music", "Experimental"),
    ("post-minimalism", "Experimental"),
    ("microtonality", "Experimental"),
    ("harsh noise", "Experimental"),
    ("lowercase", "Experimental"),
    ("spectralism", "Experimental"),

    # ── Latin ──────────────────────────────────────────────────────────────
    ("latin", "Latin"),
    ("salsa", "Latin"),
    ("merengue", "Latin"),
    ("cumbia", "Latin"),
    ("bachata", "Latin"),
    ("reggaeton", "Latin"),
    ("latin pop", "Latin"),
    ("latin rock", "Latin"),
    ("flamenco", "Latin"),
    ("bossa nova", "Latin"),  # also Jazz — Latin listed here as secondary
    ("mpb", "Latin"),
    ("tropicalia", "Latin"),
    ("axe", "Latin"),
    ("forro", "Latin"),
    ("tango", "Latin"),
    ("mariachi", "Latin"),
    ("norteño", "Latin"),
    ("corrido", "Latin"),
    ("banda", "Latin"),

    # ── Classical ──────────────────────────────────────────────────────────
    ("classical", "Classical"),
    ("orchestral", "Classical"),
    ("chamber music", "Classical"),
    ("opera", "Classical"),
    ("baroque", "Classical"),
    ("romantic", "Classical"),
    ("contemporary classical", "Classical"),
    ("neoclassical", "Classical"),
    ("minimalism", "Classical"),
    ("modern classical", "Classical"),
    ("soundtrack", "Classical"),
    ("film score", "Classical"),
    ("choral", "Classical"),
    ("sacred music", "Classical"),
    ("liturgical", "Classical"),
    ("early music", "Classical"),

    # ── World ──────────────────────────────────────────────────────────────
    ("world", "World"),
    ("afrobeat", "World"),
    ("afropop", "World"),
    ("highlife", "World"),
    ("juju", "World"),
    ("mbalax", "World"),
    ("soukous", "World"),
    ("afro", "World"),
    ("indian", "World"),
    ("bollywood", "World"),
    ("bhangra", "World"),
    ("qawwali", "World"),
    ("arabic", "World"),
    ("middle eastern", "World"),
    ("turkish", "World"),
    ("persian", "World"),
    ("global", "World"),
    ("international", "World"),
    ("central asian throat singing", "World"),
    ("throat singing", "World"),
    ("global bass", "World"),
    ("balkan", "World"),
    ("klezmer", "World"),
    ("polka", "World"),
]

# Build a fast lookup dict from the ordered list.
# Earlier entries win for any given key (more specific buckets listed first above).
GENRE_BUCKET_LOOKUP = {}
for _micro, _bucket in GENRE_BUCKET_MAP:
    if _micro not in GENRE_BUCKET_LOOKUP:
        GENRE_BUCKET_LOOKUP[_micro] = _bucket

# Canonical bucket labels — used for display ordering in the app.
CANONICAL_BUCKETS = [
    "Rock", "Classic Rock", "Blues", "Acoustic Blues", "Folk", "Country", "Americana",
    "Acoustic", "Hip Hop", "Metal", "Jazz", "R&B / Soul", "Neo Soul",
    "Funk", "Electronic", "Pop", "Reggae", "Dance", "Experimental",
    "Latin", "Classical", "World",
]


# Tags that are not real genres and should be filtered out
NOISE_TAGS = {
    # Years and decades (60s and earlier, 80s onward still noise)
    "50s", "60s", "80s", "90s", "00s", "10s", "20s",
    "1950s", "1960s", "1980s", "1990s", "2000s", "2010s", "2020s",
    # Nationalities / regions
    "american", "british", "english", "irish", "scottish", "welsh",
    "australian", "canadian", "swedish", "norwegian", "danish", "finnish",
    "german", "french", "italian", "spanish", "japanese", "korean",
    "african", "european", "northern", "southern", "eastern", "western",
    "atlanta", "chicago", "london", "nashville", "new york", "detroit",
    "berlin", "austin", "akron", "australia", "aussie",
    # List/chart/award tags
    "1001 albums you must hear before you die", "albums", "all",
    "2025 albums", "2024 albums", "2023 albums", "2022 albums", "2021 albums",
    "2020 albums", "2019 albums", "2018 albums", "2017 albums", "2016 albums",
    "2015 albums", "2014 albums", "2013 albums", "2012 albums", "2011 albums",
    "2010 albums", "2009 albums", "2008 albums", "2007 albums",
    "aln-sh", "100th", "aoty", "best of 2020", "best of 2021", "best of 2022",
    "best of 2023", "best of 2024", "2023 grammy nominations",
    "1001 albums", "abc favorite cds", "albumsdoudoune",
    # Descriptor tags
    "female vocalists", "male vocalists", "all-female", "all-male",
    "easy listening", "chillout", "chill", "relax", "background",
    "band", "group", "duo", "trio", "quartet", "ensemble",
    "ai", "ai generated", "axe", "christmas", "holiday",
    "seen live", "favorites", "favourite", "fav", "best of", "greatest hits",
    "guitar", "ballad", "alliteration", "fictional", "acquire",
    "artisttoknow", "b artist", "awesome album", "addicting",
    "album i own", "all in a day", "animals for stretchead",
    "beach weekend", "4 estrellas", "5 stars", "3-5",
    "2008 universal fire victim", "elephant 6", "lynch jelusick frontiers",
    "4jsduskmellow", "allboutguitar", "black label society",
    "army of the pharaohs", "beat junkies", "back street crawler",
    "abbey sings abbey", "bb king", "argent", "akron",
    "70th", "indie groove",
    # Bare years
    "2003", "2004", "2005", "2006", "2007", "2008", "2009",
    "2010", "2011", "2012", "2013", "2014", "2015", "2016",
    "2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025",
    "1967", "1968", "1969", "1970", "1971", "1972", "1973", "1974",
    "1975", "1976", "1977", "1978", "1979", "1980", "1981", "1982",
    "1983", "1984", "1985", "1986", "1987", "1988", "1989",
    "1990", "1991", "1992", "1993", "1994", "1995", "1996", "1997", "1998", "1999",
}




def clean_genres(genres_str):
    """
    Remove noise tags (years, nationalities, list names, random tags) from a genres string.
    Returns cleaned comma-separated string with only real genre tags.
    """
    if not genres_str:
        return genres_str
    tags = [g.strip() for g in genres_str.split(",") if g.strip()]
    cleaned = []
    for tag in tags:
        tl = tag.lower()
        # Skip pure numbers
        if tag.isdigit():
            continue
        # Skip year patterns (19xx, 20xx)
        if re.match(r'^(19|20)\d{2}$', tag):
            continue
        # Skip "YYYY albums" pattern
        if re.match(r'^(19|20)\d{2}\s+albums?$', tl):
            continue
        # Skip "best of YYYY" pattern
        if re.match(r'^best of \d{4}$', tl):
            continue
        # Skip in noise set
        if tl in NOISE_TAGS:
            continue
        # Skip very short tags
        if len(tag) <= 2:
            continue
        # Skip tags that look like usernames or codes (contain digits mixed with letters oddly)
        if re.match(r'^[a-z0-9]{1,4}[0-9]{3,}', tl) and len(tag) < 12:
            continue
        cleaned.append(tag)
    return ", ".join(cleaned)


def consolidate_genres(genres_str):
    """
    Takes a comma-separated genres string, cleans noise tags, then returns
    an expanded version with both specific genres AND their parent categories.
    Also applies EXTRA_GENRE_MAP to catch variants not in GENRE_PARENT_MAP.
    e.g. "indie rock, deep house" → "indie rock, rock, deep house, dance"
    """
    if not genres_str:
        return genres_str

    # First clean noise tags
    genres_str = clean_genres(genres_str)
    if not genres_str:
        return genres_str

    original = [g.strip() for g in genres_str.split(",") if g.strip()]
    seen = set(g.lower() for g in original)
    parents_to_add = []

    for g in original:
        # Check GENRE_PARENT_MAP first
        parent = GENRE_BUCKET_MAP.get(g.lower())
    if not parent:
        parent = EXTRA_GENRE_MAP.get(g.lower())
    if parent and parent.lower() not in seen:
            seen.add(parent.lower())
            parents_to_add.append(parent)

    combined = original + parents_to_add
    return ", ".join(combined)


def simple_normalize_genre(name):
    if not name:
        return None
    g = name.lower().strip()
    g = re.sub(r"[^a-z0-9 +/&-]", "", g)
    g = g.replace("&", "and")
    g = re.sub(r"\s+", " ", g)
    return g or None


def lastfm_lookup_genre(artist_name, api_key):
    """Look up artist tags via Last.fm API."""
    if not api_key:
        return [], 0.0, "lastfm_no_key"
    try:
        url = "https://ws.audioscrobbler.com/2.0/"
        params = {
            "method": "artist.getinfo",
            "artist": artist_name,
            "api_key": api_key,
            "format": "json",
            "autocorrect": 1,
        }
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return [], 0.0, f"lastfm_error_{resp.status_code}"
        data = resp.json()
        if "error" in data:
            return [], 0.0, f"lastfm_api_error_{data.get('error')}"
        tags = data.get("artist", {}).get("tags", {}).get("tag", [])
        if not tags:
            return [], 0.0, "lastfm_no_tags"
        # Last.fm returns tags sorted by weight already
        top_tags = [t["name"] for t in tags[:7] if t.get("name")]
        normalized = [simple_normalize_genre(g) for g in top_tags]
        normalized = [g for g in normalized if g]
        # Filter noise tags
        normalized = [g for g in normalized if g.lower() not in NOISE_TAGS and not g.isdigit() and len(g) > 2]
        if not normalized:
            return [], 0.0, "lastfm_empty_after_normalize"
        confidence = 0.85
        return normalized, confidence, "lastfm"
    except Exception as e:
        return [], 0.0, f"lastfm_exception_{str(e)[:40]}"


def musicbrainz_lookup_genre(artist_name):
    """Look up artist tags via MusicBrainz API."""
    try:
        headers = {"User-Agent": "SpotifyToSheetsSync/1.0 (personal-use)"}
        search_url = "https://musicbrainz.org/ws/2/artist/"
        search_params = {
            "query": f'artist:"{artist_name}"',
            "fmt": "json",
            "limit": 3,
        }
        time.sleep(1.1)  # Respect 1 req/sec rate limit
        resp = requests.get(search_url, params=search_params, headers=headers, timeout=15)
        if resp.status_code != 200:
            return [], 0.0, f"mb_search_error_{resp.status_code}"
        data = resp.json()
        artists = data.get("artists", [])
        if not artists:
            return [], 0.0, "mb_no_results"
        best = max(artists, key=lambda a: int(a.get("score", 0)))
        artist_id = best.get("id")
        if not artist_id:
            return [], 0.0, "mb_no_id"
        detail_url = f"https://musicbrainz.org/ws/2/artist/{artist_id}"
        detail_params = {"inc": "tags", "fmt": "json"}
        time.sleep(1.1)
        resp2 = requests.get(detail_url, params=detail_params, headers=headers, timeout=15)
        if resp2.status_code != 200:
            return [], 0.0, f"mb_detail_error_{resp2.status_code}"
        detail = resp2.json()
        tags = detail.get("tags", [])
        if not tags:
            return [], 0.0, "mb_no_tags"
        valid_tags = [t for t in tags if t.get("count", 0) >= 1]
        valid_tags.sort(key=lambda t: t.get("count", 0), reverse=True)
        top_tags = [t["name"] for t in valid_tags[:7] if t.get("name")]
        normalized = [simple_normalize_genre(g) for g in top_tags]
        normalized = [g for g in normalized if g]
        # Filter noise tags
        normalized = [g for g in normalized if g.lower() not in NOISE_TAGS and not g.isdigit() and len(g) > 2]
        if not normalized:
            return [], 0.0, "mb_empty_after_normalize"
        confidence = 0.85 if valid_tags[0].get("count", 0) >= 3 else 0.7
        return normalized, confidence, "musicbrainz"
    except Exception as e:
        return [], 0.0, f"mb_exception_{str(e)[:40]}"


def web_lookup_genre(artist_name):
    """
    Query both MusicBrainz and Last.fm, then merge and select the best result.
    - If both return tags, merge them (deduplicated), prefer the longer list
    - If only one returns tags, use that one
    - If neither returns tags, return empty
    """
    lastfm_api_key = os.getenv("LASTFM_API_KEY", "")

    mb_genres, mb_conf, mb_source = musicbrainz_lookup_genre(artist_name)
    lfm_genres, lfm_conf, lfm_source = lastfm_lookup_genre(artist_name, lastfm_api_key)

    # Both have results — merge, deduplicate, keep up to 7
    if mb_genres and lfm_genres:
        seen = set()
        merged = []
        # Interleave: take from each source alternately to balance coverage
        for g in lfm_genres + mb_genres:
            g_lower = g.lower()
            if g_lower not in seen:
                seen.add(g_lower)
                merged.append(g)
        merged = merged[:7]
        confidence = max(mb_conf, lfm_conf)
        return merged, confidence, "musicbrainz+lastfm"

    # Only Last.fm has results
    if lfm_genres:
        return lfm_genres, lfm_conf, lfm_source

    # Only MusicBrainz has results
    if mb_genres:
        return mb_genres, mb_conf, mb_source

    # Neither has results
    return [], 0.0, "no_results"


def lastfm_lookup_album_genre(artist_name, album_name, api_key):
    """Look up album-specific tags via Last.fm API."""
    if not api_key:
        return [], 0.0, "lastfm_no_key"
    try:
        url = "https://ws.audioscrobbler.com/2.0/"
        params = {
            "method": "album.getinfo",
            "artist": artist_name,
            "album": album_name,
            "api_key": api_key,
            "format": "json",
            "autocorrect": 1,
        }
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return [], 0.0, f"lastfm_album_error_{resp.status_code}"
        data = resp.json()
        if "error" in data:
            return [], 0.0, f"lastfm_album_api_error_{data.get('error')}"
        tags = data.get("album", {}).get("tags", {}).get("tag", [])
        if not tags:
            return [], 0.0, "lastfm_album_no_tags"
        top_tags = [t["name"] for t in tags[:7] if t.get("name")]
        normalized = [simple_normalize_genre(g) for g in top_tags]
        normalized = [g for g in normalized if g]
        if not normalized:
            return [], 0.0, "lastfm_album_empty_after_normalize"
        return normalized, 0.85, "lastfm_album"
    except Exception as e:
        return [], 0.0, f"lastfm_album_exception_{str(e)[:40]}"


def enrich_missing_genres(artists_df, genre_cache_path, enable_enrichment, confidence_threshold):
    cache = read_json_file(genre_cache_path, {})
    cache_changed = False

    spotify_genre_col = "spotify_genres"
    web_genre_col = "web_genres"
    final_genre_col = "final_genres"
    genre_source_col = "genre_source"
    genre_conf_col = "genre_confidence"
    genre_notes_col = "genre_notes"

    ensure_columns(
        artists_df,
        [spotify_genre_col, web_genre_col, final_genre_col, genre_source_col, genre_conf_col, genre_notes_col],
    )

    for idx, row in artists_df.iterrows():
        artist_id = row.get("artist_id")
        artist_name = row.get("artist_name") or ""
        spotify_genres = row.get(spotify_genre_col) or ""

        if spotify_genres:
            artists_df.at[idx, final_genre_col] = spotify_genres
            artists_df.at[idx, genre_source_col] = "spotify"
            artists_df.at[idx, genre_conf_col] = 1.0
            artists_df.at[idx, genre_notes_col] = "from spotify artist genres"
            continue

        if not enable_enrichment:
            artists_df.at[idx, final_genre_col] = ""
            artists_df.at[idx, genre_source_col] = "none"
            artists_df.at[idx, genre_conf_col] = 0.0
            artists_df.at[idx, genre_notes_col] = "no spotify genres; enrichment disabled"
            continue

        cache_key = artist_id or artist_name
        if cache_key in cache:
            cached = cache[cache_key]
            web_genres = cached.get("web_genres", [])
            conf = cached.get("confidence", 0.0)
            source = cached.get("source", "cache")
            notes = cached.get("notes", "")
        else:
            web_genres, conf, source = web_lookup_genre(artist_name)
            notes = f"web source={source}"
            cache[cache_key] = {
                "web_genres": web_genres,
                "confidence": conf,
                "source": source,
                "notes": notes,
            }
            write_json_file(genre_cache_path, cache)
            cache_changed = True
            log(f"MusicBrainz [{len(cache)}/{len(artists_df)}] {artist_name}: {web_genres[:2] if web_genres else 'no results'}")

        artists_df.at[idx, web_genre_col] = ", ".join(web_genres) if web_genres else ""
        if web_genres and conf >= confidence_threshold:
            artists_df.at[idx, final_genre_col] = ", ".join(web_genres)
            artists_df.at[idx, genre_source_col] = "web"
            artists_df.at[idx, genre_conf_col] = conf
            artists_df.at[idx, genre_notes_col] = notes
        else:
            artists_df.at[idx, final_genre_col] = ""
            artists_df.at[idx, genre_source_col] = "uncertain"
            artists_df.at[idx, genre_conf_col] = conf
            artists_df.at[idx, genre_notes_col] = f"{notes}; low confidence or empty"

    if cache_changed:
        write_json_file(genre_cache_path, cache)

    return artists_df


def enrich_album_genres(albums_df, artist_genre_map, genre_cache_path, confidence_threshold):
    """
    Enrich album genres using Last.fm album-specific tags.
    - First uses artist-propagated genres as a base
    - Then queries Last.fm for album-specific tags
    - Merges both, preferring album-specific tags
    - Caches results to avoid re-fetching
    """
    lastfm_api_key = os.getenv("LASTFM_API_KEY", "")
    if not lastfm_api_key:
        log("No LASTFM_API_KEY found, skipping album genre enrichment.")
        return albums_df

    # Use a separate cache file for album genres
    album_cache_path = genre_cache_path.replace(".json", "_albums.json")
    cache = read_json_file(album_cache_path, {})

    total = len(albums_df)
    enriched = 0

    for idx, row in albums_df.iterrows():
        album_id = row.get("album_id", "")
        album_name = row.get("album_name", "") or ""
        artist_name = row.get("primary_artist_name", "") or ""
        artist_genres = artist_genre_map.get(row.get("primary_artist_id", ""), "")

        cache_key = f"{artist_name}||{album_name}"

        if cache_key in cache:
            cached = cache[cache_key]
            album_genres = cached.get("genres", [])
            source = cached.get("source", "cache")
        else:
            album_genres, conf, source = lastfm_lookup_album_genre(
                artist_name, album_name, lastfm_api_key
            )
            cache[cache_key] = {"genres": album_genres, "source": source}
            write_json_file(album_cache_path, cache)
            enriched += 1
            log(f"Album genres [{enriched}/{total}] {artist_name} - {album_name}: {album_genres[:2] if album_genres else 'no results'}")

        # Merge album-specific tags with artist tags
        if album_genres:
            # Combine: album tags first (more specific), then artist tags as supplement
            seen = set()
            merged = []
            for g in album_genres + (artist_genres.split(", ") if artist_genres else []):
                g_clean = g.strip().lower()
                if g_clean and g_clean not in seen:
                    seen.add(g_clean)
                    merged.append(g.strip())
            merged = merged[:7]
            albums_df.at[idx, "final_genres"] = ", ".join(merged)
            albums_df.at[idx, "genre_source"] = "lastfm_album"
            albums_df.at[idx, "genre_confidence"] = 0.85
        elif artist_genres:
            # Fall back to artist genres
            albums_df.at[idx, "final_genres"] = artist_genres
            albums_df.at[idx, "genre_source"] = "artist_propagated"
            albums_df.at[idx, "genre_confidence"] = 0.7

    log(f"Album genre enrichment complete. {enriched} new lookups, {total - enriched} from cache.")
    return albums_df
    cache = read_json_file(genre_cache_path, {})
    cache_changed = False

    spotify_genre_col = "spotify_genres"
    web_genre_col = "web_genres"
    final_genre_col = "final_genres"
    genre_source_col = "genre_source"
    genre_conf_col = "genre_confidence"
    genre_notes_col = "genre_notes"

    ensure_columns(
        artists_df,
        [spotify_genre_col, web_genre_col, final_genre_col, genre_source_col, genre_conf_col, genre_notes_col],
    )

    for idx, row in artists_df.iterrows():
        artist_id = row.get("artist_id")
        artist_name = row.get("artist_name") or ""
        spotify_genres = row.get(spotify_genre_col) or ""

        if spotify_genres:
            artists_df.at[idx, final_genre_col] = spotify_genres
            artists_df.at[idx, genre_source_col] = "spotify"
            artists_df.at[idx, genre_conf_col] = 1.0
            artists_df.at[idx, genre_notes_col] = "from spotify artist genres"
            continue

        if not enable_enrichment:
            artists_df.at[idx, final_genre_col] = ""
            artists_df.at[idx, genre_source_col] = "none"
            artists_df.at[idx, genre_conf_col] = 0.0
            artists_df.at[idx, genre_notes_col] = "no spotify genres; enrichment disabled"
            continue

        cache_key = artist_id or artist_name
        if cache_key in cache:
            cached = cache[cache_key]
            web_genres = cached.get("web_genres", [])
            conf = cached.get("confidence", 0.0)
            source = cached.get("source", "cache")
            notes = cached.get("notes", "")
        else:
            web_genres, conf, source = web_lookup_genre(artist_name)
            notes = f"web source={source}"
            cache[cache_key] = {
                "web_genres": web_genres,
                "confidence": conf,
                "source": source,
                "notes": notes,
            }
            # Write after every new lookup so progress is never lost
            write_json_file(genre_cache_path, cache)
            cache_changed = True
            log(f"MusicBrainz [{len(cache)}/{len(artists_df)}] {artist_name}: {web_genres[:2] if web_genres else 'no results'}")

        artists_df.at[idx, web_genre_col] = ", ".join(web_genres) if web_genres else ""
        if web_genres and conf >= confidence_threshold:
            artists_df.at[idx, final_genre_col] = ", ".join(web_genres)
            artists_df.at[idx, genre_source_col] = "web"
            artists_df.at[idx, genre_conf_col] = conf
            artists_df.at[idx, genre_notes_col] = notes
        else:
            artists_df.at[idx, final_genre_col] = ""
            artists_df.at[idx, genre_source_col] = "uncertain"
            artists_df.at[idx, genre_conf_col] = conf
            artists_df.at[idx, genre_notes_col] = f"{notes}; low confidence or empty"

    if cache_changed:
        write_json_file(genre_cache_path, cache)

    return artists_df


# -----------------------------
# Mood Inference
# -----------------------------


MOOD_BUCKETS = {
    "calm": ["ambient", "acoustic", "classical"],
    "mellow": ["soft rock", "chill", "indie folk"],
    "upbeat": ["pop", "indie pop"],
    "energetic": ["rock", "edm", "house", "techno"],
    "dark": ["metal", "industrial"],
    "reflective": ["singer-songwriter", "folk"],
    "dreamy": ["shoegaze", "dreampop"],
    "aggressive": ["hard rock", "trap", "punk"],
    "romantic": ["r&b", "soul"],
    "atmospheric": ["post-rock", "soundtrack"],
    "focused": ["lofi", "minimal"],
    "danceable": ["dance", "disco"],
}


def infer_mood(genres_str, tempo=None, energy=None):
    """
    Very simple rule-based mood inference.
    - Use genres keywords first
    - Optionally incorporate audio features later (tempo, energy)
    """
    if not genres_str:
        return "", 0.0, "none"

    genres = [g.strip().lower() for g in genres_str.split(",") if g.strip()]
    genre_hits = defaultdict(int)
    for g in genres:
        for mood, keywords in MOOD_BUCKETS.items():
            for kw in keywords:
                if kw in g:
                    genre_hits[mood] += 1

    if not genre_hits:
        return "", 0.3, "weak_genre"

    mood, hits = max(genre_hits.items(), key=lambda x: x[1])
    confidence = min(1.0, 0.5 + 0.1 * hits)

    # Optionally tweak by tempo/energy if provided
    if energy is not None:
        try:
            e = float(energy)
            if e >= 0.75 and mood in ["calm", "mellow"]:
                mood = "energetic"
                confidence = max(confidence, 0.7)
        except Exception:
            pass

    return mood, confidence, "genre_rule"


# -----------------------------
# DataFrame Builders
# -----------------------------


def build_artists_dataframe(artist_items, artist_details_map):
    rows = []
    for a in artist_items:
        artist_id = a.get("id")
        if not artist_id:
            continue
        details = artist_details_map.get(artist_id, a)
        followers = safe_get(details, "followers", "total", default=None)
        popularity = safe_get(details, "popularity", default=None)
        genres = details.get("genres", []) or []
        spotify_url = safe_get(details, "external_urls", "spotify", default="")
        images = details.get("images", []) or []
        image_url = images[0].get("url", "") if images else ""
        rows.append(
            {
                "artist_id": artist_id,
                "artist_name": details.get("name", ""),
                "artist_uri": details.get("uri", ""),
                "artist_url": spotify_url,
                "artist_image_url": image_url,
                "followers": followers,
                "popularity": popularity,
                "spotify_genres": ", ".join(genres),
                "web_genres": "",
                "final_genres": "",
                "genre_source": "",
                "genre_confidence": 0.0,
                "genre_notes": "",
                "followed_flag": True,
                "appears_in_liked_tracks_flag": False,
                "appears_in_saved_albums_flag": False,
                "active_status": True,
                "first_seen_ts": datetime.now(timezone.utc).isoformat(),
                "last_seen_ts": datetime.now(timezone.utc).isoformat(),
                "missing_run_count": 0,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.drop_duplicates(subset=["artist_id"])
    return df


def build_albums_dataframe(saved_albums):
    rows = []
    for item in saved_albums:
        added_at = item.get("added_at")
        album = item.get("album") or {}
        album_id = album.get("id")
        if not album_id:
            continue
        album_name = album.get("name", "")
        album_uri = album.get("uri", "")
        album_url = safe_get(album, "external_urls", "spotify", default="")
        album_type = album.get("album_type", "")
        artists = album.get("artists", []) or []
        primary_artist = artists[0] if artists else {}
        primary_artist_name = primary_artist.get("name", "")
        primary_artist_id = primary_artist.get("id", "")
        all_artists = ", ".join([a.get("name", "") for a in artists if a.get("name")])
        release_date = album.get("release_date", "")
        total_tracks = album.get("total_tracks", None)
        label = album.get("label", "")
        images = album.get("images", []) or []
        image_url = images[0].get("url", "") if images else ""

        rows.append(
            {
                "album_id": album_id,
                "album_name": album_name,
                "album_uri": album_uri,
                "album_url": album_url,
                "album_image_url": image_url,
                "album_type": album_type,
                "primary_artist_name": primary_artist_name,
                "primary_artist_id": primary_artist_id,
                "all_artists": all_artists,
                "added_date": added_at,
                "release_date": release_date,
                "total_tracks": total_tracks,
                "label": label,
                "spotify_genres": "",
                "web_genres": "",
                "final_genres": "",
                "genre_source": "",
                "genre_confidence": 0.0,
                "genre_notes": "",
                "mood_inferred": "",
                "mood_confidence": 0.0,
                "mood_source": "",
                "final_mood": "",
                "active_status": True,
                "first_seen_ts": datetime.now(timezone.utc).isoformat(),
                "last_seen_ts": datetime.now(timezone.utc).isoformat(),
                "missing_run_count": 0,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.drop_duplicates(subset=["album_id"])
    return df


def build_tracks_dataframe(saved_tracks, artist_details_map):
    rows = []
    for item in saved_tracks:
        added_at = item.get("added_at")
        track = item.get("track") or {}
        if not track:
            continue
        track_id = track.get("id")
        if not track_id:
            continue
        track_name = track.get("name", "")
        track_uri = track.get("uri", "")
        track_url = safe_get(track, "external_urls", "spotify", default="")
        duration_ms = track.get("duration_ms", 0) or 0
        duration_min = round(duration_ms / 60000.0, 2) if duration_ms else 0
        popularity = track.get("popularity", None)
        explicit = track.get("explicit", False)
        disc_number = track.get("disc_number", None)
        track_number = track.get("track_number", None)
        isrc = safe_get(track, "external_ids", "isrc", default="")
        album = track.get("album") or {}
        album_id = album.get("id", "")
        album_name = album.get("name", "")
        album_url = safe_get(album, "external_urls", "spotify", default="")
        release_date = album.get("release_date", "")

        artists = track.get("artists", []) or []
        primary_artist = artists[0] if artists else {}
        primary_artist_name = primary_artist.get("name", "")
        primary_artist_id = primary_artist.get("id", "")
        all_artists = ", ".join([a.get("name", "") for a in artists if a.get("name")])

        # Collect genres from primary artist details
        artist_obj = artist_details_map.get(primary_artist_id, {})
        artist_genres = artist_obj.get("genres", []) or []
        spotify_genres = ", ".join(artist_genres)

        rows.append(
            {
                "track_id": track_id,
                "track_name": track_name,
                "track_uri": track_uri,
                "track_url": track_url,
                "primary_artist_name": primary_artist_name,
                "primary_artist_id": primary_artist_id,
                "all_artists": all_artists,
                "album_name": album_name,
                "album_id": album_id,
                "album_url": album_url,
                "added_date": added_at,
                "release_date": release_date,
                "duration_ms": duration_ms,
                "duration_min": duration_min,
                "popularity": popularity,
                "explicit": explicit,
                "disc_number": disc_number,
                "track_number": track_number,
                "isrc": isrc,
                "spotify_genres": spotify_genres,
                "web_genres": "",
                "final_genres": "",
                "genre_source": "",
                "genre_confidence": 0.0,
                "genre_notes": "",
                "mood_inferred": "",
                "mood_confidence": 0.0,
                "mood_source": "",
                "final_mood": "",
                "active_status": True,
                "first_seen_ts": datetime.now(timezone.utc).isoformat(),
                "last_seen_ts": datetime.now(timezone.utc).isoformat(),
                "missing_run_count": 0,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df = df.drop_duplicates(subset=["track_id"])
    return df


# -----------------------------
# Google Sheets Auth & Helpers
# -----------------------------


def authenticate_google_sheets():
    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not service_account_file or not Path(service_account_file).exists():
        log("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON environment variable missing or file not found.")
        sys.exit(1)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = Credentials.from_service_account_file(service_account_file, scopes=scopes)
    gc = gspread.authorize(credentials)
    log("Google Sheets authentication successful.")
    return gc
    log("Google Sheets authentication successful.")
    return gc


def create_or_open_sheet(gc, config):
    sheet_name = config.get("sheet_name", "Spotify Personal Library")
    sheet_id = config.get("sheet_id", "")

    if sheet_id:
        try:
            sh = gc.open_by_key(sheet_id)
            log(f"Opened existing Google Sheet by ID: {sheet_id}")
            return sh
        except Exception as e:
            import traceback
            log(f"ERROR: failed to open sheet by ID ({sheet_id}). Type: {type(e)}, Repr: {repr(e)}")
            traceback.print_exc()
            log("Make sure the spreadsheet is shared with the service account email as Editor.")
            sys.exit(1)

    # Try by name, else create
    try:
        sh = gc.open(sheet_name)
        log(f"Opened existing Google Sheet by name: {sheet_name}")
        return sh
    except gspread.SpreadsheetNotFound:
        pass
    except Exception as e:
        log(f"Warning: error opening sheet by name: {e}")

    try:
        sh = gc.create(sheet_name)
        log(f"Created new Google Sheet: {sheet_name}")
        return sh
    except Exception as e:
        log(f"ERROR: failed to create Google Sheet: {e}")
        sys.exit(1)
def get_or_create_worksheet(sh, title, rows=1000, cols=50):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
    return ws


def clear_and_set_dataframe(ws, df):
    if df is None or df.empty:
        ws.clear()
        if df is not None and df.columns.any():
            ws.update("A1", [list(df.columns)])
        return

    ws.clear()
    values = [list(df.columns)] + df.fillna("").astype(str).values.tolist()
    ws.update(values, "A1")


def read_worksheet_to_dataframe(ws):
    try:
        data = ws.get_all_records()
        df = pd.DataFrame(data)
        return df
    except Exception:
        return pd.DataFrame()


# -----------------------------
# Config & Local State
# -----------------------------


def load_config_tab(sh, config):
    tab_name = config["tab_names"]["config"]
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=50, cols=10)
        df = pd.DataFrame([DEFAULT_CONFIG])
        clear_and_set_dataframe(ws, df)
        return DEFAULT_CONFIG

    df = read_worksheet_to_dataframe(ws)
    if df.empty:
        return config
    row = df.iloc[0].to_dict()
    merged = DEFAULT_CONFIG.copy()
    merged.update(config)
    for k, v in row.items():
        if v in [None, ""]:
            continue
        if isinstance(merged.get(k), dict):
            continue
        merged[k] = v
    return merged


def write_config_tab(sh, config):
    tab_name = config["tab_names"]["config"]
    ws = get_or_create_worksheet(sh, tab_name, rows=50, cols=20)
    df = pd.DataFrame([config])
    clear_and_set_dataframe(ws, df)


def load_manual_overrides(sh, config):
    tab_name = config["tab_names"]["manual_overrides"]
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=200, cols=10)
        cols = [
            "entity_type",
            "spotify_id",
            "override_genre",
            "override_mood",
            "clean_display_name",
            "notes",
            "active_flag",
        ]
        clear_and_set_dataframe(ws, pd.DataFrame(columns=cols))
        return pd.DataFrame(columns=cols)

    df = read_worksheet_to_dataframe(ws)
    return df


def load_local_state(config):
    path = config.get("sync_state_file", DEFAULT_SYNC_STATE)
    return read_json_file(path, {})


def save_local_state(config, state):
    path = config.get("sync_state_file", DEFAULT_SYNC_STATE)
    write_json_file(path, state)


# -----------------------------
# Incremental Sync Logic
# -----------------------------


def compare_with_existing_data(new_df, existing_df, id_col, missing_threshold):
    """
    Compare new_df vs existing_df by id_col.
    - Returns (merged_df, stats_dict, missing_ids_list)
    - merged_df has updated/added rows, with missing_run_count / active_status updated.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    stats = {"added": 0, "updated": 0, "marked_inactive": 0, "reactivated": 0}

    existing_df = existing_df.copy()
    if id_col not in existing_df.columns:
        existing_df = pd.DataFrame()
    ensure_columns(
        existing_df,
        ["active_status", "missing_run_count", "first_seen_ts", "last_seen_ts"],
    )
    if not existing_df.empty:
        existing_df[id_col] = existing_df[id_col].astype(str)
    new_df = new_df.copy()
    new_df[id_col] = new_df[id_col].astype(str)

    existing_index = {str(row[id_col]): i for i, row in existing_df.iterrows()}
    merged_rows = []

    new_ids_set = set(new_df[id_col].astype(str).tolist())

    for _, new_row in new_df.iterrows():
        entity_id = str(new_row[id_col])
        if not entity_id:
            continue
        if entity_id in existing_index:
            idx = existing_index[entity_id]
            existing_row = existing_df.loc[idx].to_dict()
            for col in new_df.columns:
                if col == "first_seen_ts":
                    continue
                existing_row[col] = new_row.get(col)
            existing_row["last_seen_ts"] = now_iso
            existing_row["missing_run_count"] = 0
            if not existing_row.get("active_status", True):
                existing_row["active_status"] = True
                stats["reactivated"] += 1
            stats["updated"] += 1
            merged_rows.append(existing_row)
        else:
            row_dict = new_row.to_dict()
            row_dict["first_seen_ts"] = now_iso
            row_dict["last_seen_ts"] = now_iso
            row_dict["missing_run_count"] = 0
            row_dict["active_status"] = True
            stats["added"] += 1
            merged_rows.append(row_dict)

    # Entities missing this run
    missing_ids = []
    for _, old_row in existing_df.iterrows():
        entity_id = str(old_row[id_col])
        if entity_id not in new_ids_set:
            row_dict = old_row.to_dict()
            row_dict["missing_run_count"] = int(row_dict.get("missing_run_count", 0)) + 1
            if row_dict["missing_run_count"] >= missing_threshold:
                if row_dict.get("active_status", True):
                    row_dict["active_status"] = False
                    stats["marked_inactive"] += 1
            merged_rows.append(row_dict)
            missing_ids.append(entity_id)

    merged_df = pd.DataFrame(merged_rows)
    return merged_df, stats, missing_ids


def apply_manual_overrides(df, overrides_df, id_col, name_col):
    if overrides_df is None or overrides_df.empty:
        return df

    df = df.copy()
    ensure_columns(df, ["final_genres", "final_mood"])
    overrides_df = overrides_df[overrides_df.get("active_flag", True).astype(str).str.lower().isin(["true", "1", "yes", "y"])]

    override_map = {}
    for _, row in overrides_df.iterrows():
        entity_type = str(row.get("entity_type", "")).strip().lower()
        spotify_id = str(row.get("spotify_id", "")).strip()
        if not spotify_id:
            continue
        override_map[(entity_type, spotify_id)] = row.to_dict()

    entity_type_guess = name_col.split("_")[0]  # e.g., "track", "album", "artist"

    for idx, row in df.iterrows():
        entity_id = str(row.get(id_col, ""))
        key = (entity_type_guess, entity_id)
        if key not in override_map:
            continue
        o = override_map[key]
        if o.get("override_genre"):
            df.at[idx, "final_genres"] = o.get("override_genre")
        if o.get("override_mood"):
            df.at[idx, "final_mood"] = o.get("override_mood")
        if o.get("clean_display_name"):
            df.at[idx, name_col] = o.get("clean_display_name")
    return df


# -----------------------------
# Derived / Browse Tabs Builders
# -----------------------------


def add_hyperlink_columns_tracks(df):
    df = df.copy()
    url_col = "track_url"
    name_col = "track_name"
    link_col = "track_link"
    df[link_col] = df.apply(
        lambda r: f'=HYPERLINK("{r.get(url_col, "")}","{r.get(name_col, "")}")' if r.get(url_col) else r.get(name_col, ""),
        axis=1,
    )
    # artist & album links
    a_url_col = "artist_url"
    a_name_col = "primary_artist_name"
    if a_url_col not in df.columns:
        df[a_url_col] = ""
    df["artist_link"] = df.apply(
        lambda r: f'=HYPERLINK("{r.get(a_url_col, "")}","{r.get(a_name_col, "")}")' if r.get(a_url_col) else r.get(a_name_col, ""),
        axis=1,
    )
    alb_url_col = "album_url"
    alb_name_col = "album_name"
    df["album_link"] = df.apply(
        lambda r: f'=HYPERLINK("{r.get(alb_url_col, "")}","{r.get(alb_name_col, "")}")' if r.get(alb_url_col) else r.get(alb_name_col, ""),
        axis=1,
    )
    return df


def add_hyperlink_columns_albums(df):
    df = df.copy()
    url_col = "album_url"
    name_col = "album_name"
    df["album_link"] = df.apply(
        lambda r: f'=HYPERLINK("{r.get(url_col, "")}","{r.get(name_col, "")}")' if r.get(url_col) else r.get(name_col, ""),
        axis=1,
    )
    a_url_col = "artist_url"
    a_name_col = "primary_artist_name"
    if a_url_col not in df.columns:
        df[a_url_col] = ""
    df["artist_link"] = df.apply(
        lambda r: f'=HYPERLINK("{r.get(a_url_col, "")}","{r.get(a_name_col, "")}")' if r.get(a_url_col) else r.get(a_name_col, ""),
        axis=1,
    )
    return df


def add_hyperlink_columns_artists(df):
    df = df.copy()
    url_col = "artist_url"
    name_col = "artist_name"
    df["artist_link"] = df.apply(
        lambda r: f'=HYPERLINK("{r.get(url_col, "")}","{r.get(name_col, "")}")' if r.get(url_col) else r.get(name_col, ""),
        axis=1,
    )
    return df


def rebuild_browse_tabs(sh, config, tracks_raw_df, albums_raw_df, artists_raw_df):
    tab_names = config["tab_names"]

    # Ensure artist_url exists on tracks/albums by joining from artists_raw
    if not artists_raw_df.empty:
        art_subset = artists_raw_df[["artist_id", "artist_url", "final_genres"]].copy()
        art_subset = art_subset.rename(columns={"final_genres": "artist_final_genres"})
    else:
        art_subset = pd.DataFrame(columns=["artist_id", "artist_url", "artist_final_genres"])

    # Browse_Tracks
    if not tracks_raw_df.empty:
        bt = tracks_raw_df.copy()
        bt = bt.merge(
            art_subset[["artist_id", "artist_url", "artist_final_genres"]],
            how="left",
            left_on="primary_artist_id",
            right_on="artist_id",
            suffixes=("", "_artist"),
        )
        if "artist_id_artist" in bt.columns:
            bt = bt.drop(columns=["artist_id_artist"])
        if "final_genres" in bt.columns and "artist_final_genres" in bt.columns:
            bt["final_genres"] = bt.apply(
                lambda r: r["artist_final_genres"] if r["final_genres"] == "" else r["final_genres"], axis=1
            )
        bt = add_hyperlink_columns_tracks(bt)
        bt = bt[
            [
                "final_genres",
                "final_mood",
                "primary_artist_name",
                "album_name",
                "track_link",
                "artist_link",
                "album_link",
                "track_name",
                "track_url",
                "artist_url",
                "album_url",
                "added_date",
                "release_date",
                "duration_min",
                "popularity",
                "explicit",
                "active_status",
            ]
            + [c for c in bt.columns if c not in [
                "final_genres",
                "final_mood",
                "primary_artist_name",
                "album_name",
                "track_link",
                "artist_link",
                "album_link",
                "track_name",
                "track_url",
                "artist_url",
                "album_url",
                "added_date",
                "release_date",
                "duration_min",
                "popularity",
                "explicit",
                "active_status",
            ]]
        ]
    else:
        bt = pd.DataFrame()

    ws_bt = get_or_create_worksheet(sh, tab_names["browse_tracks"])
    clear_and_set_dataframe(ws_bt, bt)

    # Browse_Albums
    if not albums_raw_df.empty:
        ba = albums_raw_df.copy()
        # add artist_url from artists_raw
        ba = ba.merge(
            art_subset[["artist_id", "artist_url", "artist_final_genres"]],
            how="left",
            left_on="primary_artist_id",
            right_on="artist_id",
            suffixes=("", "_artist"),
        )
        if "artist_id_artist" in ba.columns:
            ba = ba.drop(columns=["artist_id_artist"])
        if "final_genres" in ba.columns and "artist_final_genres" in ba.columns:
            ba["final_genres"] = ba.apply(
                lambda r: r["artist_final_genres"] if r["final_genres"] == "" else r["final_genres"], axis=1
            )
        ba = add_hyperlink_columns_albums(ba)
        ba = ba[
            [
                "final_genres",
                "final_mood",
                "primary_artist_name",
                "album_link",
                "artist_link",
                "album_name",
                "album_url",
                "artist_url",
                "album_type",
                "added_date",
                "release_date",
                "total_tracks",
                "label",
                "active_status",
            ]
            + [c for c in ba.columns if c not in [
                "final_genres",
                "final_mood",
                "primary_artist_name",
                "album_link",
                "artist_link",
                "album_name",
                "album_url",
                "artist_url",
                "album_type",
                "added_date",
                "release_date",
                "total_tracks",
                "label",
                "active_status",
            ]]
        ]
    else:
        ba = pd.DataFrame()
    ws_ba = get_or_create_worksheet(sh, tab_names["browse_albums"])
    clear_and_set_dataframe(ws_ba, ba)

    # Browse_Artists
    if not artists_raw_df.empty:
        bar = artists_raw_df.copy()
        bar = add_hyperlink_columns_artists(bar)
        bar = bar[
            [
                "final_genres",
                "artist_link",
                "artist_name",
                "artist_url",
                "followers",
                "popularity",
                "followed_flag",
                "appears_in_liked_tracks_flag",
                "appears_in_saved_albums_flag",
                "active_status",
            ]
            + [c for c in bar.columns if c not in [
                "final_genres",
                "artist_link",
                "artist_name",
                "artist_url",
                "followers",
                "popularity",
                "followed_flag",
                "appears_in_liked_tracks_flag",
                "appears_in_saved_albums_flag",
                "active_status",
            ]]
        ]
    else:
        bar = pd.DataFrame()
    ws_bar = get_or_create_worksheet(sh, tab_names["browse_artists"])
    clear_and_set_dataframe(ws_bar, bar)

    # By_Genre
    if not tracks_raw_df.empty:
        bg_rows = []
        for _, r in tracks_raw_df.iterrows():
            genres = (r.get("final_genres") or "").split(",")
            for g in genres:
                g = g.strip()
                if not g:
                    continue
                bg_rows.append(
                    {
                        "genre": g,
                        "track_id": r.get("track_id"),
                        "track_name": r.get("track_name"),
                        "track_url": r.get("track_url"),
                        "primary_artist_name": r.get("primary_artist_name"),
                        "album_name": r.get("album_name"),
                        "added_date": r.get("added_date", ""),
                    }
                )
        bg = pd.DataFrame(bg_rows)
        if not bg.empty:
            bg["track_link"] = bg.apply(
                lambda r: f'=HYPERLINK("{r.get("track_url","")}","{r.get("track_name","")}")' if r.get("track_url") else r.get("track_name", ""),
                axis=1,
            )
    else:
        bg = pd.DataFrame()
    ws_bg = get_or_create_worksheet(sh, tab_names["by_genre"])
    clear_and_set_dataframe(ws_bg, bg)

    # By_Genre_Artists
    if not artists_raw_df.empty:
        # Build a map of artist_id -> most recent album added_date
        most_recent_album_date = {}
        if not albums_raw_df.empty:
            for _, alb in albums_raw_df.iterrows():
                aid = alb.get("primary_artist_id", "")
                date = alb.get("added_date", "")
                if aid and date:
                    if aid not in most_recent_album_date or date > most_recent_album_date[aid]:
                        most_recent_album_date[aid] = date

        # Also build by artist name as fallback
        most_recent_album_date_by_name = {}
        if not albums_raw_df.empty:
            for _, alb in albums_raw_df.iterrows():
                aname = alb.get("primary_artist_name", "")
                date = alb.get("added_date", "")
                if aname and date:
                    if aname not in most_recent_album_date_by_name or date > most_recent_album_date_by_name[aname]:
                        most_recent_album_date_by_name[aname] = date

        bga_rows = []
        canonical_set = set(CANONICAL_BUCKETS)
        for _, r in artists_raw_df.iterrows():
            genres = (r.get("final_genres") or "").split(",")
            artist_id = r.get("artist_id", "")
            artist_name = r.get("artist_name", "")
            # Use most recent album date as proxy, fall back to first_seen_ts
            proxy_date = (
                most_recent_album_date.get(artist_id)
                or most_recent_album_date_by_name.get(artist_name)
                or r.get("first_seen_ts", "")
            )
            seen_buckets = set()
            for g in genres:
                g = g.strip()
                if not g or g not in canonical_set or g in seen_buckets:
                    continue
                seen_buckets.add(g)
                bga_rows.append(
                    {
                        "genre": g,
                        "artist_name": artist_name,
                        "artist_url": r.get("artist_url"),
                        "artist_image_url": r.get("artist_image_url", ""),
                        "followers": r.get("followers"),
                        "popularity": r.get("popularity"),
                        "added_date": proxy_date,
                    }
                )
        bga = pd.DataFrame(bga_rows)
        if not bga.empty:
            bga["artist_link"] = bga.apply(
                lambda r: f'=HYPERLINK("{r.get("artist_url","")}","{r.get("artist_name","")}")' if r.get("artist_url") else r.get("artist_name", ""),
                axis=1,
            )
            bga = bga[["genre", "artist_link", "artist_name", "artist_image_url", "artist_url", "followers", "popularity", "added_date"]]
            bga = bga.sort_values(["genre", "artist_name"])
    else:
        bga = pd.DataFrame()
    ws_bga = get_or_create_worksheet(sh, tab_names["by_genre_artists"])
    clear_and_set_dataframe(ws_bga, bga)

    # By_Genre_Albums
    if not albums_raw_df.empty:
        bgal_rows = []
        canonical_set = set(CANONICAL_BUCKETS)
        for _, r in albums_raw_df.iterrows():
            genres = (r.get("final_genres") or "").split(",")
            seen_buckets = set()
            for g in genres:
                g = g.strip()
                if not g or g not in canonical_set or g in seen_buckets:
                    continue
                seen_buckets.add(g)
                bgal_rows.append(
                    {
                        "genre": g,
                        "primary_artist_name": r.get("primary_artist_name"),
                        "album_name": r.get("album_name"),
                        "album_url": r.get("album_url"),
                        "album_image_url": r.get("album_image_url", ""),
                        "artist_url": r.get("artist_url", ""),
                        "release_date": r.get("release_date"),
                        "total_tracks": r.get("total_tracks"),
                        "added_date": r.get("added_date"),
                    }
                )
        bgal = pd.DataFrame(bgal_rows)
        if not bgal.empty:
            bgal["album_link"] = bgal.apply(
                lambda r: f'=HYPERLINK("{r.get("album_url","")}","{r.get("album_name","")}")' if r.get("album_url") else r.get("album_name", ""),
                axis=1,
            )
            bgal["artist_link"] = bgal.apply(
                lambda r: f'=HYPERLINK("{r.get("artist_url","")}","{r.get("primary_artist_name","")}")' if r.get("artist_url") else r.get("primary_artist_name", ""),
                axis=1,
            )
            bgal = bgal[["genre", "artist_link", "album_link", "primary_artist_name", "album_name", "album_image_url", "album_url", "artist_url", "release_date", "total_tracks", "added_date"]]
            bgal = bgal.sort_values(["genre", "primary_artist_name", "album_name"])
    else:
        bgal = pd.DataFrame()
    ws_bgal = get_or_create_worksheet(sh, tab_names["by_genre_albums"])
    clear_and_set_dataframe(ws_bgal, bgal)

    # Artist_Saved_Albums
    if not albums_raw_df.empty:
        asa = albums_raw_df.copy()
        asa = asa.merge(
            art_subset[["artist_id", "artist_url"]],
            how="left",
            left_on="primary_artist_id",
            right_on="artist_id",
            suffixes=("", "_artist"),
        )
        if "artist_id_artist" in asa.columns:
            asa = asa.drop(columns=["artist_id_artist"])
        asa = add_hyperlink_columns_albums(asa)
        asa = asa[
            [
                "primary_artist_name",
                "artist_link",
                "album_link",
                "album_name",
                "album_url",
                "artist_url",
                "final_genres",
                "release_date",
                "total_tracks",
                "active_status",
            ]
            + [c for c in asa.columns if c not in [
                "primary_artist_name",
                "artist_link",
                "album_link",
                "album_name",
                "album_url",
                "artist_url",
                "final_genres",
                "release_date",
                "total_tracks",
                "active_status",
            ]]
        ]
    else:
        asa = pd.DataFrame()
    ws_asa = get_or_create_worksheet(sh, tab_names["artist_saved_albums"])
    clear_and_set_dataframe(ws_asa, asa)


def rebuild_needs_review_tab(sh, config, tracks_df, albums_df, artists_df):
    tab_name = config["tab_names"]["needs_review"]
    rows = []

    def add_review(entity_type, spotify_id, display_name, issue_type, cur_val, web_val, sugg, conf, notes):
        rows.append(
            {
                "entity_type": entity_type,
                "spotify_id": spotify_id,
                "display_name": display_name,
                "issue_type": issue_type,
                "current_spotify_value": cur_val,
                "current_web_value": web_val,
                "suggested_final_value": sugg,
                "confidence": conf,
                "notes": notes,
                "last_reviewed_ts": "",
            }
        )

    # Artists: genre issues
    if not artists_df.empty:
        for _, r in artists_df.iterrows():
            if not r.get("final_genres"):
                src = r.get("genre_source", "")
                conf = r.get("genre_confidence", 0.0)
                if src in ["none", "uncertain"] or float(conf) < float(
                    DEFAULT_CONFIG["genre_confidence_threshold"]
                ):
                    add_review(
                        "artist",
                        r.get("artist_id"),
                        r.get("artist_name"),
                        "genre_missing_or_uncertain",
                        r.get("spotify_genres", ""),
                        r.get("web_genres", ""),
                        "",
                        conf,
                        r.get("genre_notes", ""),
                    )

    # Tracks: mood issues
    if not tracks_df.empty:
        for _, r in tracks_df.iterrows():
            if not r.get("final_mood"):
                conf = r.get("mood_confidence", 0.0)
                if float(conf) < float(DEFAULT_CONFIG["mood_confidence_threshold"]):
                    add_review(
                        "track",
                        r.get("track_id"),
                        r.get("track_name"),
                        "mood_missing_or_uncertain",
                        "",
                        "",
                        r.get("mood_inferred", ""),
                        conf,
                        "auto mood inference low confidence",
                    )

    df = pd.DataFrame(rows)
    ws = get_or_create_worksheet(sh, tab_name, rows=max(100, len(rows) + 10), cols=20)
    clear_and_set_dataframe(ws, df)


def write_sync_log(sh, config, summary):
    tab_name = config["tab_names"]["sync_log"]
    ws = get_or_create_worksheet(sh, tab_name, rows=1000, cols=20)
    df_existing = read_worksheet_to_dataframe(ws)
    row = summary.copy()
    row["run_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    new_row_df = pd.DataFrame([row])
    if df_existing is None or df_existing.empty:
        df_final = new_row_df
    else:
        df_final = pd.concat([df_existing, new_row_df], ignore_index=True)
    clear_and_set_dataframe(ws, df_final)


# -----------------------------
# Main Orchestration
# -----------------------------


def main():
    args = load_env_and_args()

    # Determine mode
    if args.initial_load:
        mode = "initial_load"
    elif args.update:
        mode = "update"
    else:
        mode = DEFAULT_CONFIG["default_run_mode"]

    dry_run = args.dry_run
    genre_enrich_flag = args.genre_enrich
    rebuild_derived_only = args.rebuild_derived

    # Authenticate
    gc = authenticate_google_sheets()
    # Use default config until Config tab is loaded
    config = DEFAULT_CONFIG.copy()

    sh = create_or_open_sheet(gc, config)

    # Pull updated config from Config tab
    config = load_config_tab(sh, config)

    if args.config_sheet_name:
        config["sheet_name"] = args.config_sheet_name
    if args.config_sheet_id:
        config["sheet_id"] = args.config_sheet_id

    # Persist config back to sheet (in case updated)
    write_config_tab(sh, config)

    # If only rebuilding derived tabs, we just read raw tabs
    tab_names = config["tab_names"]
    missing_threshold = int(config.get("missing_runs_threshold", 2))
    genre_conf_thresh = float(config.get("genre_confidence_threshold", 0.6))
    mood_conf_thresh = float(config.get("mood_confidence_threshold", 0.6))
    genre_enrichment_enabled = False

    # Load raw data
    def load_raw_df(tab_key, id_col):
        try:
            ws = sh.worksheet(tab_names[tab_key])
            df = read_worksheet_to_dataframe(ws)
            if not df.empty and id_col in df.columns:
                df[id_col] = df[id_col].astype(str)
            return df
        except gspread.WorksheetNotFound:
            return pd.DataFrame()

    tracks_raw_df = load_raw_df("tracks_raw", "track_id")
    albums_raw_df = load_raw_df("albums_raw", "album_id")
    artists_raw_df = load_raw_df("artists_raw", "artist_id")

    overrides_df = load_manual_overrides(sh, config)

    if rebuild_derived_only:
        rebuild_browse_tabs(sh, config, tracks_raw_df, albums_raw_df, artists_raw_df)
        rebuild_needs_review_tab(sh, config, tracks_raw_df, albums_raw_df, artists_raw_df)
        log("Rebuilt derived/browse tabs only.")
        return

    # Ensure checkpoints directory exists
    os.makedirs("checkpoints", exist_ok=True)

    # Spotify fetch flows
    sp = authenticate_spotify(config)
    saved_tracks = fetch_saved_tracks(sp)
    saved_albums = fetch_saved_albums(sp)
    followed_artists = fetch_followed_artists(sp)
    recently_played = fetch_recently_played(sp)

    # Build artist details map from followed artists + other artists seen in tracks/albums
    artist_ids = set()
    for a in followed_artists:
        if a.get("id"):
            artist_ids.add(a["id"])
    for item in saved_tracks:
        for a in safe_get(item, "track", "artists", default=[]):
            if a.get("id"):
                artist_ids.add(a["id"])
    for item in saved_albums:
        for a in safe_get(item, "album", "artists", default=[]):
            if a.get("id"):
                artist_ids.add(a["id"])

    # GET /v1/artists requires Extended Quota Access on Spotify dev apps.
    # The followed_artists objects already contain genres directly, so we use
    # those instead. artist_details_map is left empty intentionally.
    artist_details_map = {}

    # Build dataframes
    new_artists_df = build_artists_dataframe(followed_artists, artist_details_map)

    # Mark appears_in_* flags using track/album data
    track_artist_ids = set()
    for item in saved_tracks:
        for a in safe_get(item, "track", "artists", default=[]):
            if a.get("id"):
                track_artist_ids.add(a["id"])
    album_artist_ids = set()
    for item in saved_albums:
        for a in safe_get(item, "album", "artists", default=[]):
            if a.get("id"):
                album_artist_ids.add(a["id"])

    if not new_artists_df.empty:
        new_artists_df["appears_in_liked_tracks_flag"] = new_artists_df["artist_id"].isin(
            track_artist_ids
        )
        new_artists_df["appears_in_saved_albums_flag"] = new_artists_df["artist_id"].isin(
            album_artist_ids
        )

    # ── Merge in album-only artists (saved albums but not followed) ──────────
    # Build a map of artist_id -> best album image and most recent added_at
    followed_ids = set(new_artists_df["artist_id"].tolist()) if not new_artists_df.empty else set()
    album_only_map = {}
    for item in saved_albums:
        added_at = item.get("added_at", "")
        album = item.get("album") or {}
        images = album.get("images", []) or []
        image_url = images[0].get("url", "") if images else ""
        for a in album.get("artists", []):
            aid = a.get("id")
            aname = a.get("name", "")
            if not aid or aid in followed_ids:
                continue
            # Skip "Various Artists"
            if aname.lower() == "various artists":
                continue
            if aid not in album_only_map:
                album_only_map[aid] = {
                    "artist_id": aid,
                    "artist_name": aname,
                    "artist_uri": f"spotify:artist:{aid}",
                    "artist_url": f"https://open.spotify.com/artist/{aid}",
                    "artist_image_url": image_url,
                    "followers": None,
                    "popularity": None,
                    "spotify_genres": "",
                    "web_genres": "",
                    "final_genres": "",
                    "genre_source": "",
                    "genre_confidence": 0.0,
                    "genre_notes": "",
                    "followed_flag": False,
                    "appears_in_liked_tracks_flag": aid in track_artist_ids,
                    "appears_in_saved_albums_flag": True,
                    "active_status": True,
                    "first_seen_ts": datetime.now(timezone.utc).isoformat(),
                    "last_seen_ts": datetime.now(timezone.utc).isoformat(),
                    "missing_run_count": 0,
                }
            else:
                # Keep most recent album image (best proxy for artist image)
                if added_at > album_only_map[aid].get("last_seen_ts", ""):
                    if image_url:
                        album_only_map[aid]["artist_image_url"] = image_url

    if album_only_map:
        album_only_df = pd.DataFrame(list(album_only_map.values()))
        log(f"Adding {len(album_only_df)} album-only artists (saved albums but not followed).")
        new_artists_df = pd.concat([new_artists_df, album_only_df], ignore_index=True)
        new_artists_df = new_artists_df.drop_duplicates(subset=["artist_id"])

    new_albums_df = build_albums_dataframe(saved_albums)
    new_tracks_df = build_tracks_dataframe(saved_tracks, artist_details_map)

    # Add artist_url to tracks & albums via artist_details_map
    # Fall back to constructing URL from artist ID if not in map
    if not new_tracks_df.empty:
        new_tracks_df["artist_url"] = new_tracks_df["primary_artist_id"].map(
            lambda aid: safe_get(artist_details_map.get(aid, {}), "external_urls", "spotify", default="")
                        or (f"https://open.spotify.com/artist/{aid}" if aid else "")
        )
    if not new_albums_df.empty:
        new_albums_df["artist_url"] = new_albums_df["primary_artist_id"].map(
            lambda aid: safe_get(artist_details_map.get(aid, {}), "external_urls", "spotify", default="")
                        or (f"https://open.spotify.com/artist/{aid}" if aid else "")
        )

    # Genre enrichment on artists
    genre_cache_file = config.get("genre_cache_file", DEFAULT_GENRE_CACHE)
    if not new_artists_df.empty:
        # First pass: set final_genres from spotify where available
        new_artists_df["final_genres"] = new_artists_df["spotify_genres"].fillna("")
        new_artists_df["genre_source"] = new_artists_df["final_genres"].apply(
            lambda x: "spotify" if x else "none"
        )
        new_artists_df["genre_confidence"] = new_artists_df["final_genres"].apply(
            lambda x: 1.0 if x else 0.0
        )
        # Second pass: enrich artists still missing genres via MusicBrainz
        missing_count = (new_artists_df["final_genres"] == "").sum()
        if missing_count > 0:
            log(f"Enriching {missing_count} artists with missing genres via MusicBrainz...")
            new_artists_df = enrich_missing_genres(
                new_artists_df, genre_cache_file, True, genre_conf_thresh
            )
        # Third pass: consolidate micro-genres to parent categories
        log("Consolidating artist genres to parent categories...")
        new_artists_df["final_genres"] = new_artists_df["final_genres"].apply(consolidate_genres)

    # Propagate artist final_genres to albums/tracks
    if not new_artists_df.empty:
        art_genre_map = new_artists_df.set_index("artist_id")["final_genres"].to_dict()
    else:
        art_genre_map = {}

    if not new_albums_df.empty:
        new_albums_df["spotify_genres"] = new_albums_df["primary_artist_id"].map(
            lambda aid: art_genre_map.get(aid, "")
        )
        new_albums_df["final_genres"] = new_albums_df["spotify_genres"]
        new_albums_df["genre_source"] = new_albums_df["final_genres"].apply(
            lambda x: "artist_propagated" if x else "none"
        )
        # Enrich with album-specific Last.fm tags
        album_missing = (new_albums_df["final_genres"] == "").sum()
        log(f"Enriching album genres via Last.fm ({len(new_albums_df)} albums, {album_missing} with no artist genres)...")
        new_albums_df = enrich_album_genres(
            new_albums_df, art_genre_map, genre_cache_file, genre_conf_thresh
        )
        # Consolidate micro-genres to parent categories
        log("Consolidating album genres to parent categories...")
        new_albums_df["final_genres"] = new_albums_df["final_genres"].apply(consolidate_genres)

    if not new_tracks_df.empty:
        # keep existing spotify_genres if present; else propagate
        def merge_track_genres(row):
            existing = row.get("spotify_genres") or ""
            from_artist = art_genre_map.get(row.get("primary_artist_id"), "")
            if existing:
                return existing
            return from_artist

        new_tracks_df["spotify_genres"] = new_tracks_df.apply(merge_track_genres, axis=1)
        new_tracks_df["final_genres"] = new_tracks_df["spotify_genres"]
        new_tracks_df["genre_source"] = new_tracks_df["final_genres"].apply(
            lambda x: "artist_propagated" if x else "none"
        )

    # Mood inference (tracks and albums)
    if not new_tracks_df.empty:
        moods = [infer_mood(g) for g in new_tracks_df["final_genres"].fillna("")]
        new_tracks_df["mood_inferred"] = [m[0] for m in moods]
        new_tracks_df["mood_confidence"] = [m[1] for m in moods]
        new_tracks_df["mood_source"] = [m[2] for m in moods]
        new_tracks_df["final_mood"] = [
            mo if (mo and conf >= mood_conf_thresh) else "" for mo, conf in zip(
                new_tracks_df["mood_inferred"], new_tracks_df["mood_confidence"]
            )
        ]

    if not new_albums_df.empty:
        moods = [infer_mood(g) for g in new_albums_df["final_genres"].fillna("")]
        new_albums_df["mood_inferred"] = [m[0] for m in moods]
        new_albums_df["mood_confidence"] = [m[1] for m in moods]
        new_albums_df["mood_source"] = [m[2] for m in moods]
        new_albums_df["final_mood"] = [
            mo if (mo and conf >= mood_conf_thresh) else "" for mo, conf in zip(
                new_albums_df["mood_inferred"], new_albums_df["mood_confidence"]
            )
        ]

    # Apply manual overrides
    new_tracks_df = apply_manual_overrides(new_tracks_df, overrides_df, "track_id", "track_name")
    new_albums_df = apply_manual_overrides(new_albums_df, overrides_df, "album_id", "album_name")
    new_artists_df = apply_manual_overrides(new_artists_df, overrides_df, "artist_id", "artist_name")

    # Incremental compare
    tracks_merged, tracks_stats, tracks_missing = compare_with_existing_data(
        new_tracks_df, tracks_raw_df, "track_id", missing_threshold
    )
    albums_merged, albums_stats, albums_missing = compare_with_existing_data(
        new_albums_df, albums_raw_df, "album_id", missing_threshold
    )
    artists_merged, artists_stats, artists_missing = compare_with_existing_data(
        new_artists_df, artists_raw_df, "artist_id", missing_threshold
    )

    # Dry run summary
    summary = {
        "run_mode": mode,
        "dry_run": dry_run,
        "tracks_added": tracks_stats["added"],
        "tracks_updated": tracks_stats["updated"],
        "tracks_marked_inactive": tracks_stats["marked_inactive"],
        "albums_added": albums_stats["added"],
        "albums_updated": albums_stats["updated"],
        "albums_marked_inactive": albums_stats["marked_inactive"],
        "artists_added": artists_stats["added"],
        "artists_updated": artists_stats["updated"],
        "artists_marked_inactive": artists_stats["marked_inactive"],
        "errors": "",
        "status": "ok",
    }

    log("Dry-run summary of planned changes:")
    log(json.dumps(summary, indent=2))

    if dry_run:
        # Optionally record log row
        write_sync_log(sh, config, summary)
        log("Dry run complete. No data written (except Sync_Log).")
        return

    # Write raw tabs
    ws_tracks = get_or_create_worksheet(sh, tab_names["tracks_raw"])
    ws_albums = get_or_create_worksheet(sh, tab_names["albums_raw"])
    ws_artists = get_or_create_worksheet(sh, tab_names["artists_raw"])
    clear_and_set_dataframe(ws_tracks, tracks_merged)
    clear_and_set_dataframe(ws_albums, albums_merged)
    clear_and_set_dataframe(ws_artists, artists_merged)

    # Rebuild derived tabs
    rebuild_browse_tabs(sh, config, tracks_merged, albums_merged, artists_merged)
    rebuild_needs_review_tab(sh, config, tracks_merged, albums_merged, artists_merged)

    # Write recently played tab
    recently_played_df = build_recently_played_dataframe(recently_played, artist_details_map)
    write_recently_played_tab(sh, config, recently_played_df)
    log(f"Written {len(recently_played_df)} recently played tracks.")

    # Local state (optional)
    state = {
        "last_run_ts": datetime.now(timezone.utc).isoformat(),
        "last_run_mode": mode,
        "last_summary": summary,
    }
    save_local_state(config, state)

    # Sync log
    write_sync_log(sh, config, summary)
    log("Sync complete.")


if __name__ == "__main__":
    main()
