import unittest
from types import SimpleNamespace
from app import _grade_row_map, _evaluate_option, _evaluate_pass_rules

class GradingPolicyEngineTests(unittest.TestCase):
    def row(self,name,score,max_score=100,weight=0):
        return SimpleNamespace(name=name,score=score,max_score=max_score,weight=weight)

    def test_best_one_of_two_selection_rule(self):
        rows=[self.row('Midterm 1',40),self.row('Midterm 2',80),self.row('Final Exam',70)]
        analysis={'selection_rules':[{'type':'best_n_of_m','components':['Midterm 1','Midterm 2'],'keep':1,'output_component':'Midterm'}]}
        option={'option_name':'Updated scheme','components':[{'source':'Midterm','weight':30},{'source':'Final Exam','weight':70}]}
        result=_evaluate_option(option,_grade_row_map(rows),analysis=analysis)
        self.assertTrue(result['complete'])
        self.assertAlmostEqual(result['grade'],73.0)

    def test_theory_group_minimum(self):
        rows=[self.row('Quizzes',100,100,15),self.row('Tutorials',100,100,25),self.row('Final Exam',10,100,60),self.row('Labs',80,100,35)]
        analysis={'components':[{'name':'Quizzes','weight':15},{'name':'Tutorials','weight':25},{'name':'Final Exam','weight':60},{'name':'Labs','weight':35}],
                  'pass_rules':[{'type':'aggregate_minimum','components':['Quizzes','Tutorials','Final Exam'],'minimum_percent':50,'message':'Theory must be at least 50%'}]}
        passed,details=_evaluate_pass_rules(analysis,_grade_row_map(rows),70)
        self.assertFalse(passed)
        self.assertLess(details[0]['actual'],50)

if __name__=='__main__': unittest.main()
