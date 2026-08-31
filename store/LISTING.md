# Chrome Web Store listing — Nexora Sync

Everything the submission form asks for. Copy the fields straight across.

---

## Item name

```
Nexora Sync
```

## Summary (132 characters max — this one is 108)

```
Bring your Brightspace courses, deadlines and lectures into Nexora. Works with any university's Brightspace.
```

## Category

`Education`

## Language

`English (United Kingdom)`

---

## Description

```
Nexora Sync brings everything from your university's Brightspace into one place,
so you stop living in twelve browser tabs.

Press it once and your courses, deadlines, assignments, quizzes and announcements
appear in Nexora — grouped by term, sorted by what is due next.

WORKS AT ANY UNIVERSITY
Most universities host Brightspace on their own domain. Nexora Sync recognises
Brightspace by the page itself, not by a list of schools, so it works wherever
yours lives. You grant access to your own university's address and nothing else.

NOTHING TO SET UP
No keys to copy, no addresses to type. Press Connect, and Nexora links itself to
your account. Then press Import my courses. That is the whole setup.

WHAT IT BRINGS ACROSS
· Courses, with their real names, terms and images
· Assignments, quizzes and every due date
· Announcements and course updates
· Lecture and outline text — only for the file you ask Nexora to work on

WHAT IT DOES WITH IT
Inside Nexora, the same material turns into things you can actually use:
a grade calculator built from your course outline's real weighting, summaries,
flashcards, practice quizzes and study guides generated from your own lectures,
and a calendar of every deadline in one view.

YOUR PASSWORD IS NEVER INVOLVED
Nexora Sync reads Brightspace through the session you are already signed into.
It never asks for, sees, or stores your university password. It only reads —
it never submits work, posts, or changes anything in your LMS.

Nexora is free while you study.
```

---

## Privacy

**Single purpose**

```
Nexora Sync imports a student's own course material from their university's
Brightspace into their Nexora account, so that their coursework, deadlines and
lecture content are available in one place.
```

**Permission justifications**

| Permission | Justification to paste |
|---|---|
| `storage` | Stores the student's Nexora address, their sync preference, and the key linking the extension to their account. No browsing data is stored. |
| `tabs` | Locates the Brightspace tab the student already has open, so the extension can read the course pages they asked to import. |
| `scripting` | Runs the reader on that Brightspace page when the student presses Import. Injection is programmatic and only on a page the student granted. |
| `alarms` | Schedules the optional background refresh, if the student turns auto-sync on. |
| `optional_host_permissions` (`https://*/*`) | Universities host Brightspace on their own domains, so the set cannot be known in advance. Nothing is granted at install: the student grants one origin, their own university's, from the popup. |
| Remote code | None. All code is in the package; nothing is fetched or evaluated at runtime. |

**Data usage disclosures — tick these**

- [x] Personally identifiable information — *name and email, to identify the student's own account*
- [x] Website content — *course pages, deadlines and lecture text from the student's own LMS*
- [ ] Health, financial, authentication, personal communications, location, user activity

Then affirm all three certifications: data is not sold, not used for unrelated
purposes, and not used to determine creditworthiness.

**Privacy policy URL**

```
https://uni-helper-project.onrender.com/privacy
```

## Additional fields

| Field | Value |
|---|---|
| Official URL | leave **None** — it only lists sites verified in Search Console |
| Homepage URL | `https://uni-helper-project.onrender.com` |
| Support URL | `https://uni-helper-project.onrender.com/support` |
| Mature content | off |

---

## Screenshots (1280×800, in `store/screenshots/`)

| File | Shows |
|---|---|
| `1-connect-in-two-clicks.png` | The popup on first run |
| `2-works-at-any-university.png` | The popup connected to a university |
| `3-everything-in-one-pass.png` | The popup after a sync |
| `4-your-semester-in-one-place.png` | The Nexora dashboard after a sync |
| `5-every-course-organised.png` | A course workspace with imported material |

Also available: `promo-small-440x280.png` and `promo-marquee-1400x560.png`.

## Store icon

`store/icon128.png` — 128×128.

---

## Submitting

1. Register once at https://chrome.google.com/webstore/devconsole — $5, one time.
2. **Add new item**, upload `store/nexora-sync.zip`.
3. Fill in the fields above, upload the three screenshots and the icon.
4. Submit. First review usually takes a few days.

Bump `version` in `extension/manifest.json` for every future upload — the store
rejects a re-upload with a version it has already seen.
