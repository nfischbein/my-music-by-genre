# My Music by Genre — Setup Guide

*A step-by-step guide for setting up your own personal Spotify library browser. No coding experience required — if you can follow instructions and copy-paste text, you can do this.*

---

## What you're building

A private web app that lives at your own GitHub Pages URL (free). It reads your Spotify library — your saved artists, albums, and recently played tracks — enriches them with genre data, and displays everything in a beautiful dark-themed mobile app you can install on your iPhone home screen.

Your data lives in a Google Sheet that only you control. Nothing goes through any third-party server.

**Time to set up: about 60–90 minutes the first time.**

---

## What you'll need

- A Mac or PC with internet access
- A free [Spotify](https://spotify.com) account
- A free [Google](https://google.com) account
- A free [GitHub](https://github.com) account
- A free [Last.fm](https://last.fm) account (optional but recommended for better genre data)
- Python installed on your computer (instructions below)

---

## Part 1 — Install the tools on your Mac

### Step 1.1 — Install Xcode Command Line Tools

Git and Python need this to work on a Mac. Open **Terminal** (search for it in Spotlight with ⌘+Space) and run:

```bash
xcode-select --install
```

A dialog box will pop up. Click **Install** (not "Get Xcode"). It takes a few minutes. When it finishes, continue.

### Step 1.2 — Install Python

Go to [python.org/downloads](https://www.python.org/downloads/) and download the latest Python 3 installer for Mac. Run it and follow the prompts.

Verify it worked by running this in Terminal:

```bash
python3 --version
```

You should see something like `Python 3.12.0`.

### Step 1.3 — Install the Python libraries

In Terminal, run:

```bash
pip3 install spotipy gspread google-auth pandas requests python-dotenv
```

This installs all the libraries the sync script needs. It may take a minute.

---

## Part 2 — Fork the repo and set up GitHub Pages

### Step 2.1 — Fork the repo

1. Go to [github.com/nfischbein/my-music-by-genre](https://github.com/nfischbein/my-music-by-genre)
2. Click **Fork** in the top right
3. Click **Create fork**

You now have your own copy of the project at `github.com/YOUR-USERNAME/my-music-by-genre`.

### Step 2.2 — Enable GitHub Pages

1. In your forked repo, click **Settings**
2. Click **Pages** in the left sidebar
3. Under "Branch", select **main** and click **Save**

After about a minute, your app will be live at:
`https://YOUR-USERNAME.github.io/my-music-by-genre`

### Step 2.3 — Clone the repo to your Mac

In Terminal:

```bash
cd ~
git clone https://github.com/YOUR-USERNAME/my-music-by-genre.git
cd my-music-by-genre
```

---

## Part 3 — Create a Google Sheet

### Step 3.1 — Create the sheet

1. Go to [sheets.google.com](https://sheets.google.com) and create a new blank spreadsheet
2. Name it **Spotify Personal Library**
3. Look at the URL — it will look like:
   `https://docs.google.com/spreadsheets/d/ABC123XYZ/edit`
4. Copy the long string between `/d/` and `/edit` — that's your **Sheet ID**. Save it somewhere.

### Step 3.2 — Make the sheet public (read-only)

1. Click **Share** in the top right of your sheet
2. Under "General access", change it to **Anyone with the link**
3. Make sure the role is set to **Viewer**
4. Click **Done**

This allows the web app to read your data. Nobody can edit it.

### Step 3.3 — Set up a Google Cloud service account

This is what allows the Python script to *write* to your sheet. It sounds technical but just involves clicking through a few screens.

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click **Select a project** → **New Project** → name it anything → **Create**
3. In the search bar at the top, search for **Google Sheets API** → click it → click **Enable**
4. Search for **Google Drive API** → click it → click **Enable**
5. In the left sidebar, go to **IAM & Admin → Service Accounts**
6. Click **Create Service Account**
7. Give it any name (e.g. "spotify-sync") → click **Create and Continue** → click **Done**
8. Click on the service account you just created
9. Go to the **Keys** tab → **Add Key → Create new key → JSON → Create**
10. A JSON file will download to your Mac. Move it to your `my-music-by-genre` folder and rename it `service-account.json`

### Step 3.4 — Share your sheet with the service account

1. Open the JSON file in a text editor (right-click → Open With → TextEdit)
2. Find the line that says `"client_email"` — copy that email address (it ends in `.gserviceaccount.com`)
3. Go back to your Google Sheet → **Share**
4. Paste that email address and set the role to **Editor** → **Send**

---

## Part 4 — Set up Spotify

### Step 4.1 — Create a Spotify developer app

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Log in with your Spotify account
3. Click **Create app**
4. Fill in any name and description
5. Set **Redirect URI** to: `http://localhost:8888/callback`
6. Check the boxes for **Web API** and **Web Playback SDK**
7. Click **Save**
8. Click on your new app → **Settings**
9. Copy your **Client ID** and **Client Secret** — save them somewhere

### Step 4.2 — Set up Last.fm (optional but recommended)

Last.fm improves genre data for artists that Spotify doesn't tag well.

1. Create a free account at [last.fm](https://www.last.fm)
2. Go to [last.fm/api/account/create](https://www.last.fm/api/account/create)
3. Fill in the form (app name can be anything, like "my music sync")
4. Copy your **API key** — save it somewhere

---

## Part 5 — Configure the script

### Step 5.1 — Create your .env file

In your `my-music-by-genre` folder, create a file called `.env` (no extension). The easiest way:

```bash
cd ~/my-music-by-genre
cp .env.example .env
```

Now open `.env` in a text editor and fill in your values:

```
SPOTIPY_CLIENT_ID=paste_your_spotify_client_id_here
SPOTIPY_CLIENT_SECRET=paste_your_spotify_client_secret_here
SPOTIPY_REDIRECT_URI=http://localhost:8888/callback
LASTFM_API_KEY=paste_your_lastfm_api_key_here
GOOGLE_SERVICE_ACCOUNT_JSON=service-account.json
```

Save the file.

### Step 5.2 — Add your Sheet ID to the script

Open `spotify_to_sheets.py` in a text editor. Find this line near the top (around line 60):

```python
"sheet_id": "1BQVKH85sVqk-jezdbyvz6wkzc87uz6OAdl4N5FiXAxI",
```

Replace the long string with your own Sheet ID from Step 3.1.

---

## Part 6 — Run the sync for the first time

In Terminal, from your `my-music-by-genre` folder:

```bash
python3 spotify_to_sheets.py --initial-load
```

The first time you run this, a browser window will open asking you to log in to Spotify and grant permission. Do that, then come back to Terminal.

The script will then:
- Pull all your saved artists, albums, and tracks from Spotify
- Look up genre data from MusicBrainz and Last.fm
- Write everything to your Google Sheet

This first run takes **10–30 minutes** depending on the size of your library, because it's fetching genre data for every artist. Subsequent runs are much faster because results are cached.

You'll see progress in Terminal as it runs. When it says `Sync complete.` you're done.

---

## Part 7 — Connect the app to your sheet

1. Open your app at `https://YOUR-USERNAME.github.io/my-music-by-genre`
2. Tap the **gear icon** in the top right
3. Paste your **Sheet ID** and tap **Connect**

Your library should appear. If it doesn't, wait 30 seconds and refresh — the sheet may still be updating.

---

## Part 8 — Install it on your iPhone

1. Open the app in Safari on your iPhone
2. Tap the **Share** button (the box with an arrow pointing up)
3. Scroll down and tap **Add to Home Screen**
4. Tap **Add**

The app now lives on your home screen and opens full-screen like a native app.

---

## Part 9 — Set up daily auto-sync (optional)

This makes the script run automatically every day so your library stays up to date without you doing anything.

### Step 9.1 — Get your Spotify refresh token

The automated sync can't open a browser to log in, so we need to give it a long-lived token. Run this once in Terminal:

```bash
cd ~/my-music-by-genre
python3 - <<'EOF'
import os
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()
auth = SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
    scope="user-library-read user-follow-read user-read-recently-played",
    cache_path=".spotify_token_cache.json",
    open_browser=True
)
token = auth.get_access_token(as_dict=True)
print("Refresh token:", token.get("refresh_token"))
EOF
```

A browser window will open. Log in and approve. Copy the refresh token that prints in Terminal.

### Step 9.2 — Add secrets to GitHub

1. Go to your GitHub repo → **Settings → Secrets and variables → Actions**
2. Click **New repository secret** for each of these:

| Secret name | Value |
|-------------|-------|
| `SPOTIPY_CLIENT_ID` | Your Spotify client ID |
| `SPOTIPY_CLIENT_SECRET` | Your Spotify client secret |
| `SPOTIPY_REFRESH_TOKEN` | The refresh token from Step 9.1 |
| `LASTFM_API_KEY` | Your Last.fm API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The *entire contents* of your `service-account.json` file (open it in TextEdit, select all, copy) |
| `GOOGLE_SHEET_ID` | Your Google Sheet ID |

### Step 9.3 — Enable the workflow

The workflow file is already in your repo at `.github/workflows/sync.yml`. It runs every day at 3 AM UTC (10 PM EST).

To trigger it manually at any time:
1. Go to your GitHub repo → **Actions**
2. Click **Daily Spotify Sync** in the left sidebar
3. Click **Run workflow → Run workflow**

---

## Troubleshooting

**The app shows "Loading your music…" forever**
- Check that your Sheet ID is correct in the gear menu
- Make sure the sheet is shared as "Anyone with the link → Viewer"
- Open your sheet in a browser and confirm the `By_Genre_Artists` tab exists

**The script fails with an authentication error**
- Double-check your `.env` file has no extra spaces or quotes around the values
- Make sure the service account email is shared on the sheet as Editor

**Genre data looks wrong or missing**
- Run `python3 spotify_to_sheets.py --initial-load` again — genre enrichment improves over multiple runs as the cache fills in
- Artists with very obscure names may not be found by MusicBrainz or Last.fm

**The GitHub Actions sync is failing**
- Go to your repo → Actions → click the failed run to see the error log
- The most common cause is an expired Spotify refresh token — repeat Step 9.1 and update the `SPOTIPY_REFRESH_TOKEN` secret

---

## Running the sync manually

Any time you want to refresh your data outside the daily schedule:

```bash
cd ~/my-music-by-genre
python3 spotify_to_sheets.py --update
```

This is much faster than the initial load because genre data is cached.

---

## Customizing your genre buckets

The default setup gives you 22 genre buckets (Rock, Classic Rock, Blues, Folk, Hip Hop, Jazz, etc.). These work well for most libraries but you might want to add, remove, or rename buckets to match your taste.

**v1.0:** Buckets are defined in `spotify_to_sheets.py` in the `GENRE_BUCKET_MAP` section. You can edit the list directly — each line is a `("micro_genre", "Bucket Name")` pair. Add new pairs, change the bucket name on existing ones, or remove pairs you don't want. Then re-run `python3 spotify_to_sheets.py --update` to apply the changes.

**Coming in v1.1:** A `Genre_Buckets` tab in your Google Sheet and a `--map-genres` interactive mode that walks you through assigning any unmapped genres in your library — no Python editing required.

---

*Built by Neil Fischbein. Fork it and make it your own.*
