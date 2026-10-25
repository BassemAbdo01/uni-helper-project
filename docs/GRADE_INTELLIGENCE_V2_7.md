# Grade Intelligence v2.7

This build focuses only on the course-outline/grade-calculation subsystem.

## Implemented
- Preserves multiple alternative grading schemes instead of merging them.
- Exact deterministic support for the uploaded MATH 1104F / MATH 1107F Winter 2026 outline.
- Creates input fields for Test 1, Test 2, Test 3, tutorial attendance, and final exam.
- Calculates all four options and chooses the highest eligible result.
- Enforces all four official pass requirements separately from the numeric grade.
- Shows an explicit PASS, FAIL, or INCOMPLETE result.
- Supports marks entered as earned points / maximum points (for example 15 / 20 or 75 / 100).
- Calculates the final-exam mark required for a chosen target under every eligible option.

## Acceptance checks performed
- Uploaded outline parsed into 4 schemes, each totaling 100%.
- Inputs detected: three tests out of 50, attendance out of 5, final out of 100.
- Scenario: all tests 100%, attendance 5/5, final 29% -> highest numeric result 64.5%, official result FAIL because the final minimum is 30%.

## Not claimed
This build was not tested against the user's live Render database or authenticated browser session. Deploy and re-analyze the outline to replace the previous incorrect profile.
