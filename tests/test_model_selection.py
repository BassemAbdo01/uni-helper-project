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
        # This used to pin an exact id -- 3.6-flash, then 3.7-flash -- and a
        # pinned id is wrong the moment availability moves, which is the whole
        # reason the order is re-probed. It broke on 2026-10-09 when 3.7-flash,
        # measured as answering every call in September, answered none at all.
        #
        # What must stay true whatever the probe says: a LITE model does not
        # lead. Whichever id is first reads a student's outline, and flash-lite
        # is the one documented as dropping selection_rules on a multi-option
        # outline. Ordering by uptime alone would put it there, because it is
        # the most available model and the weakest. The measured order itself
        # lives in the comment on _gemini_model_candidates, where it can be
        # updated by re-probing rather than by editing a test.
        self.assertNotIn('lite', models[0].lower(),
                         f'the weakest model is leading the chain: {models}')

    def test_vision_and_text_are_ordered_separately(self):
        """One list cannot serve both. Measured 2026-10-09: gemini-3.8-flash
        answered text one time in three and timed out on BOTH vision passes, so
        an order that is reasonable for text makes every image call pay a
        45-second timeout first."""
        text = _gemini_model_candidates()
        vision = _gemini_model_candidates(vision=True)
        self.assertNotEqual(text, vision)
        # nothing that timed out on every vision pass may sit ahead of a model
        # that answered every one of them
        lite = next(i for i, m in enumerate(vision) if 'lite' in m.lower())
        for timed_out in ('gemini-3.8-flash', 'gemini-3.7-flash'):
            if timed_out in vision:
                self.assertGreater(vision.index(timed_out), lite,
                                   f'{timed_out} times out on images and is ahead of flash-lite')

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
