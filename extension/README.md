# Nexora Sync 1.1

Production-hardening update for Smart Sync and Course Intelligence Bridge.

- Automatically detects and injects the Brightspace content script when Chrome did not load it.
- Retries messaging safely instead of failing with “Receiving end does not exist”.
- Background Smart Sync continues through the signed-in Brightspace browser session without requiring an open Brightspace tab after initial course detection.
- Improved lecture classification for titles such as L18, Lec-18, Week 4, Unit 3, and similar naming schemes.
- On-demand PDF/PPTX/DOCX reading remains temporary; only extracted text is cached in Nexora.
- Maximum temporary lecture file size: 30 MB.

After updating the unpacked extension, refresh Brightspace once, run Detect & clean courses, then Scan every course now.
