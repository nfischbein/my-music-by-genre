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


def save_checkpoint(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_checkpoint(path, default=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
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


def simple_normalize_genre(name):
    if not name:
        return None
    g = name.lower().strip()
    g = re.sub(r"[^a-z0-9 +/&-]", "", g)
    g = g.replace("&", "and")
    g = re.sub(r"\s+", " ", g)
    return g or None


def web_lookup_genre(artist_name):
    """
    Look up artist genres via the MusicBrainz API.
    - Searches for the artist by name, takes the best match
    - Fetches their tags (MusicBrainz's term for genres)
    - Filters to tags with vote count >= 1 and returns top 5
    - Respects MusicBrainz rate limit: 1 request/second
    - No API key required
    """
    try:
        headers = {"User-Agent": "SpotifyToSheetsSync/1.0 (personal-use)"}

        # Step 1: Search for the artist
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

        # Pick best match: highest score
        best = max(artists, key=lambda a: int(a.get("score", 0)))
        artist_id = best.get("id")
        if not artist_id:
            return [], 0.0, "mb_no_id"

        # Step 2: Fetch artist detail with tags
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

        # Filter tags with count >= 1, sort by count descending, take top 5
        valid_tags = [t for t in tags if t.get("count", 0) >= 1]
        valid_tags.sort(key=lambda t: t.get("count", 0), reverse=True)
        top_tags = [t["name"] for t in valid_tags[:5] if t.get("name")]

        normalized = [simple_normalize_genre(g) for g in top_tags]
        normalized = [g for g in normalized if g]

        if not normalized:
            return [], 0.0, "mb_empty_after_normalize"

        confidence = 0.85 if valid_tags[0].get("count", 0) >= 3 else 0.7
        return normalized, confidence, "musicbrainz"

    except Exception as e:
        return [], 0.0, f"exception_{str(e)[:40]}"


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
        bga_rows = []
        for _, r in artists_raw_df.iterrows():
            genres = (r.get("final_genres") or "").split(",")
            for g in genres:
                g = g.strip()
                if not g:
                    continue
                bga_rows.append(
                    {
                        "genre": g,
                        "artist_name": r.get("artist_name"),
                        "artist_url": r.get("artist_url"),
                        "artist_image_url": r.get("artist_image_url", ""),
                        "followers": r.get("followers"),
                        "popularity": r.get("popularity"),
                    }
                )
        bga = pd.DataFrame(bga_rows)
        if not bga.empty:
            bga["artist_link"] = bga.apply(
                lambda r: f'=HYPERLINK("{r.get("artist_url","")}","{r.get("artist_name","")}")' if r.get("artist_url") else r.get("artist_name", ""),
                axis=1,
            )
            bga = bga[["genre", "artist_link", "artist_name", "artist_image_url", "artist_url", "followers", "popularity"]]
            bga = bga.sort_values(["genre", "artist_name"])
    else:
        bga = pd.DataFrame()
    ws_bga = get_or_create_worksheet(sh, tab_names["by_genre_artists"])
    clear_and_set_dataframe(ws_bga, bga)

    # By_Genre_Albums
    if not albums_raw_df.empty:
        bgal_rows = []
        for _, r in albums_raw_df.iterrows():
            genres = (r.get("final_genres") or "").split(",")
            for g in genres:
                g = g.strip()
                if not g:
                    continue
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

    new_albums_df = build_albums_dataframe(saved_albums)
    new_tracks_df = build_tracks_dataframe(saved_tracks, artist_details_map)

    # Add artist_url to tracks & albums via artist_details_map
    if not new_tracks_df.empty:
        new_tracks_df["artist_url"] = new_tracks_df["primary_artist_id"].map(
            lambda aid: safe_get(artist_details_map.get(aid, {}), "external_urls", "spotify", default="")
        )
    if not new_albums_df.empty:
        new_albums_df["artist_url"] = new_albums_df["primary_artist_id"].map(
            lambda aid: safe_get(artist_details_map.get(aid, {}), "external_urls", "spotify", default="")
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
