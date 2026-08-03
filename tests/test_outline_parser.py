"""Deterministic acceptance tests for Grade Intelligence outline parsing.
Run with: python -m unittest tests.test_outline_parser
"""
import unittest
from app import _local_outline_analysis

STATICS = """
7) Evaluation and marking scheme
Assessment Breakdown
Item Description Weight
Midterm Exam I
Closed book. Tentatively Weekend of Jan 31 - Feb 1
20%
Midterm Exam II
Closed book. Tentatively Weekend of March 7/8
30%
Final Exam
Closed book. scheduled by the University
50%
"""

MATH_OPTIONS = """
Evaluation in this Course:
We have four options for the evaluation in this course.
Option 1: The course grade will be assigned based on the final exam only.
Final exam: 100%
Option 2:
The best test of all written tests: 15%
Tutorial attendance: 5%
Final exam: 80%
Option 3:
The average of best two written tests: 30%
Tutorial attendance: 5%
Final exam: 65%
Option 4:
The average of three written tests: 45%
Tutorial attendance: 5%
Final exam: 50%
Students must obtain at least 30% from at least one test, obtain at least 30% from the final exam,
obtain at least 50% on the total course grade, and attend at least two tutorials.
"""

class OutlineParserTests(unittest.TestCase):
    def test_separate_cell_assessment_breakdown(self):
        result=_local_outline_analysis(STATICS,'ECOR1033')
        self.assertEqual([(c['name'],c['weight']) for c in result['components']],
                         [('Midterm I',20.0),('Midterm II',30.0),('Final Exam',50.0)])
        self.assertEqual(result['total_weight'],100.0)
        self.assertEqual(result['confidence'],'high')

    def test_multi_option_scheme_is_not_flattened(self):
        result=_local_outline_analysis(MATH_OPTIONS,'MATH1104')
        self.assertEqual(len(result['grading_options']),4)
        self.assertTrue(result['multiple_options'])
        self.assertEqual(result['final_minimum'],30.0)

if __name__=='__main__': unittest.main()
