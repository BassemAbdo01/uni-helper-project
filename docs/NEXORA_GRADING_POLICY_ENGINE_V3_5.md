# Nexora Grading Policy Engine v3.5

This release moves Grade Intelligence from a flat weight parser toward a course-agnostic grading-policy engine.

Implemented:
- University grading scale must be configured before course-outline analysis.
- Grading-scale screenshots/PDFs can be analyzed, reviewed, and saved to the user's account.
- Multi-page course outlines remain supported.
- Best-N-of-M / drop-lowest selection rules can be represented and executed.
- Derived or aggregate pass rules can be represented and executed (for example, a theory group made of quizzes + tutorials + final).
- Conditional scheme eligibility can use a saved numeric or letter threshold (for example, Scheme 2 only if Scheme 1 is C- or higher).
- Detected policies require confirmation before official calculations run.
- The UI explicitly states that AI interpretation may not be 100% accurate.
- Dashboard courses are grouped by Fall/Winter/Summer term.

Important limitation:
No AI can guarantee perfect interpretation of every possible outline. Nexora now surfaces confidence/ambiguity and requires confirmation rather than silently pretending certainty.
