# Gunicorn auto-loads this file from the working directory unless a different
# config path is passed explicitly, so this takes effect with the existing
# `gunicorn app:app` start command -- no Render dashboard change needed.
#
# The course-outline analysis pipeline (/api/course/<code>/outline/analyze)
# makes several sequential Gemini API calls (fact extraction, policy
# compilation, adversarial audit), each with its own internal timeout of up
# to 90-120s and up to 3 retries. That can legitimately take several minutes
# for a large outline. Gunicorn's default worker timeout is 30s, so the
# worker was being killed mid-request while it was still waiting on a normal,
# in-progress Gemini response -- not an actual crash or real OOM.
timeout = 300
graceful_timeout = 300
