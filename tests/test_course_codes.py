"""Course-code normalisation, against the titles Carleton actually publishes.

These are not invented strings. They are what appears on the My Courses page:
merged sections, crosslisted pairs, bracketed registrar ids, and support
programmes that carry no course code at all. Two faults came out of running the
normaliser over them, and both were the kind that only show up on real data.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REAL_TITLES = [
    ("ECOR1056D Eng Disciplines II (LEC) Winter 2026", "ECOR1056D"),
    ("Merge ECOR1031 D:E Programming and Data Management (LEC) Winter 2026 [11939:11940]", "ECOR1031"),
    ("ECOR1034D Dynamics (LEC) Winter 2026", "ECOR1034D"),
    ("Crosslist MATH1104F:MATH1107F [13424:13494] Linear Algebra (Eng or Sci ) (LEC) Winter 2026", "MATH1104F"),
    ("ECOR1033D Statics (LEC) Winter 2026", "ECOR1033D"),
    ("Merge PHYS1004 A:B:C Electromagnetism and Wave Motion (LEC) Winter 2026 [14078:14079:14080]", "PHYS1004"),
]


@pytest.mark.parametrize("title,expected", REAL_TITLES)
def test_real_course_titles_resolve_to_their_code(title, expected):
    from app import _normalize_course_code
    assert _normalize_course_code(title) == expected


@pytest.mark.parametrize("term", ["Winter 2026", "Fall 2025", "Summer 2026", "SPRING 2027"])
def test_a_term_is_not_a_course(term):
    """A course code is three to six letters and four digits, and so is
    "Winter 2026". Left alone, every org unit named for its term collapsed into
    a single invented course called WINTER2026."""
    from app import _normalize_course_code
    assert _normalize_course_code(term) == "OTHER"


@pytest.mark.parametrize("title", [
    "(MS-LAP) Math and Stats Learning Assistance Program - Winter 2026",
    "Co-operative Education Work Term Reflection and Reporting Seminar 2026",
    "A" * 300,
])
def test_a_code_always_fits_its_column(title):
    """course_code and course_full are String(40). SQLite keeps an over-long
    value quietly; Postgres raises StringDataRightTruncation and the sync dies.
    A student enrolled in anything long and codeless would have taken the whole
    import down in production and nowhere else."""
    from app import _normalize_course_code, COURSE_CODE_MAX
    code = _normalize_course_code(title)
    assert 0 < len(code) <= COURSE_CODE_MAX == 40


def test_an_empty_title_is_other_not_empty():
    from app import _normalize_course_code
    assert _normalize_course_code("") == "OTHER"
    assert _normalize_course_code(None) == "OTHER"


def test_the_cap_matches_every_column_that_stores_a_code():
    """If a column is ever widened or narrowed, the cap has to move with it."""
    import app as nexora
    from app import COURSE_CODE_MAX
    for model, field in [(nexora.SyncedCourse, "course_code"),
                         (nexora.CourseContent, "course_full"),
                         (nexora.GradeEntry, "course_full"),
                         (nexora.Task, "course_full"),
                         (nexora.AcademicUpdate, "course_full")]:
        width = getattr(model, field).property.columns[0].type.length
        assert width == COURSE_CODE_MAX, f"{model.__name__}.{field} is {width}, cap is {COURSE_CODE_MAX}"
