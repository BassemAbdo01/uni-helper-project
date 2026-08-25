"""Tests for Gemini model tier selection.

Outline analysis decides whether a student is shown a correct grade, so it
runs on Pro-tier models. Everything else (flashcards, summaries, quizzes)
stays on flash, where cost matters more than deep reasoning.
"""
import os
import unittest

from app import _gemini_model_candidates


class ModelTierTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ('GEMINI_REASONING_MODEL', 'GEMINI_MODEL', 'GEMINI_FALLBACK_MODEL')}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_tier_is_unchanged(self):
        """Cheap tasks must not silently start costing Pro prices."""
        models = _gemini_model_candidates()
        self.assertTrue(all('pro' not in m.lower() for m in models),
                        f'a Pro model leaked into the default tier: {models}')
        self.assertEqual(models[0], 'gemini-3.6-flash')

    def test_reasoning_tier_prefers_pro(self):
        models = _gemini_model_candidates(reasoning=True)
        self.assertIn('pro', models[0].lower())

    def test_reasoning_tier_still_falls_back_to_flash(self):
        """A key without Pro access must degrade rather than fail outright."""
        models = _gemini_model_candidates(reasoning=True)
        self.assertTrue(any('flash' in m.lower() for m in models),
                        'no flash fallback: outline uploads would hard-fail without Pro access')

    def test_pro_is_ordered_before_flash(self):
        models = _gemini_model_candidates(reasoning=True)
        first_pro = next(i for i, m in enumerate(models) if 'pro' in m.lower())
        first_flash = next(i for i, m in enumerate(models) if 'flash' in m.lower())
        self.assertLess(first_pro, first_flash)

    def test_env_override_wins(self):
        os.environ['GEMINI_REASONING_MODEL'] = 'gemini-custom-pro'
        self.assertEqual(_gemini_model_candidates(reasoning=True)[0], 'gemini-custom-pro')

    def test_env_override_does_not_affect_default_tier(self):
        os.environ['GEMINI_REASONING_MODEL'] = 'gemini-custom-pro'
        self.assertNotIn('gemini-custom-pro', _gemini_model_candidates())

    def test_no_duplicates(self):
        for models in (_gemini_model_candidates(), _gemini_model_candidates(reasoning=True)):
            self.assertEqual(len(models), len(set(models)))

    def test_blank_env_values_are_ignored(self):
        os.environ['GEMINI_REASONING_MODEL'] = '   '
        models = _gemini_model_candidates(reasoning=True)
        self.assertTrue(all(m.strip() for m in models))
        self.assertIn('pro', models[0].lower())


if __name__ == '__main__':
    unittest.main()
