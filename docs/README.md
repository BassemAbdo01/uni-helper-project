# Design documents and release notes

Working documents kept with the code. They are written as records of *why* a
thing is the way it is, with the bug or measurement that motivated it — the
same standard as [`../CLAUDE.md`](../CLAUDE.md), which is the consolidated
engineering log.

## Audits and designs

| Document | What it establishes |
|---|---|
| [`SYNC_AUDIT_V1.md`](SYNC_AUDIT_V1.md) | Eight ways the Brightspace sync could fail silently, lose data or duplicate it, ranked by what each costs a student |
| [`ACADEMIC_UPDATE_DESIGN_V1.md`](ACADEMIC_UPDATE_DESIGN_V1.md) | Dedupe and retention for the table that once reached 786 MB and suspended the database |
| [`OUTLOOK_FEASIBILITY_V1.md`](OUTLOOK_FEASIBILITY_V1.md) | Whether a university tenant permits a student to grant mail access, and what each Microsoft error code means for the build |

## Grade intelligence

The grading engine is the part of this project with the least tolerance for
being wrong, and it has the longest paper trail.

| Document | |
|---|---|
| [`NEXORA_GRADING_POLICY_ENGINE_V3_5.md`](NEXORA_GRADING_POLICY_ENGINE_V3_5.md) | The policy engine itself |
| [`GRADING_ACCURACY_V3_6.md`](GRADING_ACCURACY_V3_6.md) | Accuracy work |
| [`GRADE_INTELLIGENCE_V3_4.md`](GRADE_INTELLIGENCE_V3_4.md) · [`V2_9`](GRADE_INTELLIGENCE_V2_9.md) · [`V2_8`](GRADE_INTELLIGENCE_V2_8.md) · [`V2_7`](GRADE_INTELLIGENCE_V2_7.md) | Earlier passes |
| [`GRADE_INTELLIGENCE_STABILIZATION_V3_3.md`](GRADE_INTELLIGENCE_STABILIZATION_V3_3.md) | Stabilisation |

## LLM pipeline

| Document | |
|---|---|
| [`GEMINI_MODEL_FIX_V3_1.md`](GEMINI_MODEL_FIX_V3_1.md) | Model fallback ordered by measured availability rather than version number |
| [`OUTLINE_PNG_FIX_V3_0.md`](OUTLINE_PNG_FIX_V3_0.md) | Reading an outline from a photograph |

## Releases

[`ACADEMIC_OS_V1_4.md`](ACADEMIC_OS_V1_4.md) ·
[`NEXORA_V1_5_README.md`](NEXORA_V1_5_README.md) ·
[`NEXORA_V2_README.md`](NEXORA_V2_README.md) ·
[`NEXORA_V2_3_FIXES.md`](NEXORA_V2_3_FIXES.md) ·
[`STUDY_ROOM_V1_README.md`](STUDY_ROOM_V1_README.md) ·
[`BRIDGE_V1_README.txt`](BRIDGE_V1_README.txt) ·
[`COPILOT_SETUP.txt`](COPILOT_SETUP.txt)
