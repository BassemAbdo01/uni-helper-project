/* What KIND of thing this is.
 *
 * ONE definition, loaded by both the service worker (importScripts) and the
 * injected content script (executeScript lists it before content.js). There
 * were two copies, character for character, and two copies of a rule this
 * fiddly is two rules the moment either is touched -- the same class of bug as
 * two screens disagreeing about a grade, one layer down.
 *
 * The type decides where an item appears in a course workspace, how much of
 * its text is kept (_CONTENT_TEXT_CAP on the server), and whether the study
 * tools will ever offer to summarise it. Getting it wrong does not lose the
 * item; it hides it somewhere the student will not look.
 */
(function (root) {
  'use strict';
  if (root.classifyContent) return;          // injected into several frames

  /* Words that name lecture material on their own, with no number anywhere.
   *
   * The old rule required a digit AFTER the keyword --
   * `slides?\s*[-_#:]?\s*\d+` -- and real Brightspace courses number the other
   * way round. Measured against one student's own account: "01 SLIDES",
   * "02 Slides - Rydberg eqn", "10. Gas NOTES", "05 Nomenclature NOTES A",
   * "15 Stoichiometry Video LECTURES", "10: Video Lecture: Gases". Not one
   * matched, so every one was filed as `resource` or `page` and never appeared
   * in the workspace's Lectures list. CHEM1101 ended up with 3 lectures
   * against 90 resources.
   *
   * `notes` was not a keyword at all, and it is the single commonest name a
   * lecturer gives their own material.
   *
   * `slides` is plural on purpose: a singular "slide" is a lab protocol far
   * more often than a deck -- "Wet-Mounted Slide Preparation for Cheek Cells"
   * is a real title from the same account.
   */
  var LECTURE_WORDS = /\b(lectures?|slides|slide\s*deck|lesson|notes|handout\s*notes)\b/i;

  /* A number on EITHER side of the keyword. "Week 3" and "3 - Week" both
   * happen, and so do "Topic 04" and "04. Periodic properties". */
  var LECTURE_NUMBERED = new RegExp(
    '(?:lecture|lec|slides?|week|wk|module|chapter|ch|lesson|unit|topic)\\s*[-_#:.]?\\s*\\d+'
    + '|\\d+\\s*[-_#:.]?\\s*(?:lecture|lec|slides?|week|module|chapter|lesson|unit|topic)\\b'
    + '|\\b(?:l|lec|wk|ch)\\s*[-_#]?\\s*\\d+\\b', 'i');

  /* Things that carry a lecture word but are not the material: the schedule
   * saying when lectures are, the discussion board for asking about them, the
   * office-hours page. Classifying these as lectures is how a student opens
   * "Lectures" and finds a Discord link. */
  var NOT_THE_MATERIAL = /\b(schedule|timetable|calendar|office\s*hours|discussion|forum|ask\s+them|q\s*&\s*a|sign[\s-]*up|attendance|evaluation)\b/i;

  function classifyContent(title, url, activityType, moduleContext) {
    title = title || '';
    url = url || '';
    var ownText = (title + ' ' + url + ' ' + (activityType || '')).toLowerCase();
    var fullText = (ownText + ' ' + (moduleContext || '')).toLowerCase();
    var ext = ((title.match(/\.([a-z0-9]{1,8})\b/i) || url.match(/\.([a-z0-9]{1,8})(?:$|[?#])/i) || [])[1] || '').toLowerCase();

    if (/course\s*(?:outline|syllabus|information|guide)|syllabus|grading\s*(?:scheme|breakdown|policy)|evaluation\s*(?:scheme|breakdown)|marking\s*scheme|assessment\s*(?:scheme|breakdown)|course\s*policies/.test(ownText)) return 'outline';
    if (/practice|sample question|review question|worksheet|problem set/.test(ownText)) return 'practice';

    /* Anything that names an assessment is ABOUT that assessment, and the
     * decision ends here rather than falling through.
     *
     * It used to fall through, and that was invisible until the lecture rules
     * got better at their job: "Week 5 Quiz - Chapter 4 ... Proposal Assignment
     * Guidelines" contains "guideline", which sets isSupportingDoc, which
     * skipped the quiz branch -- and then "Week 5" matched a lecture number and
     * the quiz was filed as a lecture. Twelve of them on one real account.
     *
     * A supporting doc is still not the assessment itself: a rubric is not the
     * assignment and must not show up as one on the dashboard. It is a
     * resource, which is where a student looks for a rubric. */
    var isSupportingDoc = /instructions?|guideline|rubric|handout|answer key|solutions?|prep\b/.test(ownText);
    if (/assignment|dropbox|\bquiz(zing)?\b/.test(ownText)) {
      if (isSupportingDoc) return 'resource';
      return /assignment|dropbox/.test(ownText) ? 'assignment' : 'quiz';
    }
    if (/announcement|news/.test(ownText)) return 'announcement';

    // A lecture word in the item's OWN title wins. The module name is weaker
    // evidence -- "Lecture Material" is a folder that also holds the schedule
    // -- so it only counts alongside a number, as it always did.
    if (LECTURE_WORDS.test(title) && !NOT_THE_MATERIAL.test(ownText)) return 'lecture';
    if (LECTURE_NUMBERED.test(fullText) && !NOT_THE_MATERIAL.test(ownText)) return 'lecture';
    if (['mp4', 'mov', 'm4v', 'webm', 'mp3', 'm4a', 'wav'].indexOf(ext) >= 0
        && /lecture|lec|recording|class|week|module/.test(fullText)) return 'lecture';
    // A slide deck in a course is lecture material whatever it is called.
    if (['ppt', 'pptx', 'key'].indexOf(ext) >= 0) return 'lecture';
    if (['py', 'c', 'cpp', 'h', 'java', 'js', 'ts', 'ipynb', 'zip', 'rar', '7z', 'csv', 'xlsx', 'xls'].indexOf(ext) >= 0) return 'resource';
    if (['pdf', 'doc', 'docx', 'mp4', 'mov', 'm4v', 'webm', 'mp3', 'm4a', 'wav'].indexOf(ext) >= 0) return 'resource';
    return 'page';
  }

  root.classifyContent = classifyContent;
  root.NEXORA_CLASSIFY = {LECTURE_WORDS: LECTURE_WORDS, LECTURE_NUMBERED: LECTURE_NUMBERED,
                          NOT_THE_MATERIAL: NOT_THE_MATERIAL};
})(typeof self !== 'undefined' ? self : this);
