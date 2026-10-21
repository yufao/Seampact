@echo off
setlocal
pushd "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "SEAMPACT_PYTHON=python"
if exist "%~dp0.venv-agentdojo\Scripts\python.exe" set "SEAMPACT_PYTHON=%~dp0.venv-agentdojo\Scripts\python.exe"
"%SEAMPACT_PYTHON%" -B -m seampact serve --home var/prototype --host 127.0.0.1 --port 8880 %*
set "SEAMPACT_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %SEAMPACT_EXIT_CODE%
