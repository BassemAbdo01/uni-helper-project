"""Regression tests for component key matching.

Numbered group members past 9 were silently dropped because plain substring
containment treated "Lab 1" as a duplicate of "Lab 10". An outline saying
"best 10 of 13 labs" ended up with only 9 enterable cards, so the selection
rule could never be satisfied.
"""
import re
import unittest

from app import _component_keys_match


def _key(name):
    return re.sub(r'[^a-z0-9]+', ' ', str(name or '').lower()).strip()


def register_all(names):
    """Mirror of the dedupe loop in analyze_course_outline."""
    seen, kept = set(), []
    for name in names:
        key = _key(name)
        if not key:
            continue
        if key in seen or any(_component_keys_match(key, k) for k in seen):
            continue
        seen.add(key)
        kept.append(name)
    return kept


class ComponentKeyMatchingTests(unittest.TestCase):
    def test_numbered_siblings_are_distinct(self):
        self.assertFalse(_component_keys_match(_key('Lab 1'), _key('Lab 10')))
        self.assertFalse(_component_keys_match(_key('Quiz 2'), _key('Quiz 12')))
        self.assertFalse(_component_keys_match(_key('Assignment 1'), _key('Assignment 11')))

    def test_identical_names_still_match(self):
        self.assertTrue(_component_keys_match(_key('Lab 1'), _key('lab  1')))
        self.assertTrue(_component_keys_match(_key('Final Exam'), _key('final exam')))

    def test_loose_containment_still_collapses_aliases(self):
        # The behaviour the containment check was added for: a rule naming a
        # component more briefly than the outline did must not create a
        # duplicate zero-weight card.
        self.assertTrue(_component_keys_match(_key('Labs'), _key('Labs (all 5 count)')))

    def test_parent_group_is_not_confused_with_its_members(self):
        self.assertFalse(_component_keys_match(_key('Labs'), _key('Lab 1')))
        self.assertFalse(_component_keys_match(_key('Quizzes'), _key('Quiz 1')))

    def test_all_thirteen_labs_survive_registration(self):
        labs = [f'Lab {i}' for i in range(1, 14)]
        self.assertEqual(register_all(labs), labs)

    def test_ecor1031_group_sizes(self):
        """The outline in the bug report: best 4 of 6 quizzes, best 10 of 13 labs."""
        quizzes = [f'Quiz {i}' for i in range(1, 7)]
        labs = [f'Lab {i}' for i in range(1, 14)]
        kept = register_all(quizzes + labs)
        self.assertEqual(len([n for n in kept if n.startswith('Quiz')]), 6)
        self.assertEqual(len([n for n in kept if n.startswith('Lab')]), 13)

    def test_duplicates_are_still_removed(self):
        self.assertEqual(register_all(['Lab 1', 'Lab 1', 'lab 1']), ['Lab 1'])


if __name__ == '__main__':
    unittest.main()
