# Nexora Sync 0.5

This build keeps the working Carleton course discovery and replaces the rough content crawler.

## Improvements
- Uses one minimized worker window instead of opening tabs in the student's active window.
- Reuses one worker tab for the whole scan.
- Expands Load More / Show More controls and infinite-scroll content before parsing.
- Preserves course images when later scans return an empty image.
- Replaces old raw-link dumps course-by-course with a clean structured scan.
- Separates lectures, practice material, code/files, assignments, quizzes, announcements, and pages.
- Copies direct files up to 25 MB into Nexora when the Brightspace URL returns a real file.

## Important
Render's local filesystem is temporary. The included file-copy storage is suitable for testing. Before production, configure durable object storage such as Cloudflare R2, Amazon S3, or Render Persistent Disk.

Auto-sync works while Chrome is running and the student's Brightspace session remains signed in. True syncing while the laptop is off requires official Brightspace OAuth/API access.
