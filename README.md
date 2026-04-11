# My Music by Genre

A personal Spotify library browser — mobile PWA + Python sync script.

Browse your saved artists and albums by genre, drill into any genre, tap an artist to see their saved albums, and jump straight into Spotify. Syncs automatically via GitHub Actions daily.

**Live app:** [nfischbein.github.io/my-music-by-genre](https://nfischbein.github.io/my-music-by-genre)

---

## How it works

```
Spotify API
    ↓
spotify_to_sheets.py   ← pulls saved artists, albums, tracks
    ↓                     enriches genres via MusicBrainz + Last.fm
Google Sheet           ← public read-only data store
    ↓
index.html             ← PWA reads the sheet, renders the UI
```

No backend. No database. The Google Sheet is the data layer — the PWA reads it directly via the public CSV export endpoint.

---

## Features

- **Artists / Albums / Genres / Recent** tabs
- **22 genre buckets** — Classic Rock, Blues, Folk, Hip Hop, Jazz, R&B / Soul, Neo Soul, Funk, and more
- **Artist inline expand** — tap any artist to see their saved albums, tap an album to open in Spotify
- **Genre drill-down** — tap any genre to browse its artists and albums
- **Grid and list view** toggle
- **Sort** by Newest Added, A–Z, or Popularity (artists)
- **Genre filter** modal with search
- **Recently Added** and **Recently Played** sub-tabs
- Dark theme, iPhone mini optimized, installable as home screen PWA

---

## Repository structure

```
my-music-by-genre/
├── index.html                  # The entire PWA (single file)
├── spotify_to_sheets.py        # Sync script
├── .env.example                # Credential template
├── .gitignore
└── .github/
    └── workflows/
        └── sync.yml            # Daily GitHub Actions sync
```

---

## Setup

See **[My Music by Genre — Setup Guide](SETUP.md)** for step-by-step instructions aimed at non-developers.

For the technically inclined, the short version:

### Prerequisites

- Python 3.9+
- A Spotify account
- A Google account
- A GitHub account (for auto-sync)

### 1. Clone and install dependencies

```bash
git clone https://github.com/nfischbein/my-music-by-genre.git
cd my-music-by-genre
pip install spotipy gspread google-auth pandas requests python-dotenv
```

### 2. Create credentials

**Spotify:**
1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Create an app, set redirect URI to `http://localhost:8888/callback`
3. Copy Client ID and Client Secret

**Google Sheets:**
1. Create a Google Sheet and note its ID (the string between `/d/` and `/edit` in the URL)
2. In [Google Cloud Console](https://console.cloud.google.com), create a project
3. Enable the Google Sheets API and Google Drive API
4. Create a Service Account, download the JSON key
5. Share your Google Sheet with the service account email as Editor

**Last.fm (optional):**
1. Create a free account at [last.fm](https://www.last.fm)
2. Get an API key at [last.fm/api/account/create](https://www.last.fm/api/account/create)

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

Update `DEFAULT_CONFIG` in `spotify_to_sheets.py`:
```python
"sheet_id": "your-google-sheet-id-here",
```

### 4. Run initial sync

```bash
python spotify_to_sheets.py --initial-load
```

A browser window will open for Spotify OAuth on first run. After that the token is cached.

### 5. Make the sheet public

In Google Sheets: **Share → Anyone with the link → Viewer**

### 6. Deploy the PWA

Enable GitHub Pages on your fork (Settings → Pages → Deploy from branch → main). The app will be live at `yourusername.github.io/my-music-by-genre`.

Open the app, tap the gear icon, and enter your Sheet ID.

---

## Running the sync script

```bash
# Full re-sync (default)
python spotify_to_sheets.py --update

# First run / rebuild everything
python spotify_to_sheets.py --initial-load

# Preview changes without writing
python spotify_to_sheets.py --dry-run

# Rebuild derived tabs only (fast, no Spotify API calls)
python spotify_to_sheets.py --rebuild-derived
```

Checkpoints are saved locally so interrupted runs can resume without re-fetching everything from Spotify.

---

## Genre system

Genres are enriched in three passes:

1. **Spotify** — artist genre tags from the Spotify API
2. **MusicBrainz + Last.fm** — web lookup for artists with no Spotify genres
3. **Consolidation** — 200+ micro-genre tags mapped to 22 canonical buckets

### The 22 buckets

| # | Bucket | # | Bucket |
|---|--------|---|--------|
| 1 | Rock | 12 | Neo Soul |
| 2 | Classic Rock | 13 | Funk |
| 3 | Blues | 14 | Electronic |
| 4 | Acoustic Blues | 15 | Pop |
| 5 | Folk | 16 | Reggae |
| 6 | Country | 17 | Dance |
| 7 | Americana | 18 | Experimental |
| 8 | Acoustic | 19 | Latin |
| 9 | Hip Hop | 20 | Classical |
| 10 | Metal | 21 | World |
| 11 | Jazz | 22 | R&B / Soul |

Micro-genre tags are preserved in the raw data — consolidation adds bucket labels alongside them, it doesn't replace them.

---

## Auto-sync via GitHub Actions

The included workflow (`.github/workflows/sync.yml`) runs the sync script daily at 3 AM UTC. It requires the following repository secrets:

| Secret | Value |
|--------|-------|
| `SPOTIPY_CLIENT_ID` | Spotify app client ID |
| `SPOTIPY_CLIENT_SECRET` | Spotify app client secret |
| `SPOTIPY_REFRESH_TOKEN` | Spotify OAuth refresh token |
| `LASTFM_API_KEY` | Last.fm API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of service account JSON |
| `GOOGLE_SHEET_ID` | Your Google Sheet ID |

See the Setup Guide for how to obtain the Spotify refresh token for headless use.

---

## Privacy

- Your Spotify data lives in your own Google Sheet, which you control
- The PWA reads your sheet directly — no data passes through any third-party server
- The sync script runs either locally on your machine or in your own GitHub Actions runner
- Anthropic's Claude products are ad-free and do not share your data with advertisers

---

## Roadmap

### v1.1 — Custom genre buckets

Currently the 22 genre buckets are hardcoded in the script. Two features planned for v1.1 make this fully customizable without editing Python:

**`Genre_Buckets` sheet tab**
On first run the script will write the default bucket map to a dedicated tab in your Google Sheet — two columns: `micro_genre` and `bucket`. You'll be able to edit it freely: rename buckets, merge them, split them, add new ones. The script will read your customized map on every subsequent sync.

**`--map-genres` interactive setup mode**
A guided command-line walkthrough that finds any genre tags in your library that don't map to a bucket yet, and asks you to assign each one. Results are written back to the `Genre_Buckets` tab. Useful on first setup and after adding a lot of new music.

Together these mean friends who fork the repo can build their own bucket system — "Prog Rock" as its own bucket, "Neo Soul" merged into "R&B / Soul", no "Classical" bucket at all — without touching any code.

---

## License

Personal use. Fork freely for your own library.
