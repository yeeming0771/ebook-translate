@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m ebook_translate %*
exit /b %errorlevel%
