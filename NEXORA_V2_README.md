# Nexora V2 Foundation

This build is a real architectural pass over the latest uploaded project.

## Included in this build

- A genuine WebGL/Three.js Study Room, not a screenshot with HTML hotspots.
- Interactive 3D laptop, notebook, books, calendar, timer, mug, lighting, camera movement, hover effects, and drawers using live Nexora data.
- Laptop portal transition into Mission Control.
- Outline-driven Grade Intelligence that automatically presents the exact components extracted from the course outline.
- Automatic preservation of entered marks when an outline is re-analyzed.
- Removal of obsolete empty grade components after a new outline analysis.
- Course pass marks, final-exam minimums, target simulation, best/worst cases, and outcome tables.
- Hardened course workspace tab navigation with query-string persistence and browser back/forward support.
- Existing Brightspace Sync, Course Outline upload, AI Tutor, Study Tools, and course data remain preserved.

## Important

The 3D room uses Three.js from jsDelivr. A normal internet connection is required for the first load. Flask remains the backend.

## Deploy

1. Replace the current project with this folder.
2. Push to GitHub and let Render redeploy.
3. Reload the extension only if you also replace its folder.
4. Open `/study-room` for the new immersive room.
5. Re-analyze a course outline once to rebuild its exact grade components.
