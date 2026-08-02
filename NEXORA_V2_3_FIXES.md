# Nexora v2.3 — Course deletion and outline-driven Grade Intelligence

## Course deletion
- "Remove from Nexora" now creates a persistent per-user tombstone.
- It deletes the course card, imported content, outline profile, grade entries, study artifacts, cached files, course updates, and related Nexora tasks.
- Smart Sync ignores tombstoned courses so they do not return on the next scan.
- Brightspace data is never modified.

## Course outline flow
1. Smart Sync attempts to detect a course outline.
2. If a readable outline is found, Nexora automatically analyzes it once.
3. If none is found, the Course Outline tab explicitly says detection failed and asks for a manual upload.
4. Manual upload supports PDF, PPTX, DOCX, TXT, MD, CSV, and HTML.
5. After upload, Gemini analyzes the complete extracted text; a deterministic parser is used if Gemini fails or returns an implausible grading scheme.
6. Grade Intelligence is generated from the detected components and pass rules, then the page moves directly to the Grade Intelligence tab.

## Grade Intelligence
- Exact component cards are created from the outline (for example Assignments, Labs, Midterm, Final Exam).
- Students enter only their marks and maxima.
- The official weights remain visible and editable for review.
- Nexora calculates current standing, best/worst possible grades, needed average, exact final-exam requirement, and special final-pass rules.
