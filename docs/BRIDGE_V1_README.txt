NEXORA COURSE INTELLIGENCE v1.0 — NEXORA BRIDGE

IMPORTANT: The screenshot showed Nexora Sync 0.6. Remove/disable the old extension and load the extension folder from this package. The extension popup must show version 1.0.0.

What changed:
- Reliable website ↔ extension Nexora Bridge.
- Direct Brightspace file download with fallback URL resolution.
- Detects login pages instead of uploading HTML as a lecture.
- Detects PDF/PPTX/DOCX by file signature when Brightspace hides the extension.
- Better extraction and database-stage errors.
- Temporary lecture bytes are discarded; only extracted text is cached.
- Full-screen course AI remains multimodal with image paste, drag/drop, and attachments.

Install website:
1. Replace the deployed project files with this package.
2. Commit/push to GitHub.
3. Redeploy the Render web service.
4. Confirm Render installs pypdf, python-pptx, and python-docx.

Install extension:
1. Open chrome://extensions.
2. Remove or disable every older Nexora Sync version.
3. Enable Developer mode.
4. Click Load unpacked.
5. Select this package's extension folder.
6. Confirm the popup says Nexora Sync 1.0.
7. Save the connection, sign into Brightspace, Detect & clean courses, then Scan every course now.

Test:
- Open a course workspace.
- Choose a lecture that visibly exists in the Lectures tab.
- Ask: Explain [exact lecture title].
- Nexora should fetch, extract, cache as text, retry the AI request, and answer.
