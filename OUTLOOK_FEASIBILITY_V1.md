# Outlook feasibility test

One question, nothing more: **can a Carleton student grant Nexora read access
to their own mail, or does the tenant answer "Need admin approval"?**

The answer decides whether Phase 4 is a Microsoft sign-in or an extension
scrape, and those are completely different builds. Worth twenty minutes before
designing either.

---

## Part 1 — Register the app in Entra

Go to **entra.microsoft.com** and sign in. Use a **personal** Microsoft account
if you have one, not your Carleton account: a student account usually cannot
register applications, and if yours can, the app lands in Carleton's tenant
where IT can see and remove it. Either works for the test; a personal tenant is
cleaner.

**1. Create the registration**

- Left rail → **Applications** → **App registrations** → **+ New registration**
- **Name:** `Nexora Outlook Test`
- **Supported account types:** *Accounts in any organizational directory
  (Any Microsoft Entra ID tenant — Multitenant)*
  - This is the one that matters. Single-tenant would only ever admit your own
    tenant, and the whole question is about Carleton's.
  - Do **not** pick the "...and personal Microsoft accounts" variant. A
    university is an organizational directory; admitting Outlook.com accounts
    widens the test for nothing.
- **Redirect URI:** platform **Web**, value exactly:

      https://uni-helper-project.onrender.com/outlook-test/callback

  Exactly. A trailing slash or `http` is `AADSTS50011` and will read as
  Carleton blocking you when it is our own typo.
- **Register**

**2. Copy the client id**

The Overview page shows **Application (client) ID**. That is `MS_CLIENT_ID`.

**3. Create the secret**

- **Certificates & secrets** → **Client secrets** → **+ New client secret**
- Description `nexora-outlook-test`, expiry **3 months** (this is a test)
- **Add**, then copy the **Value** column — *not* the Secret ID.
  - The Value is shown once and is masked for ever after. If you miss it,
    delete the secret and make another.
  - Copying the Secret ID instead is `AADSTS7000215`, and the page will say so.

**4. Add the permissions**

- **API permissions** → **+ Add a permission** → **Microsoft Graph** →
  **Delegated permissions**
- Tick exactly these three:
  - `User.Read` — who signed in
  - `Mail.Read` — read mail
  - `offline_access` — a refresh token
- **Add permissions**
- **Do not press "Grant admin consent"** on your own tenant. Consenting as an
  admin in *your* tenant tells you nothing about *Carleton's*, and pressing it
  is the single easiest way to get a false pass on this test.

There is no send permission on that list and there must not be. Phase 4 reads.

**5. Set the Render environment variables**

Render → the `uni-helper-project` service → **Environment**:

| Key | Value |
|---|---|
| `OUTLOOK_TEST_ENABLED` | `1` |
| `MS_CLIENT_ID` | the Application (client) ID |
| `MS_CLIENT_SECRET` | the secret **Value** |
| `MS_REDIRECT_URI` | `https://uni-helper-project.onrender.com/outlook-test/callback` |

Save; Render redeploys. **Set `OUTLOOK_TEST_ENABLED` back to `0` when you are
done** — that is the switch, and everything else stays harmless without it.

---

## Part 2 — Run the test

1. Sign in to Nexora **as your admin account**. No other account can see the
   page; it redirects to the dashboard.
2. Go to **https://uni-helper-project.onrender.com/outlook-test**
3. Press **Connect Outlook**.
4. Sign in with your **Carleton** account — this is the part being tested.
5. **Read the consent screen before clicking anything.** That screen is the
   result.

### What you will see, and what it means

| What appears | What it means | What Phase 4 becomes |
|---|---|---|
| A consent list with an **Accept** you can press yourself | Carleton allows user consent | **Microsoft sign-in.** Clean OAuth, works with the browser closed, no extension involved |
| **"Need admin approval"** / `AADSTS90094` | Carleton blocks user consent | Microsoft sign-in needs Carleton IT to approve the app. Without that, the extension fallback below |
| `AADSTS65001` | No consent on record | Usually the same answer as above, but try once more first — a dismissed consent screen looks identical |
| `AADSTS50011` | Redirect URI mismatch | **Ours, not Carleton's.** The Entra URI is not exactly the one above |
| `AADSTS7000215` | Invalid client secret | **Ours.** You copied the Secret ID instead of the Value |
| Your name and five subject lines | It worked | Best case, and you can stop reading |

If it works you will see your display name, the scopes Microsoft actually
granted, and five subjects with senders. **Nothing is stored** — no token, no
message, not in the database and not in the session. Reload the page and it is
gone.

Whatever Microsoft says is printed **verbatim**, with the `AADSTS` code pulled
out and a sentence saying what that code means for the build. A paraphrased
permission error would make the whole exercise worthless.

---

## Part 3 — How it is built, and what it deliberately does not do

- **Off by default.** `OUTLOOK_TEST_ENABLED` is `0` unless Render says
  otherwise. The route exists in production right now and answers "this test is
  off" to the only account that can reach it.
- **Admin only.** All three routes check `ADMIN_EMAIL` before anything else.
- **No token is stored anywhere, not even the session.** Flask's session is a
  signed **cookie** and browsers drop one over ~4 KB; a Graph access token is
  1–2 KB and a refresh token more, so keeping them there would break the
  session silently rather than fail loudly. The callback exchanges the code,
  calls Graph, renders, and lets the token fall out of scope. Stricter than
  "session only", and it avoids the bug.
- **No email content reaches the database.** There is no model that could hold
  it, which is the cheapest way to keep that promise.
- **The secret is never rendered, logged, or put in an error message.**
- **`state` is checked on the callback**, so the exchange cannot be driven by
  anyone who can make your browser issue a GET.
- **Read scopes only.** `User.Read`, `Mail.Read`. No send, now or by accident.

Two things found while building it, both of which would have looked like
Carleton blocking us:

- **MSAL rejects `offline_access`.** Passing it — which is the obvious reading
  of the requirement — raises `ValueError: You cannot use any scope value that
  is reserved` *before any network call*. MSAL adds `offline_access`, `openid`
  and `profile` itself; the authorization URL it builds carries all three.
  Verified rather than assumed.
- **`msal` pulls three packages this app did not have** — `requests`, `PyJWT`,
  `idna`. All four are pinned, because `requirements.txt` opens with an essay
  about an outage caused by an unpinned dependency, and `app.py` imports `msal`
  in a `try/except` so a missing wheel degrades this one route instead of
  taking every worker down at import. Delete all four if the test is abandoned.

---

## Part 4 — Fallback, if Microsoft sign-in is blocked

**Plan only. Nothing here is built.**

If `AADSTS90094` comes back and Carleton IT will not approve the app, the
pattern that already works for Brightspace applies: the student is signed in to
Outlook Web in their own browser, so the extension can read what they can read.

**Shape**, mirroring the Brightspace sync exactly:

- The extension requests `outlook.office.com` as an **optional** permission,
  granted by the student for their own origin — the same programmatic
  `chrome.scripting.executeScript` injection, not a static content script, and
  the same reason: it must work without us naming a domain up front.
- Outlook Web has its own internal API (`/owa/service.svc`, and Graph itself
  with the session's token). **Prefer Graph with the page's own token** over
  scraping the DOM: Outlook's UI is a React app that changes constantly, and
  the Brightspace lesson was that scraping the rendered page returns whatever
  happened to be mounted (the My Courses widget term tab).
- The extension is **transport only**. Every line that interprets a mail
  payload lives on the server, once — the rule the Canvas integration already
  follows. No parsing in JavaScript.
- It posts to a new `/api/outlook/ingest`, authenticated with the same
  extension bearer token as `/api/sync/content`, checking `_entitlement`
  inline rather than via the decorator because it is a token, not a session.

**What makes this harder than Brightspace, and would need deciding first:**

1. **Which mail is course-related.** Brightspace items arrive already attached
   to a course; email does not. A classifier on sender domain, subject codes
   and course names would be a `classify.js`-shaped problem, and the lecture
   mis-filing we just finished fixing is a fair warning about how that goes.
   Getting it wrong here is worse: a missed email is a missed deadline.
2. **Privacy.** Brightspace content is coursework. A mailbox is not. Reading
   it needs a far more explicit consent moment than the extension's current
   one, a strict scope on what is stored, and probably storing only what was
   extracted (a date, a course, a subject line) rather than any body text.
   `_CONTENT_TEXT_CAP` is not the right instinct here; "store almost nothing"
   is.
3. **Conditional Access.** Carleton may require MFA or a managed device for
   OWA sessions. That does not stop the extension, which rides an existing
   session, but it does mean the student must be actively signed in — so this
   path can never sync with the browser closed, unlike the Canvas token path.
4. **Terms.** Scraping Brightspace is defensible because there is no
   student-facing API. Outlook **has** one, and we would be going around it
   because the university declined to approve our app. That is a different
   argument, and worth being sure of before building.

My read: if Microsoft sign-in is blocked, the honest next step is **asking
Carleton IT to approve a read-only app**, not routing around them. The
extension fallback is real and buildable, but it is the second choice, and
point 4 is why.
