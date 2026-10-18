# Run the daily thinking retrospective.
$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
python "$here\habit_feedback.py" reflect
