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
        "id": "prose_bonus",
        "why": "A bonus stated in a sentence rather than in the table. It still has a stated "
               "amount a student can earn, so it must be recorded -- grade points are summed "
               "without normalising, so recording it is what credits the student. What it must "
               "NOT do is join the base scheme and dilute the other weights.",
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
            "total": 100,          # base only; the bonus is checked separately
            "signals": [],
            "bonus": ("tutorial", 3),
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
               "worst possible behaviour; saying so is correct. Note what passes this case: "
               "the raw model invents four components here every time -- flash-lite did, and "
               "so does gemini-3.7-flash -- and _scheme_is_uncorroborated is what refuses "
               "them. Two different right answers exist here and both pass: the single-call "
               "path names the four assessments at weight 0 with a note that the weighting is "
               "unannounced, which is honest and useful; the three-stage pipeline invented "
               "25/25/20/30 at HIGH confidence, and _scheme_is_uncorroborated is what refuses "
               "that. So the case scores whether a WEIGHT was fabricated, never whether an "
               "assessment was named -- flagging the latter failed a correct answer.",
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
    {
        "id": "multi_option_eligibility",
        "why": "The shape `_math1104_outline_analysis` is hardcoded for. Four schemes whose "
               "availability depends on how many tests the student actually wrote, selection "
               "inside each scheme rather than across the outline, a threshold counted in "
               "tutorials attended rather than percent, and a floor on a single test. Nothing "
               "here is unique to that course -- multi-option evaluation is common -- so the "
               "general path should reach it without a course-specific branch.",
        "text": """
Evaluation

There are four options for the evaluation in this course. Your grade will be
computed under every option you are eligible for, and the highest resulting
grade will be awarded.

Option 1: The course grade will be assigned based on the final exam only.
  Final Exam                                   100%

Option 2: Best written test, tutorials and final exam.
  Best of the three written tests               15%
  Tutorial attendance                            5%
  Final Exam                                    80%

Option 3: Average of best two written tests, tutorials and final exam.
  Average of best two written tests             30%
  Tutorial attendance                            5%
  Final Exam                                    65%

Option 4: All three written tests, tutorials and final exam.
  Average of all three written tests            45%
  Tutorial attendance                            5%
  Final Exam                                    50%

Options 3 and 4 require that you have written at least two and three of the
written tests respectively. Each written test is marked out of 50. Tutorial
attendance is worth 5% and you must attend at least two eligible tutorials.

Regardless of the option used, you must obtain at least 30% on at least one
written test, at least 30% on the final exam, and at least 50% overall in order
to pass the course.
""",
        "expect": {
            "components": [("test", 45), ("tutorial", 5), ("final", 50)],
            "total": 100,
            "signals": ["grading_options", "component_minimums", "selection_rules"],
            "options_count": 4,
        },
    },
    {
        "id": "two_column_schemes_with_bonus",
        "why": "A real Carleton outline. One row per component and a column per scheme, so "
               "each component carries two different weights, and a bonus row sits in the "
               "same table as the graded ones. The bonus is what made Scheme 2 total 104%: "
               "the rule excluding it existed but was never sent to the pipeline. Also "
               "carries a prerequisite letter rule that must not become a course-fail rule. The "
               "bonus must SURVIVE: grade points are summed without normalising, so a 4% bonus "
               "component adds exactly 4 points and a student who earns it genuinely finishes at "
               "104%. Dropping it under-reports every student who did the work.",
        "text": """
To pass the course, students must pass the final examination (i.e., achieve at least 50%), and
achieve at least 4/10 on the lab component and at least 4/10 on the assignment component. For
students who pass the final exam and meet these requirements, a numeric mark out of 100 will be
calculated by weighting the course components according to Scheme 1.

If you meet the above requirements and the grade under Scheme 1 is lower than C-, this will be the
final grade.

If you meet the above requirements and the letter grade under Scheme 1 is C- or higher, then a
second numeric mark will be calculated using Scheme 2, and this mark will be converted to a second
letter grade. The final grade will be the higher of the two letter grades.

Component                     Required                        Scheme 1   Scheme 2
Assignments (best 2 out of 3) at least 4/10                   0%         10% (max of 12%)
Labs (best 10 out of 12)      at least 4/10                   0%         10% (max of 11.5%)
Bonus: Class Participation    see attendance details below    0%         4% (bonus)
Midterm                       see Missed Midterm below        30%        20%
Final exam                    at least 50/100                 70%        60%

Note: SYSC 1006/2006 with a minimum grade of C- is a prerequisite for SYSC 2100 and several 3000
and 4000 level SYSC courses.
""",
        "expect": {
            "components": None,   # ambiguous which scheme belongs at top level; options carry the assertion
            "total": None,
            "signals": ["grading_options", "component_minimums", "scheme_policies"],
            "options_count": 2,
            "options_base_total": 100,
            "some_option_contains": ["participation"],
        },
    },

    {
        "id": "points_not_percent",
        "why": "A syllabus priced in raw points, which is the North American default and is "
               "not in this corpus anywhere else. Nexora's entire model is percentage weight, "
               "so a component recorded at its point value is catastrophic and silent: 500 "
               "points of work becomes 500% of a course, every contribution is five times too "
               "big, and the student is told they are failing. The correct reading divides by "
               "the stated total.",
        "text": """
Grading

Your grade is based on 500 possible points, distributed as follows:

  Homework (10 sets, 10 points each)      100 points
  Laboratory reports (5 @ 20)             100 points
  Midterm examination                     100 points
  Final examination                       200 points
                                          -----------
  Total                                   500 points

Letter grades are assigned on the usual scale: 450 points and above is an A,
400 and above a B, 350 and above a C, 300 and above a D.
""",
        "expect": {
            "components": [("homework", 20), ("lab", 20), ("midterm", 20), ("final", 40)],
            "total": 100,
            "signals": [],
        },
    },
    {
        "id": "penalties_are_not_weights",
        "why": "Every number on this page is a percentage and only four of them are weights. "
               "A late penalty, an attendance deduction and a pass mark all read like rows in "
               "a grading table to anything matching on '%'. One false component dilutes every "
               "real one, so this case fails on an EXTRA component as hard as on a missing one.",
        "text": """
Course Evaluation

Term paper                40%
Seminar presentation      15%
Participation             15%
Final examination         30%

Late work is penalized at 5% per day, to a maximum deduction of 25%. Students
who miss more than three seminars without documentation will have 10% deducted
from their participation mark. A final course mark of at least 50% is required
to pass. The instructor reserves the right to adjust the class average by up to
3% in either direction.
""",
        "expect": {
            "components": [("term paper", 40), ("presentation", 15), ("participation", 15),
                           ("final", 30)],
            "total": 100,
            "signals": [],
            "must_not_contain": ["late", "deduct", "adjust"],
        },
    },
    {
        "id": "ungraded_hurdle",
        "why": "A component worth no marks that can still fail the course. Recording it as a "
               "weighted component corrupts the arithmetic; dropping it loses a requirement "
               "that fails students every year. It belongs in the rules, not in the scheme.",
        "text": """
Evaluation

Midterm test              35%
Final examination         45%
Term assignments          20%

In addition, the six laboratory sessions are graded on a satisfactory /
unsatisfactory basis and carry no percentage weight. A student who does not
receive a satisfactory grade in at least five of the six laboratories will
receive a grade of F in the course, irrespective of their standing on the
components above.
""",
        "expect": {
            "components": [("midterm", 35), ("final", 45), ("assignment", 20)],
            "total": 100,
            "signals": ["rules"],
            "must_not_contain": ["laborator"],
        },
    },

    {
        "id": "two_finals_two_schemes",
        "why": "A lecture final AND a lab final, inside alternative schemes. Nexora reads "
               "'the final exam' in four places and every one of them matched any component "
               "containing the word, so the hypothetical mark used by 'what do I need on the "
               "final' was applied to both at once and the answer was unreachable arithmetic. "
               "The extraction side of that is what this case checks: both finals must survive "
               "as separate components with their own weights in every scheme, because merging "
               "them or dropping one is the reading that makes the solver's job impossible. "
               "Real shape -- a science course with a lecture stream and a lab stream is the "
               "commonest place two finals appear.",
        "text": """
Evaluation

The course has a lecture component and a laboratory component, each with its own
final examination. Your grade is computed under both schemes below and the
higher of the two results is recorded.

Scheme A
  Term work                       40%
  Final examination               40%
  Final laboratory examination    20%

Scheme B
  Term work                       20%
  Final examination               60%
  Final laboratory examination    20%

You must pass the final laboratory examination (at least 50%) to pass the
course, regardless of your overall mark.
""",
        "expect": {
            "components": None,   # either scheme may sit at top level
            "total": None,
            "signals": ["grading_options", "component_minimums"],
            "options_count": 2,
            "options_base_total": 100,
            "some_option_contains": ["laboratory"],
        },
    },
]
