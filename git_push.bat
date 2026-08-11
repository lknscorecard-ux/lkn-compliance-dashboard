@echo off
cd /d "%~dp0"
echo.
echo Pushing latest changes to GitHub...
echo.
git push
echo.
if %ERRORLEVEL% == 0 (
    echo SUCCESS - all changes pushed.
) else (
    echo.
    echo Login required. Running: gh auth login
    gh auth login
    git push
)
echo.
pause
