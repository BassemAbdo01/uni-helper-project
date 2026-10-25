# Grade Intelligence v2.8

This build focuses on outline ingestion and extraction reliability.

- Recognizes headings such as Assessment Breakdown, Assessment Distribution, Grade Distribution,
  Evaluation and Marking Scheme, Marking Scheme, Basis of Grade, and similar variants.
- Reads PDF tables whose assessment name, description, and percentage are extracted on separate lines.
- Preserves multiple alternative grading options rather than adding them into one invalid total.
- Supports PNG, JPG, JPEG, and WEBP outline screenshots.
- Screenshot path uses local OCR when available and Gemini Vision as a fallback.
- Includes deterministic acceptance tests for a 20/30/50 Statics scheme and a four-option Linear Algebra scheme.

No parser can honestly guarantee perfect extraction from every university document. Low-confidence results
must still be reviewed by the student before being treated as official.
