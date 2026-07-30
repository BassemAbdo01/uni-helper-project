# Nexora Sync v0.8 — Course Intelligence

This version adds on-demand lecture understanding to Background Smart Sync.

## What it does
- Syncs Brightspace course metadata in the background while Chrome runs.
- Lets Nexora AI locate an imported lecture from a natural-language request.
- Temporarily downloads the matching PDF, PPTX, DOCX, or text file through the signed-in Brightspace browser session.
- Extracts text on Nexora, discards the temporary bytes, and keeps only processed text for later questions.
- Retries the original AI request automatically after processing.
- Shows a clear reconnect/error message when Brightspace or the extension is unavailable.

## Install
1. Deploy the included website files and requirements.
2. Open chrome://extensions and enable Developer mode.
3. Remove/disable the older Nexora Sync version.
4. Load the `extension` folder unpacked.
5. Sign into Brightspace once, detect courses, and run one full sync.
6. In Nexora ask: `Explain Lecture 4 from ELEC 2507`.

## Limits
- Chrome must be running and the Brightspace session must remain valid.
- Maximum lecture file size: 30 MB.
- Supported extraction: PDF, PPTX, DOCX, and common text/code files.
- Image-only scanned lectures need OCR, which is not enabled in this version.
