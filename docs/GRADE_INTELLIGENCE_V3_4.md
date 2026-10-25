# Nexora Grade Intelligence v3.4

Focused stabilization for courses with multiple grading schemes.

## Fixed
- Alternative-scheme components now accept parser fields named `name`, `source`, `component`, or `label`.
- Saved results are reused across every scheme and each scheme displays its calculated numeric grade.
- Scheme cards show component names and weights instead of blank labels.
- Calculated option cards show the actual weighted calculation used.
- Added a persistent multi-file upload queue so students can add screenshots/pages in separate selections before analyzing all together.
- Each grade card visibly shows Saved, Unsaved changes, or Save failed.
- Required-final calculations ignore the currently saved final mark and solve a hypothetical final target.
- Single-scheme required-final calculations now work even when a final mark was previously saved.

## Local acceptance test
Dynamics example:
- Scheme A: Assignments 10, Project 20, Midterm 20, Final 50
- Scheme B: Project 20, Midterm 30, Final 50
- Scheme C: Assignments 10, Project 20, Final 70

With 100, 100, 100, and 59 respectively, the evaluator returns:
- Scheme A: 79.5%
- Scheme B: 79.5%
- Scheme C: 71.3%
