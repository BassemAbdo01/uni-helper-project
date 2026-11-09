"""Who teaches the course, how to reach them, and when they are in.

Every outline states it and Nexora threw it away, so a student who wanted to
email their professor went back to Brightspace to find the address.

The shape is the `letter_requirements` shape: the model answers, and the
server checks the answer against the source and fills what the model missed.
Both halves are load-bearing.

- Verification, because a fabricated email address is the worst failure
  available here. A wrong grade is visibly a grade and gets questioned; a
  plausible address is indistinguishable from a real one until a student
  emails it about a deferral and never hears back.
- The deterministic reader, because there is no Gemini key on a self-hosted
  install and the free key 429s. A contact detail must not be a thing that
  only works when quota is available.

The sharpest test here is `test_an_address_is_checked_as_a_token_not_a_
substring`, which is a real defect this file caught: a substring check passes
"a.patel@carleton.ca" against an outline that says "anita.patel@carleton.ca",
because it literally is a substring of it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as nexora  # noqa: E402

FIELDS = ("instructor", "instructor_email", "office_hours", "office_location")

LABELLED = """
ELEC 2501 -- Circuits and Signals
Instructor: Dr. Anita Patel
Email: anita.patel@carleton.ca
Office: ME 4158
Office hours: Tuesdays 10:00-11:30 and by appointment
Teaching Assistant: Sam Oyelaran, sam.oyelaran@carleton.ca
"""

TA_FIRST = """
SYSC 2004 Object-Oriented Software
Teaching Assistants: Wei Zhang (wei.zhang@cmail.carleton.ca), Ana Souza
Course Instructor: Prof. Michael Oduya
e-mail: michael.oduya@carleton.ca
Office hours: Wednesday 14:00-16:00, HP 3302
"""

ONE_LINE = """
BIOL 1103 Foundations of Biology
Instructor: Dr. Helen Mbeki  Office: NI 340  Email: helen.mbeki@carleton.ca
Office Hours: Thursdays 13:00-14:30
General enquiries: biology@carleton.ca
"""

NO_PERSON = """
MATH 1004 Calculus
All questions go to the course coordinator.
Contact: advising@math.carleton.ca
Office hours are posted on Brightspace.
"""

TAUGHT_BY = """
CHEM 1101
Taught by Dr. Oluwaseun Adeyemi
oluwaseun.adeyemi@carleton.ca
Office hours:
Mondays and Fridays, 9:00-10:00
"""

NOTHING = """
ECOR 1010 Introduction to Engineering
Evaluation: Assignments 30%, Midterm 30%, Final 40%
"""


# --------------------------------------------------------------------------
# reading it
# --------------------------------------------------------------------------

def test_labels_on_their_own_lines():
    got = nexora._outline_contacts(LABELLED)
    assert got["instructor"] == "Dr. Anita Patel"
    assert got["instructor_email"] == "anita.patel@carleton.ca"
    assert got["office_hours"] == "Tuesdays 10:00-11:30 and by appointment"
    assert got["office_location"] == "ME 4158"


def test_the_teaching_assistant_listed_first_is_not_the_instructor():
    """An outline lists the professor and two TAs under labels one word apart,
    and the TAs are often first. Taking the first address in the document
    would hand the student the marker's inbox."""
    got = nexora._outline_contacts(TA_FIRST)
    assert got["instructor"] == "Prof. Michael Oduya"
    assert got["instructor_email"] == "michael.oduya@carleton.ca"


def test_the_whole_contact_block_on_one_line():
    """"Instructor: Dr. Mbeki  Office: NI 340  Email: ..." is how outlines
    actually print it. Anchoring the office label to the start of a line
    missed it on every one of them -- found by running this shape."""
    got = nexora._outline_contacts(ONE_LINE)
    assert got["instructor"] == "Dr. Helen Mbeki"
    assert got["instructor_email"] == "helen.mbeki@carleton.ca"
    assert got["office_location"] == "NI 340"
    assert got["office_hours"] == "Thursdays 13:00-14:30"


def test_taught_by_carries_no_colon():
    """Requiring a separator after every label missed "Taught by Dr. Adeyemi"
    entirely. The separator is optional only after a phrase that is already
    complete, or the word in a sentence swallows the rest of the line."""
    got = nexora._outline_contacts(TAUGHT_BY)
    assert got["instructor"] == "Dr. Oluwaseun Adeyemi"
    assert got["office_hours"] == "Mondays and Fridays, 9:00-10:00"


def test_a_departmental_address_is_not_a_person():
    """advising@, info@, registrar@ -- a student told to email "the
    instructor" there gets no answer and does not know why. Saying nothing is
    the honest answer."""
    got = nexora._outline_contacts(NO_PERSON)
    assert got["instructor_email"] == ""
    assert got["instructor"] == ""


def test_office_hours_that_name_no_time_are_not_office_hours():
    """"Office hours are posted on Brightspace" is prose about the concept.
    Storing it would put a sentence where a student expects a time."""
    assert nexora._outline_contacts(NO_PERSON)["office_hours"] == ""


def test_an_outline_with_no_contact_details_claims_none():
    got = nexora._outline_contacts(NOTHING)
    assert all(got[f] == "" for f in FIELDS)


def test_empty_text_is_answered_rather_than_raising():
    assert nexora._outline_contacts("") == {f: "" for f in FIELDS}
    assert nexora._outline_contacts(None) == {f: "" for f in FIELDS}


# --------------------------------------------------------------------------
# verifying it
# --------------------------------------------------------------------------

def test_an_address_is_checked_as_a_token_not_a_substring():
    """The defect this file caught. "a.patel@carleton.ca" IS a substring of
    "anita.patel@carleton.ca", so a substring check accepted an invented
    address that differs from the real one by four characters and goes
    nowhere. The same family as `\\bfinal\\b` matching "Final Lab Exam"."""
    out = nexora._verify_contacts(
        {"instructor_email": "a.patel@carleton.ca"}, LABELLED)
    assert out["instructor_email"] != "a.patel@carleton.ca"
    assert out["instructor_email"] == "anita.patel@carleton.ca"


def test_an_address_the_outline_never_states_is_refused():
    out = nexora._verify_contacts(
        {"instructor_email": "someone.else@example.com"}, NOTHING)
    assert out["instructor_email"] == ""


def test_an_answer_the_outline_does_state_is_kept_verbatim():
    model = {"instructor": "Dr. Anita Patel",
             "instructor_email": "anita.patel@carleton.ca",
             "office_hours": "Tuesdays 10:00-11:30 and by appointment",
             "office_location": "ME 4158"}
    out = nexora._verify_contacts(dict(model), LABELLED)
    assert all(out[f] == model[f] for f in FIELDS)


def test_a_field_the_model_left_blank_is_filled_from_the_outline():
    """Half the point of keeping a deterministic reader: the model skipping a
    field must not cost the student the field."""
    out = nexora._verify_contacts({"instructor": "Dr. Anita Patel"}, LABELLED)
    assert out["instructor_email"] == "anita.patel@carleton.ca"
    assert out["office_hours"] == "Tuesdays 10:00-11:30 and by appointment"


def test_verification_survives_an_analysis_with_no_contact_keys_at_all():
    """Every analysis saved before this shipped, and every deterministic
    fallback, arrives with none of these keys."""
    out = nexora._verify_contacts({"components": []}, LABELLED)
    assert out["instructor"] == "Dr. Anita Patel"
    assert out["components"] == []


def test_a_non_dict_analysis_is_handed_back_untouched():
    assert nexora._verify_contacts(None, LABELLED) is None
    assert nexora._verify_contacts([], LABELLED) == []


def test_the_schema_offers_the_four_fields():
    """The model has to be told they exist. They live in the schema rather
    than only in a rule, because the schema is what both the single-call
    prompt and the three-stage pipeline are built around."""
    schema = nexora._outline_schema()
    for field in FIELDS:
        assert field in schema


def test_the_rule_is_short():
    """Rule 17 is the warning: a paragraph crowded out rule 10 and
    `multi_option_eligibility` started failing on flash-lite, while three
    sentences passed. Prompt length is a variable in its own right."""
    rules = nexora._outline_extraction_rules()
    rule18 = rules.split("\n18.")[1]
    assert len(rule18.split(".")) <= 5, rule18
