# Nexora Sync v0.2

## Install locally
1. Open Chrome or Edge extensions.
2. Enable Developer mode.
3. Choose **Load unpacked** and select this `extension` folder.
4. In Nexora, open **Nexora Sync**, generate an extension key, and copy it.
5. Open the extension, enter the deployed Nexora URL and key, then save.
6. Click **Open Brightspace & import everything**.
7. Log into Brightspace normally if prompted and leave the browser open while the importer visits course pages.

## What this version imports
- Course code and title
- Course image/banner when exposed in the page
- Course Brightspace link and term when visible
- Content/module links
- Lecture/resource file links
- Assignments and quizzes visible in Brightspace pages
- Announcements visible in Brightspace pages

The importer uses the student's existing Brightspace browser session. It does not collect or store Brightspace passwords. Brightspace HTML differs between institutions, so selectors should be tested against the real Carleton pages and refined if a section is missed.
