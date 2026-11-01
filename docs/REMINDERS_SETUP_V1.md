# Switching reminders on

A student can create a reminder the moment this ships. **Nothing will notify
them until the two settings below exist**, and the add sheet tells them so
rather than letting them trust a notification that cannot arrive.

The reason is in the architecture, not in the feature: Nexora is one gunicorn
web service, so code runs when a request arrives and at no other time. Nothing
in the process can wake up at nine in the morning. Something outside has to
knock.

## What has to be true

| | Without it |
|---|---|
| `SMTP_HOST`, `SMTP_FROM` (+ `SMTP_USER`, `SMTP_PASSWORD`) | The dispatcher runs and sends nothing. Its `blocked` count says how many reminders came due with no way to deliver them. |
| `REMINDER_DISPATCH_TOKEN` | `/api/reminders/dispatch` is a 404, so no cron can ask for anything to be sent. |
| An external cron calling that route | Both settings can be perfect and nothing fires. **Nothing in Nexora can detect that the cron stopped.** |

`/admin/health` reports all three states, and the same finding is logged at
startup.

## 1. SMTP

Gmail with an app password is free and plenty at student volume. This is the
same configuration password reset has been waiting on, so it fixes two things:
until it exists, a student who forgets their password is locked out
permanently.

On Render → the web service → Environment:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=<the gmail address>
SMTP_PASSWORD=<the 16-character app password, not the account password>
SMTP_FROM=<the gmail address>
SMTP_USE_TLS=true
```

## 2. A dispatch token

Any long random string. Generate one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set it as `REMINDER_DISPATCH_TOKEN`. It is the entire credential for that route
— the same arrangement as the `.ics` feed token, because the caller cannot sign
in — so treat it like a password and rotate it by changing the variable.

## 3. Something to call it

Free, no account needed beyond a sign-up: [cron-job.org](https://cron-job.org).

- URL: `https://uni-helper-project.onrender.com/api/reminders/dispatch`
- Method: **POST** (not GET — a prefetch must not spend it)
- Header: `X-Nexora-Token: <the token>`
- Every **5 minutes**

Five minutes is the resolution of a reminder: one set for 09:00 arrives by
09:05. Calling it oftener is harmless — the dispatcher is idempotent, and a
reminder is sent exactly once however many callers overlap.

## Checking it works

```bash
curl -s -X POST "https://uni-helper-project.onrender.com/api/reminders/dispatch" \
     -H "X-Nexora-Token: $REMINDER_DISPATCH_TOKEN"
```

```json
{"considered": 3, "sent": 1, "failed": 0, "waiting": 2,
 "blocked": 0, "too_late": 0, "email_configured": true}
```

Every count answers a different question on purpose:

- **`waiting`** — not due yet.
- **`blocked`** — due now, and nothing can send it. This is SMTP missing.
- **`too_late`** — due more than `REMINDER_CATCHUP_HOURS` (12) ago. Deliberately
  never sent and deliberately **never marked as sent**: if the cron has been
  down, fourteen notices of things that already happened is worse than silence,
  and the unsent state is the only record that it was down.
- **`failed`** — tried, and the mail server refused. Retried inside the window,
  no oftener than every 15 minutes.

Folding `blocked` into `waiting` would make "nothing can send these" read as
"nothing was due", which is the mistake `/api/sync/auto` made once already.

## Knobs

| Variable | Default | |
|---|---|---|
| `REMINDER_CATCHUP_HOURS` | 12 | How stale a reminder may be and still be sent |
| `REMINDER_DEFAULT_HOUR` | 9 | When a reminder with no time fires, on the student's clock |

## The second channel, which needs nothing

A reminder also carries a `VALARM` in the `.ics` feed, so a student who has
subscribed Nexora to Google or Apple Calendar gets a native phone notification
with Nexora sending nothing at all.

It is not a substitute for the email. Google refreshes a subscribed feed on its
own schedule, up to once a day, so a reminder set today for tonight may never
reach it in time.
