"""Tests for folding group members underneath their parent component.

The calculator rendered every registered component as an equal full-size card,
so an outline with 13 labs and 6 quizzes produced 25 boxes with no indication
that most of them rolled up into two.
"""
import unittest
from types import SimpleNamespace

from app import _group_grade_entries, _parse_group_note


def entry(eid, name, weight, notes=''):
    return SimpleNamespace(id=eid, name=name, weight=weight, notes=notes,
                           score=None, max_score=100, category='assessment')


def ecor1031_entries():
    """The outline from the bug report."""
    rows = [
        entry('q', 'Quizzes', 10.0),
        entry('l', 'Labs', 10.0),
        entry('b', 'Bonus: Class Participation', 4.0, 'Bonus points'),
        entry('m', 'Midterm', 20.0),
        entry('f', 'Final exam', 60.0),
    ]
    rows += [entry(f'q{i}', f'Quiz {i}', 0.0,
                   'Counts toward Quizzes — best 4 of 6 counts.') for i in range(1, 7)]
    rows += [entry(f'l{i}', f'Lab {i}', 0.0,
                   'Counts toward Labs — best 10 of 13 counts.') for i in range(1, 14)]
    rows += [entry('exam', 'Examination component', 0.0,
                   'Required minimum applies to this component.')]
    return rows


class ParseGroupNoteTests(unittest.TestCase):
    def test_best_n_of_m(self):
        self.assertEqual(_parse_group_note('Counts toward Labs — best 10 of 13 counts.'),
                         ('Labs', 'best 10 of 13 counts'))

    def test_drop_lowest(self):
        parent, rule = _parse_group_note('Counts toward Quizzes — the lowest 1 of 6 is dropped.')
        self.assertEqual(parent, 'Quizzes')
        self.assertIn('lowest', rule)

    def test_bare_note_has_no_rule(self):
        self.assertEqual(_parse_group_note('Counts toward Labs.'), ('Labs', ''))

    def test_non_group_notes_are_ignored(self):
        self.assertEqual(_parse_group_note('Required minimum applies to this component.'), (None, ''))
        self.assertEqual(_parse_group_note('Bonus points'), (None, ''))
        self.assertEqual(_parse_group_note(''), (None, ''))
        self.assertEqual(_parse_group_note(None), (None, ''))


class GroupGradeEntriesTests(unittest.TestCase):
    def test_ecor1031_collapses_to_six_cards(self):
        groups = _group_grade_entries(ecor1031_entries())
        self.assertEqual(len(groups), 6)
        self.assertEqual([g['parent'].name for g in groups],
                         ['Quizzes', 'Labs', 'Bonus: Class Participation',
                          'Midterm', 'Final exam', 'Examination component'])

    def test_members_land_under_the_right_parent(self):
        by_name = {g['parent'].name: g for g in _group_grade_entries(ecor1031_entries())}
        self.assertEqual(len(by_name['Quizzes']['children']), 6)
        self.assertEqual(len(by_name['Labs']['children']), 13)
        self.assertEqual(by_name['Labs']['rule'], 'best 10 of 13 counts')

    def test_every_member_appears_exactly_once(self):
        groups = _group_grade_entries(ecor1031_entries())
        seen = [g['parent'].id for g in groups]
        for g in groups:
            seen += [c.id for c in g['children']]
        self.assertEqual(len(seen), len(set(seen)), 'a component was rendered twice')
        self.assertEqual(len(seen), len(ecor1031_entries()), 'a component was lost')

    def test_pass_rule_is_not_a_mark_entry(self):
        by_name = {g['parent'].name: g for g in _group_grade_entries(ecor1031_entries())}
        self.assertEqual(by_name['Examination component']['kind'], 'requirement')

    def test_weighted_components_stay_single(self):
        by_name = {g['parent'].name: g for g in _group_grade_entries(ecor1031_entries())}
        for name in ('Midterm', 'Final exam', 'Bonus: Class Participation'):
            self.assertEqual(by_name[name]['kind'], 'single')
            self.assertEqual(by_name[name]['children'], [])

    def test_a_member_never_becomes_its_own_parent(self):
        # A note naming a parent that does not exist must not self-match.
        rows = [entry('a', 'Lab 1', 0.0, 'Counts toward Lab 1 — best 1 of 1 counts.')]
        groups = _group_grade_entries(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['children'], [])

    def test_empty_input(self):
        self.assertEqual(_group_grade_entries([]), [])
        self.assertEqual(_group_grade_entries(None), [])

    def test_group_with_more_than_nine_members_is_intact(self):
        """Guards the interaction with the numbered-sibling dedupe fix."""
        rows = [entry('l', 'Labs', 10.0)]
        rows += [entry(f'l{i}', f'Lab {i}', 0.0,
                       'Counts toward Labs — best 10 of 13 counts.') for i in range(1, 14)]
        groups = _group_grade_entries(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]['children']), 13)


if __name__ == '__main__':
    unittest.main()
