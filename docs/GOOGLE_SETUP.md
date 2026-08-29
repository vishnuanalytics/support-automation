# Google Docs connector setup (Phase 15)

Lets a tenant link a Google Doc into a KB collection; a background job
(`gdoc_sync.py`) keeps it in sync. All optional — the "Link Google Doc"
button stays hidden until `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` are
set on the API server.

## 1. Create an OAuth client (one, platform-level)

1. <https://console.cloud.google.com> → create/select a project.
2. **APIs & Services → Enabled APIs** → enable **Google Drive API** and
   **Google Docs API**.
3. **APIs & Services → OAuth consent screen**:
   - User type **External** (or Internal if you have a Workspace).
   - Scopes: `.../auth/drive.readonly`, `.../auth/documents.readonly`.
   - While it's in "Testing", add each Google account that will connect as
     a **Test user**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Type **Web application**.
   - **Authorised redirect URI**: exactly the value of `GOOGLE_REDIRECT_URI`
     (default `http://localhost:8000/api/integrations/google/callback`;
     use your deployed API origin in prod).
5. Copy the **Client ID** and **Client secret** into `.env`:

   ```
   GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=xxxx
   GOOGLE_REDIRECT_URI=http://localhost:8000/api/integrations/google/callback
   ```

Restart the API. `GET /api/integrations/google/status` now reports
`configured: true`.

## 2. Connect a tenant

In the web app: **Knowledge** tab → open a collection → **Connect Google**
→ complete the consent popup. The refresh token is stored in
`tenant_integrations (tenant_id, kind='google')` (one connection per
tenant; flagged for Vault encryption — see "Known issues / debt").

## 3. Link a doc

**＋ Google Doc** → paste the doc URL. The doc is exported to markdown,
chunked, embedded, and added to the collection as a **read-only** entry
(`origin='gdoc'`). Edit it in Google, then hit **re-sync** — or let the
cron do it.

## 4. Keep it in sync

```
python -m ingestion.sources.gdoc_sync --once
```

Add it to `.github/workflows/daily-sync.yml` (or its own schedule). It
re-exports only docs whose Drive `modifiedTime` moved since the last sync;
auth failures set `kb_entries.sync_error` and leave the current content.

---

# Google sign-in for the editor (Phase 18d)

Separate from the Docs connector above — this is "Continue with Google" on
the **login page**, handled by Supabase Auth (not our API). The button is
always visible; it just fails with a provider error until the dashboard is
configured.

## 1. OAuth web client (reuse the Phase 15 project or a new one)

**APIs & Services → Credentials →** your OAuth **Web** client:
- **Authorised redirect URIs** — add
  `https://<your-project-ref>.supabase.co/auth/v1/callback`
  (this project: `https://mjohgmivnxfwkqmlojqs.supabase.co/auth/v1/callback`).
- **Authorised JavaScript origins** — add `http://localhost:5173`
  (and your deployed web origin in prod).

## 2. Supabase dashboard

- **Authentication → Providers → Google** → enable; paste the client
  **ID** and **secret**.
- **Authentication → URL Configuration** → Site URL and Additional
  Redirect URLs: `http://localhost:5173` (+ prod origin).

No API restart or `.env` change needed — it's all Supabase-side.

## 3. Access

Signing in with Google only authenticates. A new user still needs a
`tenant_members` row: an owner invites their email in the **Team** tab
(Phase 18c), and `App.tsx`'s `acceptInvitations()` claims it on their
first sign-in. No invite → the "no workspace yet" screen.
