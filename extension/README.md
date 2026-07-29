# Nexora Sync v0.4

This developer build is for Carleton Brightspace testing.

## What it does
- Detects and cleans Carleton course names, codes, terms, IDs, URLs and images.
- Imports/updates matching Nexora course workspaces.
- Opens temporary inactive tabs for each imported course.
- Maps visible Content, Assignments, Quizzes and Announcements links.
- Sends the content map to Nexora with deduplication.
- Runs on a 15/30/60-minute Chrome alarm when auto-sync is enabled.

## Current limitation
This version maps links and visible items. It does not yet upload the actual PDF/PPTX file bytes. The next version will add authenticated file transfer after this content map is verified against real Carleton courses.
