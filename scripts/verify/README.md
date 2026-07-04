# M0 verification scripts

Two throwaway scripts that prove the load-bearing assumptions in
`docs/technical-brief.md` §16 before we write any real service code.

Both are single-file [PEP 723](https://peps.python.org/pep-0723/) scripts —
run with `uv run` and dependencies install automatically into a temporary
environment.

---

## 01 — Drive access from a service account on a personal user's shared folder

Confirms the highest-risk assumption: that a service account can (a) list
files in a folder shared to it by a personal Gmail user and (b) receive
those files' modifications through the `changes.list` feed.

### One-time setup

1. **GCP project + service account**
   - Create a GCP project (personal Google account is fine for the console).
   - Enable the **Google Drive API** on the project.
   - Create a service account (IAM & Admin → Service Accounts → Create).
   - Generate a JSON key for the service account, download it.
   - Note the `client_email` from the JSON.

2. **Share a test folder**
   - In personal Drive, create a scratch folder (or use the real Supernote
     sync folder if you're feeling brave).
   - Share it with the service account's `client_email` as **Viewer**.
   - Grab the folder ID from the URL: `.../drive/folders/<THIS_PART>`.

3. **Env vars**
   ```sh
   export GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/sa.json
   export SOURCE_FOLDER_ID=1a2b3c...
   ```

### Run

```sh
uv run scripts/verify/01_drive_access.py
```

### What it does

1. Lists the files it can see inside `SOURCE_FOLDER_ID`. Pass condition:
   the folder's contents are printed.
2. Fetches a `startPageToken`.
3. Polls `changes.list` every 5 seconds and prints any change entries.
   Now edit / rename / add / delete a file in the shared folder from
   your personal account — a change entry should appear within a few
   seconds.

### Pass criteria

- Files listed in step 1 (proves basic read access).
- **A change entry appears in step 3 for every modification you make**
  (proves the changes feed carries edits made by the owning personal user
  on files shared to the service account). This is the critical result.

### If step 3 fails

The service account's changes feed does not carry personal-user
modifications on shared files. Fallback plan is documented in the
technical brief §7 (poll `files.list` with `modifiedTime` filter).

---

## 02 — sn2md → Gemini 2.5 Pro end-to-end

Confirms that sn2md's Python API produces Markdown from a real `.note`
file using `llm-gemini`.

### One-time setup

1. **Gemini API key** from Google AI Studio (`ai.google.dev`).
2. **A sample `.note` file** — either grab one off your Supernote via USB
   or download an example from the sn2md repo's `docs/` samples.

### Env vars

```sh
export LLM_GEMINI_KEY=your-key
# Model string. Confirmed 2026-07-04 that llm-gemini requires the
# prefixed "gemini/..." form. The script defaults to
# "gemini/gemini-2.5-pro"; override only if trying a different model.
# export SN2MD_MODEL=gemini/gemini-2.5-pro
```

### Run

```sh
uv run scripts/verify/02_sn2md_gemini.py /path/to/sample.note
```

### Pass criteria

- Script exits 0.
- A `.md` file lands in the temp output directory.
- The first 800 chars look like plausibly-transcribed Markdown (not
  error text, not the raw prompt).
- Note the wall-clock time and any messaging about Gemini calls — a
  rough sense of per-page latency informs the queue concurrency default.

---

## After both pass

The technical brief's §16 tasks 1 and 4 are resolved. Delete this
directory (or leave it as a smoke test) and move on to M1.
