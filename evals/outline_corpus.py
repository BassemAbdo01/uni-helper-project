"""Course outlines chosen to be structurally different from one another.

The point of this corpus is generalization, not any one course. Each case
isolates a mechanism that shows up across universities -- dropped marks, an
overflow cap, a bonus that must not be counted, a hurdle that fails the
student, an aggregate taken over several components, alternative schemes, a
gate on one of those schemes, weight that moves between components, and an
outline that genuinely does not say enough. Wording is varied on purpose:
different heading names, tables in some, prose in others, marks out of 100 in
some and raw marks in others, since the parser must not key off one phrasing.

`expect` records only what is objectively checkable. `components` is the set of
(name, weight) pairs a correct reading produces, matched loosely on name.
`signals` names structured fields that must be populated for the case's
mechanism to have been understood at all -- extracting the weights but dropping
the rule that governs them is a partial answer, not a correct one.
"""

CASES = [
    {
        "id": "flat",
        "why": "Baseline. No mechanism, just a table. Anything that fails here is broken.",
        "text": """
Evaluation

The final grade in PSYC 2001 is based on the following:

Assignments        30%
Midterm Test       25%
Final Examination  45%

All assignments are due at the start of class on the posted date.
""",
        "expect": {
            "components": [("assignment", 30), ("midterm", 25), ("final", 45)],
            "total": 100,
            "signals": [],
        },
    },
    {
        "id": "drop_lowest",
        "why": "Dropped marks stated as prose rather than a 'best n of m' phrase.",
        "text": """
Basis of Grade

Six quizzes will be given over the term. Your lowest two quiz scores will be
discarded and the remaining four will count equally toward the quiz portion of
your grade.

Quizzes            20%
Term Project       30%
Final Examination  50%
""",
        "expect": {
            "components": [("quiz", 20), ("project", 30), ("final", 50)],
            "total": 100,
            "signals": ["selection_rules"],
        },
    },
    {
        "id": "overflow_cap",
        "why": "A component nominally worth one weight may count for more, up to a ceiling.",
        "text": """
Marking Scheme

Component                     Weight
Laboratory work               10% (may count up to a maximum of 11.5%)
Participation                 5%
Midterm                       25%
Final exam                    60%

Laboratory work above the stated weight is carried over only to the maximum
shown.
""",
        "expect": {
            "components": [("lab", 10), ("participation", 5), ("midterm", 25), ("final", 60)],
            "total": 100,
            "signals": ["cap"],
        },
    },
    {
        "id": "bonus_excluded",
        "why": "Extra credit is phrased like a component and must still not be counted.",
        "text": """
Assessment Distribution

Weekly problem sets     25%
Midterm examination     30%
Final examination       45%

Students who attend at least eight tutorials will receive a bonus of 3% added
to their final course mark. This bonus is in addition to the 100% above and
cannot raise a failing grade to a pass.
""",
        "expect": {
            "components": [("problem set", 25), ("midterm", 30), ("final", 45)],
            "total": 100,
            "signals": [],
            "must_not_contain": ["bonus", "tutorial"],
        },
    },
    {
        "id": "final_hurdle",
        "why": "A true hurdle: below this you fail the course regardless of the total.",
        "text": """
Course Evaluation

Labs                20%
Assignments         20%
Final Examination   60%

Notwithstanding the weights above, a student must obtain at least 50% on the
final examination in order to pass the course. A student who does not do so
will receive a grade of F regardless of their term work.
""",
        "expect": {
            "components": [("lab", 20), ("assignment", 20), ("final", 60)],
            "total": 100,
            "signals": ["component_minimums"],
        },
    },
    {
        "id": "aggregate_minimum",
        "why": "The threshold applies to several components taken together, not to one.",
        "text": """
Grade Distribution

Quizzes                 15%
Tutorial work           10%
Midterm                 25%
Final examination       50%

To pass the course a student must achieve at least 50% on the examination
component, defined as the midterm and the final examination taken together.
""",
        "expect": {
            "components": [("quiz", 15), ("tutorial", 10), ("midterm", 25), ("final", 50)],
            "total": 100,
            "signals": ["derived_components"],
        },
    },
    {
        "id": "alternative_schemes",
        "why": "Two complete schemes; the student receives whichever is better.",
        "text": """
Method of Evaluation

Your grade will be computed under both of the following schemes and you will
receive the higher of the two results.

Scheme A
  Term tests      40%
  Final exam      60%

Scheme B
  Final exam     100%
""",
        "expect": {
            "components": [("term test", 40), ("final", 60)],
            "total": 100,
            "signals": ["grading_options"],
            "options_count": 2,
        },
    },
    {
        "id": "gated_scheme",
        "why": "The second scheme is conditional; treating it as always available is wrong.",
        "text": """
Determination of Grade

A first mark is calculated as:
  Assignments   30%
  Midterm       20%
  Final exam    50%

If, and only if, that mark converts to a letter grade of C- or higher, a second
mark will also be calculated using the same components with the final exam
weighted at 70% and the midterm at 0%. The higher of the two resulting letter
grades will be recorded. A student whose first mark is below C- receives the
first mark.
""",
        "expect": {
            "components": [("assignment", 30), ("midterm", 20), ("final", 50)],
            "total": 100,
            "signals": ["scheme_policies"],
        },
    },
    {
        "id": "weight_migration",
        "why": "Weight moves between components based on performance. The first version of "
               "this case demanded selection_rules and failed a correct answer: the model had "
               "expressed the transfer as two grading_options, which is arithmetically the same "
               "thing and is what the app already knows how to execute. The case now checks the "
               "mechanism is represented, not that it took one particular shape.",
        "text": """
Evaluation

Assignments          20%
Midterm test         30%
Final examination    50%

If your percentage on the final examination is higher than your percentage on
the midterm test, the midterm weight will be transferred to the final
examination, which will then be worth 80%. This adjustment is automatic and
applies only in the student's favour.
""",
        "expect": {
            "components": [("assignment", 20), ("midterm", 30), ("final", 50)],
            "total": 100,
            "signals": ["rules"],
            "options_count": 2,
        },
    },
    {
        "id": "underspecified",
        "why": "The outline genuinely omits the weights. Inventing plausible ones is the "
               "worst possible behaviour; saying so is correct.",
        "text": """
Evaluation

Students will be assessed through a combination of laboratory work, a midterm
test, participation in seminars, and a final examination. The relative
weighting of these components will be announced in class during the first week
of term and posted to Brightspace at that time.
""",
        "expect": {
            "components": [],
            "total": None,
            "signals": ["ambiguities"],
            "low_confidence": True,
        },
    },
]
