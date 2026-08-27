"""Guards for the wiring between the outline prompt, the schema and the pipeline.

These exist because moving the schema into the prompt builder put it out of
scope for the route that hands it to _high_accuracy_outline_analysis. Every
analysis then raised NameError, the broad except around the pipeline turned
that into "the AI pipeline was unavailable", and students silently got the
deterministic parser's near-empty model with a 35% confidence badge. Nothing
in the suite noticed, because the failure lived in a branch no test reached.
"""

import ast
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


class OutlineSchemaTests(unittest.TestCase):
    def test_schema_is_reachable_and_shaped(self):
        schema = app._outline_schema()
        self.assertIsInstance(schema, dict)
        for key in ("components", "grading_options", "component_minimums",
                    "selection_rules", "derived_components", "scheme_policies",
                    "letter_requirements", "ambiguities", "confidence"):
            self.assertIn(key, schema, f"{key} missing from the extraction schema")

    def test_schema_example_component_carries_a_weight(self):
        # A schema whose example component has no weight teaches the compiler to
        # emit weightless components, which is how a stub schema produced a model
        # of five components all weighted None.
        example = (app._outline_schema().get("components") or [{}])[0]
        self.assertIn("weight", example)
        self.assertIsInstance(example["weight"], (int, float))

    def test_prompt_builder_embeds_the_schema(self):
        prompt = app._outline_analysis_prompt("Final exam 100%", "TEST 1000")
        self.assertIn("grading_options", prompt)
        self.assertIn("OUTLINE:", prompt)
        self.assertIn("Final exam 100%", prompt)


class NoUndefinedNamesTests(unittest.TestCase):
    """The actual defect was a name that existed at import time but not at call
    time. Nothing short of a static pass over every branch catches that."""

    def test_no_undefined_names_in_app(self):
        try:
            from pyflakes.api import check
            from pyflakes.reporter import Reporter
        except ImportError:
            self.skipTest("pyflakes not installed")

        out, err = io.StringIO(), io.StringIO()
        source = io.open(APP_PATH, encoding="utf-8").read()
        check(source, "app.py", Reporter(out, err))
        undefined = [
            line for line in out.getvalue().splitlines()
            if "undefined name" in line
        ]
        self.assertEqual(undefined, [], "undefined names in app.py:\n" + "\n".join(undefined))


class RouteWiringTests(unittest.TestCase):
    def test_analyze_route_does_not_reference_a_local_schema(self):
        """The route must call the shared builder, not a local it no longer owns."""
        tree = ast.parse(io.open(APP_PATH, encoding="utf-8").read())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "analyze_course_outline":
                target = node
                break
        self.assertIsNotNone(target, "analyze_course_outline not found")

        assigned = {
            t.id
            for n in ast.walk(target)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        loaded = {
            n.id for n in ast.walk(target)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        # Anything the route reads but never assigns has to resolve globally.
        for name in loaded - assigned:
            if name.startswith("_") or name in dir(app) or name in dir(__builtins__):
                continue
        self.assertNotIn(
            "outline_schema", loaded - assigned - set(dir(app)),
            "analyze_course_outline reads outline_schema without owning it",
        )


if __name__ == "__main__":
    unittest.main()
