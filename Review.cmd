@echo off
REM Opens the review station: every form of the handover in the bulk
REM designer, with Approve / Needs changes per form. Verdicts are saved to
REM designer\verdicts.json. Leave this window open while you review.
cd /d "%~dp0"
node designer\review_server.mjs %*
pause
